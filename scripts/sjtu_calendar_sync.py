#!/usr/bin/env python3
"""Preview and synchronize SJTU calendar events to a CalDAV calendar.

The module is import-safe: network access and calendar mutations happen only through
``main()``. Preview is the default. Writes require ``--apply``; deletion additionally
requires ``--delete-stale`` and a plan-specific confirmation token.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

APP_NAME = "sjtu-calendar-sync"
SJTU_LOGIN_URL = "https://i.sjtu.edu.cn/jaccountlogin"
SJTU_CALENDAR_URL = "https://calendar.sjtu.edu.cn/api/event/list"
JACCOUNT_HOST = "jaccount.sjtu.edu.cn"
SJTU_HOST = "i.sjtu.edu.cn"
DEFAULT_CALDAV_URL = "https://caldav.icloud.com"
MANAGED_PROPERTY = "X-SJTU-CALENDAR-SYNC-ID"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT = (10, 30)
CALDAV_TIMEOUT = 30
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60
SOURCE_ID_KEYS = ("id", "eventId", "eventID", "uuid", "uid")


class SyncError(RuntimeError):
    """A user-facing synchronization failure."""


@dataclass(frozen=True)
class SourceEvent:
    source_id: str
    title: str
    starts_at: date | datetime
    ends_at: date | datetime
    location: str = ""

    @property
    def managed_id(self) -> str:
        return hashlib.sha256(self.source_id.encode("utf-8")).hexdigest()

    @property
    def uid(self) -> str:
        return f"sjtu-{self.managed_id[:32]}@calendar-sync"


@dataclass
class SyncPlan:
    creates: list[SourceEvent] = field(default_factory=list)
    updates: list[tuple[Any, SourceEvent]] = field(default_factory=list)
    unchanged: list[tuple[Any, SourceEvent]] = field(default_factory=list)
    deletes: list[Any] = field(default_factory=list)


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_time(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        return value.astimezone(SHANGHAI_TZ).isoformat()
    return value.isoformat()


def stable_source_id(raw_event: Mapping[str, Any]) -> str:
    for key in SOURCE_ID_KEYS:
        value = raw_event.get(key)
        if value not in (None, ""):
            return str(value)
    keys = ", ".join(sorted(str(key) for key in raw_event))
    raise SyncError(
        "SJTU event is missing a stable identifier. "
        f"Available fields: {keys or '(none)'}. Refusing to create unstable events."
    )


def parse_source_event(raw_event: Mapping[str, Any]) -> SourceEvent:
    try:
        starts_at = datetime.strptime(str(raw_event["startTime"]), "%Y-%m-%d %H:%M")
        ends_at = datetime.strptime(str(raw_event["endTime"]), "%Y-%m-%d %H:%M")
    except (KeyError, TypeError, ValueError) as exc:
        raise SyncError("SJTU event has an invalid startTime or endTime") from exc

    all_day = str(raw_event.get("allDay", "false")).lower() == "true"
    if all_day:
        start_date = starts_at.date()
        end_date = ends_at.date()
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        start_value: date | datetime = start_date
        end_value: date | datetime = end_date
    else:
        start_value = starts_at.replace(tzinfo=SHANGHAI_TZ)
        end_value = ends_at.replace(tzinfo=SHANGHAI_TZ)
        if end_value <= start_value:
            raise SyncError("Timed SJTU event ends before it starts")

    return SourceEvent(
        source_id=stable_source_id(raw_event),
        title=normalize_text(raw_event.get("title")) or "Untitled SJTU event",
        starts_at=start_value,
        ends_at=end_value,
        location=normalize_text(raw_event.get("location")),
    )


def source_response_is_complete(
    data: Mapping[str, Any], event_count: int, requested_page_size: int
) -> bool:
    for key in ("total", "totalCount", "recordsTotal"):
        if data.get(key) not in (None, ""):
            try:
                return event_count >= int(data[key])
            except (TypeError, ValueError):
                return False

    for key in ("hasNext", "hasMore"):
        if key in data:
            value = data[key]
            if isinstance(value, str):
                value = value.lower() == "true"
            return not bool(value)

    reported_page_size = requested_page_size
    for key in ("pageSize", "size", "limit"):
        if data.get(key) not in (None, ""):
            try:
                reported_page_size = min(reported_page_size, int(data[key]))
            except (TypeError, ValueError):
                return False
            break
    return event_count < reported_page_size


def build_http_session() -> Any:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_policy = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "HEAD", "OPTIONS")),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry_policy))
    return session


def cookie_cache_path() -> Path:
    from platformdirs import user_cache_path

    return Path(user_cache_path(APP_NAME, ensure_exists=True)) / "jaccount.cookies"


def clear_cookie_cache() -> bool:
    path = cookie_cache_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def load_cookie_cache(session: Any) -> bool:
    path = cookie_cache_path()
    if not path.exists():
        return False
    if time.time() - path.stat().st_mtime > COOKIE_MAX_AGE_SECONDS:
        path.unlink(missing_ok=True)
        return False
    if path.stat().st_mode & 0o077:
        raise SyncError(f"Cookie cache permissions are too broad: {path}")

    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=False)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise SyncError("Could not read the JAccount cookie cache") from exc
    session.cookies.update(jar)
    return True


def save_cookie_cache(session: Any) -> None:
    path = cookie_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="cookies-", dir=path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.chmod(0o600)
        jar = http.cookiejar.MozillaCookieJar(str(temporary_path))
        for cookie in session.cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True, ignore_expires=True)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def is_authenticated_sjtu_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == SJTU_HOST
        and not parsed.path.rstrip("/").endswith("/jaccountlogin")
    )


def show_qr_and_wait(login_uuid: str, timeout_seconds: int = 180) -> None:
    import qrcode
    from websocket import create_connection

    login_seen = threading.Event()
    monitor_errors: list[Exception] = []
    websocket = create_connection(
        f"wss://{JACCOUNT_HOST}/jaccount/sub/{login_uuid}", timeout=timeout_seconds
    )

    def monitor() -> None:
        try:
            while not login_seen.is_set():
                raw_message = websocket.recv()
                if not raw_message:
                    return
                message = json.loads(raw_message)
                message_type = message.get("type")
                if message_type == "LOGIN":
                    login_seen.set()
                    return
                if message_type == "UPDATE_QR_CODE":
                    payload = message.get("payload") or {}
                    if "ts" not in payload or "sig" not in payload:
                        raise SyncError("JAccount returned an incomplete QR payload")
                    query = urlencode(
                        {"uuid": login_uuid, "ts": payload["ts"], "sig": payload["sig"]}
                    )
                    image = qrcode.make(f"https://{JACCOUNT_HOST}/jaccount/confirmscancode?{query}")
                    image.show()
        except Exception as exc:  # The main thread reports a sanitized failure.
            monitor_errors.append(exc)
            login_seen.set()

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        websocket.send(json.dumps({"type": "UPDATE_QR_CODE"}))
        print("Scan the JAccount QR code in the image window.")
        if not login_seen.wait(timeout_seconds):
            raise SyncError("JAccount QR login timed out")
        if monitor_errors:
            raise SyncError("JAccount QR login channel failed") from monitor_errors[0]
    finally:
        websocket.close()
        thread.join(timeout=2)


def authenticate_sjtu(session: Any, cache_session: bool) -> None:
    if cache_session:
        load_cookie_cache(session)

    response = session.get(SJTU_LOGIN_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if is_authenticated_sjtu_url(response.url):
        return

    parsed = urlparse(response.url)
    if parsed.scheme != "https" or parsed.hostname != JACCOUNT_HOST:
        raise SyncError("SJTU login redirected to an unexpected host")

    match = re.search(r'uuid:\s*"([^"]+)"', response.text)
    if not match:
        raise SyncError("Could not locate the JAccount QR session identifier")
    login_uuid = match.group(1)
    show_qr_and_wait(login_uuid)

    for attempt in range(5):
        response = session.get(
            f"https://{JACCOUNT_HOST}/jaccount/expresslogin",
            params={"uuid": login_uuid},
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if is_authenticated_sjtu_url(response.url):
            if cache_session:
                save_cookie_cache(session)
            return
        time.sleep(2 ** min(attempt, 2))
    raise SyncError("JAccount authentication did not complete")


def fetch_source_events(
    session: Any, days_before: int, days_after: int, page_size: int
) -> tuple[list[SourceEvent], bool, datetime, datetime]:
    now = datetime.now(SHANGHAI_TZ)
    window_start = now - timedelta(days=days_before)
    window_end = now + timedelta(days=days_after)
    response = session.get(
        SJTU_CALENDAR_URL,
        params={
            "startDate": window_start.strftime("%Y-%m-%d %H:00:00"),
            "endDate": window_end.strftime("%Y-%m-%d %H:00:00"),
            "weekly": "false",
            "ids": "",
            "pageSize": page_size,
        },
        headers={"Referer": "https://calendar.sjtu.edu.cn/ui/calendar"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise SyncError("SJTU calendar returned invalid JSON") from exc
    data = payload.get("data") if isinstance(payload, Mapping) else None
    raw_events = data.get("events") if isinstance(data, Mapping) else None
    if not isinstance(raw_events, list):
        raise SyncError("SJTU calendar response is missing data.events")
    if not raw_events:
        raise SyncError("SJTU calendar returned zero events; refusing to continue")

    events_by_id: dict[str, SourceEvent] = {}
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            raise SyncError("SJTU calendar returned a non-object event")
        event = parse_source_event(raw_event)
        existing = events_by_id.get(event.source_id)
        if existing is not None and existing != event:
            raise SyncError(f"SJTU returned conflicting rows for source ID {event.source_id!r}")
        events_by_id[event.source_id] = event

    complete = source_response_is_complete(data, len(raw_events), page_size)
    return list(events_by_id.values()), complete, window_start, window_end


def get_component(resource: Any) -> Any:
    getter = getattr(resource, "get_icalendar_component", None)
    if callable(getter):
        return getter()
    return resource.icalendar_component


def component_text(resource: Any, name: str, default: str = "") -> str:
    value = get_component(resource).get(name)
    return default if value is None else str(value)


def component_time(resource: Any, name: str) -> date | datetime | None:
    value = get_component(resource).get(name)
    return getattr(value, "dt", value)


def source_matches_resource(source: SourceEvent, resource: Any) -> bool:
    return (
        normalize_text(component_text(resource, "summary")) == source.title
        and normalize_time(component_time(resource, "dtstart")) == normalize_time(source.starts_at)
        and normalize_time(component_time(resource, "dtend")) == normalize_time(source.ends_at)
        and normalize_text(component_text(resource, "location")) == source.location
        and component_text(resource, MANAGED_PROPERTY) == source.managed_id
    )


def build_plan(source_events: Sequence[SourceEvent], existing_resources: Iterable[Any]) -> SyncPlan:
    plan = SyncPlan()
    managed: dict[str, list[Any]] = {}
    for resource in existing_resources:
        marker = component_text(resource, MANAGED_PROPERTY)
        if marker:
            managed.setdefault(marker, []).append(resource)

    source_ids = {event.managed_id for event in source_events}
    for event in source_events:
        matches = managed.get(event.managed_id, [])
        if not matches:
            plan.creates.append(event)
            continue
        primary, *duplicates = matches
        if source_matches_resource(event, primary):
            plan.unchanged.append((primary, event))
        else:
            plan.updates.append((primary, event))
        plan.deletes.extend(duplicates)

    for managed_id, resources in managed.items():
        if managed_id not in source_ids:
            plan.deletes.extend(resources)
    return plan


def set_component_property(component: Any, name: str, value: Any) -> None:
    if name in component:
        del component[name]
    if value not in (None, ""):
        component.add(name, value)


def create_event(target_calendar: Any, source: SourceEvent) -> Any:
    from icalendar import Calendar, Event

    calendar_data = Calendar()
    calendar_data.add("prodid", "-//SJTU Calendar Sync//EN")
    calendar_data.add("version", "2.0")
    event_data = Event()
    event_data.add("uid", source.uid)
    event_data.add("dtstamp", datetime.now(UTC))
    event_data.add("summary", source.title)
    event_data.add("dtstart", source.starts_at)
    event_data.add("dtend", source.ends_at)
    if source.location:
        event_data.add("location", source.location)
    event_data.add(MANAGED_PROPERTY, source.managed_id)
    calendar_data.add_component(event_data)
    return target_calendar.add_event(calendar_data.to_ical().decode("utf-8"))


def update_event(resource: Any, source: SourceEvent) -> None:
    editor = getattr(resource, "edit_icalendar_component", None)
    if callable(editor):
        with editor() as component:
            set_component_property(component, "summary", source.title)
            set_component_property(component, "dtstart", source.starts_at)
            set_component_property(component, "dtend", source.ends_at)
            set_component_property(component, "location", source.location)
            set_component_property(component, MANAGED_PROPERTY, source.managed_id)
        resource.save()
        return

    component = resource.icalendar_component
    set_component_property(component, "summary", source.title)
    set_component_property(component, "dtstart", source.starts_at)
    set_component_property(component, "dtend", source.ends_at)
    set_component_property(component, "location", source.location)
    set_component_property(component, MANAGED_PROPERTY, source.managed_id)
    resource.save()


def calendar_display_name(calendar: Any) -> str:
    getter = getattr(calendar, "get_display_name", None)
    if callable(getter):
        return str(getter())
    return str(getattr(calendar, "name", ""))


def connect_target_calendar(url: str, username: str, password: str, name: str) -> tuple[Any, Any]:
    import caldav

    client = caldav.DAVClient(
        url=url,
        username=username,
        password=password,
        timeout=CALDAV_TIMEOUT,
    )
    try:
        principal_getter = getattr(client, "get_principal", None)
        principal = principal_getter() if callable(principal_getter) else client.principal()
        calendars_getter = getattr(principal, "get_calendars", None)
        calendars = calendars_getter() if callable(calendars_getter) else principal.calendars()
        for calendar in calendars:
            if calendar_display_name(calendar) == name:
                return client, calendar
    except Exception:
        client.close()
        raise
    client.close()
    available = ", ".join(sorted(filter(None, (calendar_display_name(c) for c in calendars))))
    raise SyncError(f"CalDAV calendar {name!r} was not found. Available: {available or '(none)'}")


def event_label(source: SourceEvent) -> str:
    return f"{source.title} ({normalize_time(source.starts_at)})"


def deletion_label(resource: Any) -> str:
    return (
        f"{component_text(resource, 'summary', 'Untitled event')} "
        f"({normalize_time(component_time(resource, 'dtstart')) or 'unknown time'})"
    )


def print_plan(plan: SyncPlan, source_complete: bool) -> None:
    print("\nSync preview")
    print(f"  Create:    {len(plan.creates)}")
    print(f"  Update:    {len(plan.updates)}")
    print(f"  Unchanged: {len(plan.unchanged)}")
    print(f"  Stale/duplicate managed events: {len(plan.deletes)}")
    print(f"  Source completeness verified: {'yes' if source_complete else 'no'}")

    for event in plan.creates:
        print(f"    + {event_label(event)}")
    for _, event in plan.updates:
        print(f"    ~ {event_label(event)}")
    for resource in plan.deletes:
        print(f"    - {deletion_label(resource)}")
    if plan.deletes:
        print(f"  Deletion confirmation token: DELETE-{len(plan.deletes)}")


def validate_apply_safety(args: argparse.Namespace, plan: SyncPlan, source_complete: bool) -> None:
    if not args.apply or not args.delete_stale:
        return
    if not source_complete:
        raise SyncError("Source completeness is not verified; refusing stale-event deletion")
    if plan.deletes:
        expected = f"DELETE-{len(plan.deletes)}"
        if args.confirm_delete != expected:
            raise SyncError(
                f"Deletion requires --confirm-delete {expected}. No changes were applied."
            )


def apply_plan(plan: SyncPlan, target_calendar: Any, delete_stale: bool) -> None:
    write_errors: list[str] = []
    for event in plan.creates:
        try:
            create_event(target_calendar, event)
            print(f"Created: {event_label(event)}")
        except Exception as exc:
            write_errors.append(f"create {event.title!r}: {type(exc).__name__}")
    for resource, event in plan.updates:
        try:
            update_event(resource, event)
            print(f"Updated: {event_label(event)}")
        except Exception as exc:
            write_errors.append(f"update {event.title!r}: {type(exc).__name__}")

    if write_errors:
        details = "; ".join(write_errors)
        raise SyncError(f"One or more writes failed; deletion was skipped. {details}")

    if delete_stale:
        for resource in plan.deletes:
            label = deletion_label(resource)
            resource.delete()
            print(f"Deleted: {label}")


def get_caldav_credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = args.caldav_user or os.environ.get("CALDAV_USERNAME", "")
    if not username:
        if not sys.stdin.isatty():
            raise SyncError("Set CALDAV_USERNAME for non-interactive use")
        username = input("CalDAV username: ").strip()
    password = os.environ.get("CALDAV_APP_PASSWORD", "")
    if not password:
        if not sys.stdin.isatty():
            raise SyncError("Set CALDAV_APP_PASSWORD for non-interactive use")
        password = getpass.getpass("CalDAV app-specific password: ")
    if not username or not password:
        raise SyncError("CalDAV username and app-specific password are required")
    return username, password


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or sync SJTU events to a CalDAV calendar. Preview is the default."
    )
    parser.add_argument("--apply", action="store_true", help="Apply creates and updates")
    parser.add_argument(
        "--delete-stale",
        action="store_true",
        help="Delete stale managed events; requires --apply and confirmation",
    )
    parser.add_argument(
        "--confirm-delete",
        metavar="DELETE-N",
        help="Plan-specific deletion token printed by the preview",
    )
    parser.add_argument(
        "--calendar-name",
        default=os.environ.get("CALDAV_CALENDAR_NAME", "SJTU Calendar"),
    )
    parser.add_argument("--caldav-url", default=os.environ.get("CALDAV_URL", DEFAULT_CALDAV_URL))
    parser.add_argument("--caldav-user", help="CalDAV username; prefer CALDAV_USERNAME")
    parser.add_argument("--days-before", type=int, default=30)
    parser.add_argument("--days-after", type=int, default=30)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument(
        "--cache-session",
        action="store_true",
        help="Opt in to a 12-hour local JAccount cookie cache",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Delete the local cookie cache and exit",
    )
    args = parser.parse_args(argv)
    if args.delete_stale and not args.apply:
        parser.error("--delete-stale requires --apply")
    if args.confirm_delete and not args.delete_stale:
        parser.error("--confirm-delete requires --delete-stale")
    if args.days_before < 0 or args.days_after < 0 or args.page_size < 1:
        parser.error("day ranges must be non-negative and page size must be positive")
    return args


def run(args: argparse.Namespace) -> int:
    if args.logout:
        message = (
            "Removed local JAccount session cache." if clear_cookie_cache() else "No cache found."
        )
        print(message)
        return 0

    session = build_http_session()
    client = None
    try:
        authenticate_sjtu(session, args.cache_session)
        source_events, source_complete, window_start, window_end = fetch_source_events(
            session, args.days_before, args.days_after, args.page_size
        )
        username, password = get_caldav_credentials(args)
        client, target_calendar = connect_target_calendar(
            args.caldav_url, username, password, args.calendar_name
        )
        existing = target_calendar.search(
            start=window_start,
            end=window_end,
            event=True,
        )
        plan = build_plan(source_events, existing)
        print_plan(plan, source_complete)
        validate_apply_safety(args, plan, source_complete)
        if not args.apply:
            print("\nPreview only; no calendar changes were made.")
            return 0
        apply_plan(plan, target_calendar, args.delete_stale)
        print("\nSynchronization completed.")
        return 0
    finally:
        session.close()
        if client is not None:
            client.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except KeyboardInterrupt:
        print("\nCancelled; no further changes will be made.", file=sys.stderr)
        return 130
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected {type(exc).__name__}; secrets were not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
