---
name: sjtu-calendar-sync
description: Safely preview and sync authenticated SJTU calendar events to an iCloud or other CalDAV calendar. Use for SJTU calendar previews, one-way CalDAV synchronization, session-cache cleanup, or diagnosing this sync tool; do not use for unrelated calendar providers.
---

# SJTU Calendar Sync

Use `scripts/sjtu_calendar_sync.py` from this skill directory. The command opens an
interactive JAccount QR login and reads CalDAV credentials from environment variables
or a hidden password prompt.

## Safety invariants

- Treat the default invocation as preview-only. Do not add `--apply` unless the user
  asks to write calendar changes.
- Never add `--delete-stale` or supply its confirmation token without explicit user
  authorization immediately before deletion. Run a fresh preview first.
- Never request, display, log, or save a password, cookie, QR payload, or authentication
  URL in chat. Let the user scan the QR code and enter secrets in the terminal.
- The tool may update or delete only events carrying its `X-SJTU-CALENDAR-SYNC-ID`
  property. Do not weaken this boundary or adopt unmarked events by title or time.
- Stop on an empty or incomplete SJTU response before deletion. Do not bypass the
  completeness check.

## Workflow

1. Run `python3 scripts/sjtu_calendar_sync.py --help` when setup or options are unclear.
2. If dependencies are missing, install `requirements.txt` in an isolated environment.
3. Run a preview and report the create, update, unchanged, and stale counts.
4. For ordinary synchronization, ask before rerunning with `--apply`.
5. For stale-event deletion, show the exact previewed items and confirmation token, then
   wait for explicit authorization before rerunning with both `--apply` and
   `--delete-stale`.

Use `--logout` to remove the optional local JAccount session cache. The cache is disabled
unless the user passes `--cache-session`.
