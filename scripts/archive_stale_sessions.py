#!/usr/bin/env python3
"""Archive stale task records in coordination.sqlite3.

A task is considered stale when its status is not "done"/"cancelled" and its
last update is older than --days days. Stale records (active/paused/waiting
whose owning session has gone quiet) are the kind of leftovers that pile up
in the coordination database.

Without --apply the script is a dry run: it prints the stale records it would
archive and changes nothing. With --apply it marks them done, rewrites their
body JSON in place, and records an 'archived' event per task.

Usage:
  archive_stale_sessions.py [--days N] [--apply] [--db PATH]

  --dry-run is accepted for clarity and is the default (no-op without --apply).
Exit code: always 0 when the database is reachable, even if nothing is stale.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

DEFAULT_DB = os.path.expanduser("~/.claude-local-delegate/coordination.sqlite3")
TERMINAL = ("done", "cancelled")


def fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "n/a"


def read_tasks(db_path):
    """Return (tasks, error). Each task is a dict: id, rowid, body."""
    if not os.path.exists(db_path):
        return None, "database not found: %s" % db_path
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = con.execute(
            "SELECT rowid, id, body FROM tasks"
        ).fetchall()
        con.close()
    except sqlite3.Error as e:
        return None, "database error: %s" % e
    tasks = []
    for rowid, task_id, body in rows:
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            continue  # malformed body: skip, never archive what we can't read
        tasks.append({"rowid": rowid, "id": task_id, "body": parsed})
    return tasks, None


def is_stale(task_body, now, max_age_seconds):
    """True when status is open and the last update is older than the threshold."""
    if task_body.get("status") in TERMINAL:
        return False
    ref = task_body.get("updated_at") or task_body.get("created_at")
    if ref is None:
        return False
    return (now - float(ref)) > max_age_seconds


def archive(db_path, task, now, note):
    """Rewrite one task's body in place: status=done, updated_at=now, plus an event."""
    task["body"]["status"] = "done"
    task["body"]["updated_at"] = now
    task["body"]["note"] = (task["body"].get("note") or "").strip() + " | " + note
    con = sqlite3.connect(db_path, timeout=15)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE tasks SET owner=?, body=? WHERE rowid=?",
            (
                task["body"].get("owner"),
                json.dumps(task["body"]),
                task["rowid"],
            ),
        )
        con.execute(
            "INSERT INTO events(project, body) VALUES (?, ?)",
            (
                task["body"].get("project"),
                json.dumps(
                    {
                        "task_id": task["id"],
                        "owner": task["body"].get("owner"),
                        "kind": "archived",
                        "text": note,
                        "at": now,
                    }
                ),
            ),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(
        description="Archive stale (non-done/cancelled) task records in coordination.sqlite3."
    )
    parser.add_argument(
        "--days", type=int, default=2, help="archive tasks whose last update is older than N days (default: 2)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually update the records (default is dry-run: print only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="no-op alias for the default dry-run behaviour; mutually exclusive with --apply",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help="path to coordination.sqlite3 (default: %s)" % DEFAULT_DB
    )
    args = parser.parse_args()

    if args.dry_run and args.apply:
        parser.error("--apply and --dry-run are mutually exclusive")

    tasks, err = read_tasks(args.db)
    if err:
        print("error: %s" % err)
        sys.exit(1)

    now = time.time()
    max_age = float(args.days) * 86400.0
    note = (
        "archived by scripts/archive_stale_sessions.py (stale > %d days, was %s)"
        % (args.days, "status")
    )

    stale = [t for t in tasks if is_stale(t["body"], now, max_age)]

    print("database : %s" % args.db)
    print("threshold: last update older than %d day(s)" % args.days)
    print("tasks    : %d total, %d stale (not done/cancelled)" % (len(tasks), len(stale)))
    for t in sorted(stale, key=lambda x: x["body"].get("updated_at") or 0):
        b = t["body"]
        ref = b.get("updated_at") or b.get("created_at")
        age_days = (now - float(ref)) / 86400.0 if ref is not None else -1
        print(
            "  [stale] status=%-9s age=%6.2fd  updated=%s  project=%s  task=%s"
            % (
                b.get("status", "?"),
                age_days,
                fmt_ts(ref),
                b.get("project"),
                b.get("task_key"),
            )
        )

    if args.apply:
        for t in stale:
            try:
                archive(args.db, t, now, note)
                print("  archived -> done : %s/%s" % (t["body"].get("project"), t["body"].get("task_key")))
            except (sqlite3.Error, OSError) as e:
                print("  FAILED to archive : %s (%s)" % (t["id"], e))
    else:
        mode = "dry-run (no changes made; pass --apply to update)"
        print("mode     : %s" % mode)

    sys.exit(0)


if __name__ == "__main__":
    main()