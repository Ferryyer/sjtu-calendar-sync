from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar, Event

SCRIPT = Path(__file__).parents[1] / "scripts" / "sjtu_calendar_sync.py"
SPEC = importlib.util.spec_from_file_location("sjtu_calendar_sync_script", SCRIPT)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class FakeResource:
    def __init__(self, component):
        self.component = component

    def get_icalendar_component(self):
        return self.component


class FakeCalendar:
    def __init__(self):
        self.payload = None

    def add_event(self, payload):
        self.payload = payload
        return object()


def event(source_id="course-1", title="Calculus", location="Room 101"):
    tz = ZoneInfo("Asia/Shanghai")
    return sync.SourceEvent(
        source_id=source_id,
        title=title,
        starts_at=datetime(2026, 9, 22, 8, 0, tzinfo=tz),
        ends_at=datetime(2026, 9, 22, 10, 0, tzinfo=tz),
        location=location,
    )


def component_for(source, *, marked=True, title=None):
    component = Event()
    component.add("summary", title or source.title)
    component.add("dtstart", source.starts_at)
    component.add("dtend", source.ends_at)
    component.add("location", source.location)
    if marked:
        component.add(sync.MANAGED_PROPERTY, source.managed_id)
    return component


def test_parse_requires_a_stable_source_id():
    with pytest.raises(sync.SyncError, match="stable identifier"):
        sync.parse_source_event(
            {
                "title": "Calculus",
                "startTime": "2026-09-22 08:00",
                "endTime": "2026-09-22 10:00",
            }
        )


def test_all_day_same_date_gets_exclusive_end_date():
    parsed = sync.parse_source_event(
        {
            "id": "holiday-1",
            "title": "Holiday",
            "startTime": "2026-10-01 00:00",
            "endTime": "2026-10-01 00:00",
            "allDay": True,
        }
    )
    assert parsed.starts_at == date(2026, 10, 1)
    assert parsed.ends_at == date(2026, 10, 2)


def test_uid_is_stable_when_mutable_fields_change():
    original = event()
    changed = event(title="Advanced Calculus", location="Room 202")
    assert original.uid == changed.uid
    assert original.managed_id == changed.managed_id


def test_unmarked_user_event_is_never_adopted():
    source = event()
    personal_event = FakeResource(component_for(source, marked=False))
    plan = sync.build_plan([source], [personal_event])
    assert plan.creates == [source]
    assert plan.updates == []
    assert plan.deletes == []


def test_marked_event_updates_and_stale_managed_event_is_deletable():
    source = event()
    existing = FakeResource(component_for(source, title="Old title"))
    stale_source = event(source_id="old-course")
    stale = FakeResource(component_for(stale_source))
    plan = sync.build_plan([source], [existing, stale])
    assert plan.updates == [(existing, source)]
    assert plan.deletes == [stale]


def test_unchanged_managed_event_is_not_rewritten():
    source = event()
    existing = FakeResource(component_for(source))
    plan = sync.build_plan([source], [existing])
    assert plan.unchanged == [(existing, source)]
    assert plan.creates == []
    assert plan.updates == []


def test_created_icalendar_contains_only_hashed_management_id():
    source = event(source_id="private-source-id")
    target = FakeCalendar()
    sync.create_event(target, source)
    parsed = Calendar.from_ical(target.payload)
    created = next(component for component in parsed.walk() if component.name == "VEVENT")
    assert str(created[sync.MANAGED_PROPERTY]) == source.managed_id
    assert source.source_id not in target.payload


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://i.sjtu.edu.cn/", True),
        ("https://i.sjtu.edu.cn/jaccountlogin", False),
        ("http://i.sjtu.edu.cn/", False),
        ("https://example.com/", False),
    ],
)
def test_authenticated_url_requires_https_portal_not_login(url, expected):
    assert sync.is_authenticated_sjtu_url(url) is expected


@pytest.mark.parametrize(
    ("data", "count", "expected"),
    [
        ({"total": 3}, 3, True),
        ({"total": 4}, 3, False),
        ({"hasNext": False}, 200, True),
        ({"hasNext": True}, 199, False),
        ({}, 199, True),
        ({}, 200, False),
        ({"pageSize": 50}, 50, False),
    ],
)
def test_source_completeness_is_conservative(data, count, expected):
    assert sync.source_response_is_complete(data, count, 200) is expected


def test_delete_confirmation_is_plan_specific():
    args = sync.parse_args(["--apply", "--delete-stale", "--confirm-delete", "DELETE-1"])
    plan = sync.SyncPlan(deletes=[object(), object()])
    with pytest.raises(sync.SyncError, match="DELETE-2"):
        sync.validate_apply_safety(args, plan, source_complete=True)


def test_incomplete_source_blocks_deletion():
    args = sync.parse_args(["--apply", "--delete-stale", "--confirm-delete", "DELETE-1"])
    plan = sync.SyncPlan(deletes=[object()])
    with pytest.raises(sync.SyncError, match="completeness"):
        sync.validate_apply_safety(args, plan, source_complete=False)


def test_cookie_cache_age_constant_is_twelve_hours():
    assert sync.COOKIE_MAX_AGE_SECONDS == int(timedelta(hours=12).total_seconds())
