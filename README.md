# SJTU Calendar Sync

An unofficial, local-first tool and Codex skill for previewing SJTU calendar events and
synchronizing them one way to an iCloud or other CalDAV calendar.

The default command is read-only. It does not write calendar events until `--apply` is
provided, and it cannot delete stale events without a fresh plan-specific confirmation.

## Security notice

If an earlier copy of this script contained an iCloud app-specific password, revoke that
password before using this version. Never put a real password, Cookie, calendar export,
or `.env` file in Git.

This version:

- reads the CalDAV username from `CALDAV_USERNAME` or an interactive prompt;
- reads the app-specific password from `CALDAV_APP_PASSWORD` or a hidden prompt;
- stores no JAccount Cookie unless `--cache-session` is explicitly enabled;
- manages only events containing its private `X-SJTU-CALENDAR-SYNC-ID` property;
- uses a stable source ID instead of mutable title, time, or location fields.

## Setup

Python 3.11 or newer is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set credentials in the current terminal without placing them in a tracked file:

```bash
export CALDAV_USERNAME="your-icloud-address"
export CALDAV_APP_PASSWORD="your-app-specific-password"
export CALDAV_CALENDAR_NAME="SJTU Calendar"
```

## Preview and sync

Preview only:

```bash
python scripts/sjtu_calendar_sync.py
```

Apply creates and updates without deletion:

```bash
python scripts/sjtu_calendar_sync.py --apply
```

To delete stale events, first preview the plan. The preview prints a token such as
`DELETE-3`. Rerun only after checking every listed deletion:

```bash
python scripts/sjtu_calendar_sync.py \
  --apply \
  --delete-stale \
  --confirm-delete DELETE-3
```

The deletion count is tied to the current plan. If the plan changes, the command stops
before applying any changes.

## Optional session cache

Pass `--cache-session` to retain the JAccount session for at most 12 hours in the current
user's cache directory. Cookie domains and secure attributes are preserved. Remove it
with:

```bash
python scripts/sjtu_calendar_sync.py --logout
```

## Limits

- This is an unofficial integration and depends on SJTU web endpoints that may change.
- Deletion is disabled if the source response cannot be shown to be complete.
- The source event payload must expose a stable `id`, `eventId`, `eventID`, `uuid`, or
  `uid` field. The tool refuses to create events from mutable fields alone.
- A software license has not yet been selected; choose one before public distribution.
