#!/usr/bin/env python3
"""events.py - summarise the size-meter event log.

    dk events [--since YYYY-MM-DD] [--last N] [--json] [--file PATH]

Reads $KIMCHI_DK_DIR/state/events.jsonl (one JSON line per nudge or deny, written by a
size-meter hook) and prints counts by role, by level, by day (UTC), and the last N events.

Location: --file, else $KIMCHI_DK_DIR/state/events.jsonl. $KIMCHI_DK_DIR defaults to
~/.local/share/kimchi/delegate-kit; the env var, when set, wins everywhere.
Malformed lines and lines without a timestamp are skipped and counted as "skipped".
A missing log is not an error: it prints zero events. A log that exists but cannot be read
(permissions, a directory passed as --file) is an error.

Exit codes: 0 ok, 1 unreadable log, 2 bad arguments.

Stdlib only, Python >= 3.8.
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile


def kit_dir():
    """The kit dir: $KIMCHI_DK_DIR, else the ~/.local/share/kimchi/delegate-kit default."""
    env = os.environ.get("KIMCHI_DK_DIR", "").strip()
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".local", "share", "kimchi", "delegate-kit")


def default_log():
    """$KIMCHI_DK_DIR/state/events.jsonl (the --file default)."""
    return os.path.join(kit_dir(), "state", "events.jsonl")


def load(path, since=None):
    """(events, skipped). Each event is a dict with a string `ts`; `since` is a datetime.date or 'YYYY-MM-DD'
    lower bound, compared as a date. A missing file is (events=[], 0); any other OSError propagates."""
    events, skipped = [], 0
    if isinstance(since, str):
        since = datetime.datetime.strptime(since, "%Y-%m-%d").date()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return events, skipped
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(ev, dict) or not isinstance(ev.get("ts"), str) or len(ev["ts"]) < 10:
                skipped += 1
                continue
            if since:
                try:
                    day = datetime.date.fromisoformat(ev["ts"][:10])
                except ValueError:
                    skipped += 1
                    continue
                if day < since:
                    continue
            events.append(ev)
    return events, skipped


def summarise(events, last):
    def count(key):
        out = {}
        for ev in events:
            k = str(ev.get(key))
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))

    by_day = {}
    for ev in events:
        by_day[ev["ts"][:10]] = by_day.get(ev["ts"][:10], 0) + 1
    ordered = sorted(events, key=lambda e: e["ts"])
    return {"total": len(events), "by_role": count("role"), "by_level": count("level"),
            "by_day": dict(sorted(by_day.items())), "last": ordered[-last:] if last > 0 else []}


def render(summary, path, skipped):
    lines = ["events: %s" % path, "total: %d%s" % (summary["total"], " (skipped %d malformed)" % skipped if skipped else "")]
    for title, key in (("by role", "by_role"), ("by level", "by_level"), ("by day", "by_day")):
        lines.append("%s:" % title)
        lines.extend("  %-12s %d" % (k, v) for k, v in summary[key].items())
        if not summary[key]:
            lines.append("  (none)")
    lines.append("last %d:" % len(summary["last"]))
    for ev in summary["last"]:
        lines.append("  %s  %-8s %-3s ctx=%s calls=%s agent=%s" % (
            ev["ts"], ev.get("role"), ev.get("level"), ev.get("context"), ev.get("calls"),
            ev.get("agent_id") or ev.get("session_id") or "-"))
    if not summary["last"]:
        lines.append("  (none)")
    return "\n".join(lines)


def selftest():
    failures = []

    def check(name, ok):
        if not ok:
            failures.append(name)

    rows = [
        {"ts": "2026-10-01T10:00:00Z", "role": "main", "level": "delegate", "context": 210000, "calls": 3},
        {"ts": "2026-10-02T10:00:00Z", "role": "executer", "level": "L1", "context": 1, "calls": 40, "agent_id": "a1"},
        {"ts": "2026-10-02T11:00:00Z", "role": "executer", "level": "L2", "context": 2, "calls": 60, "agent_id": "a1"},
        {"ts": "2026-10-03T09:00:00Z", "role": "executer", "level": "L3", "context": 3, "calls": 70},
    ]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "events.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\nnot json\n[1]\n{\"no\": \"ts\"}\n")
        ev, sk = load(p)
        s = summarise(ev, 2)
        check("total", s["total"] == 4 and sk == 3)
        check("by_role", s["by_role"] == {"executer": 3, "main": 1})
        check("by_level", s["by_level"] == {"L1": 1, "L2": 1, "L3": 1, "delegate": 1})
        check("by_day", s["by_day"] == {"2026-10-01": 1, "2026-10-02": 2, "2026-10-03": 1})
        check("last", [e["level"] for e in s["last"]] == ["L2", "L3"])
        ev2, _ = load(p, "2026-10-02")
        check("since", len(ev2) == 3)
        check("since unpadded", len(load(p, "2026-10-2")[0]) == 3 and len(load(p, "2026-10-3")[0]) == 1)
        check("missing file", load(os.path.join(d, "none.jsonl")) == ([], 0))
        try:
            load(d)
            check("directory file", False)
        except OSError:
            pass
        with contextlib.redirect_stderr(io.StringIO()):
            check("main directory file", main(["--file", d]) == 1)
        old = os.environ.get("KIMCHI_DK_DIR")
        os.environ["KIMCHI_DK_DIR"] = os.path.join(d, "kit")
        try:
            check("env var respected", default_log() == os.path.join(d, "kit", "state", "events.jsonl"))
        finally:
            if old is None:
                os.environ.pop("KIMCHI_DK_DIR", None)
            else:
                os.environ["KIMCHI_DK_DIR"] = old
        gone = os.environ.pop("KIMCHI_DK_DIR", None)
        try:
            check("default fallback path", default_log().endswith(os.path.join("kimchi", "delegate-kit", "state", "events.jsonl")))
        finally:
            if gone is not None:
                os.environ["KIMCHI_DK_DIR"] = gone
        check("render", "total: 4 (skipped 3 malformed)" in render(s, p, sk))
    if failures:
        print("FAIL: " + ", ".join(failures))
        return 1
    print("ok: events selftest")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dk events", description="Summarise the size-meter events.jsonl.")
    ap.add_argument("--file", help="events.jsonl path (default: the installed kit's state dir)")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="only events on or after this UTC day")
    ap.add_argument("--last", type=int, default=10, metavar="N", help="show the last N events (default 10)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.since:
        try:
            day = datetime.datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            ap.error("--since must be YYYY-MM-DD")
        args.since = day.isoformat()
    if args.last < 0:
        ap.error("--last must be >= 0")
    path = args.file or default_log()
    try:
        events, skipped = load(path, day if args.since else None)
    except OSError as e:
        print("dk events: cannot read %s: %s" % (path, e), file=sys.stderr)
        return 1
    summary = summarise(events, args.last)
    if args.json:
        print(json.dumps(dict(summary, file=path, since=args.since, skipped=skipped), indent=2))
    else:
        print(render(summary, path, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
