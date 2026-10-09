#!/usr/bin/env python3
"""SessionStart hook: print the delegate-kit card (card.md) as additionalContext. Stdlib only.

Run as `<python> -S card.py` through `dk hook card`; reads the hook JSON on stdin. SessionStart
fires only in the main session, never in subagents. FAIL-OPEN: any error exits 0 with no output.

    - config.json `card.enabled: false` turns the card off (the level reset below still runs).
    - source=compact also resets the main context meter's levels (meter.main_nudge does it),
      so one process serves both SessionStart entries.

    card.py --selftest
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(os.path.expanduser("~"), ".claude")


def card_enabled():
    try:
        with open(os.path.join(_config_dir(), "delegate-kit", "config.json"), encoding="utf-8") as fh:
            v = json.load(fh)["card"]["enabled"]
        return v if isinstance(v, bool) else True
    except Exception:
        return True


def card_text():
    with open(os.path.join(HERE, "card.md"), encoding="utf-8") as fh:
        return fh.read().strip()


def reset_meter(payload):
    """On a compaction, hand the SessionStart payload to the main meter (it resets the levels)."""
    if payload.get("source") != "compact":
        return
    try:
        sys.path.insert(0, HERE)
        import meter
        meter.main_nudge(payload)
    except Exception:
        pass


def handle(payload):
    """-> output dict or None."""
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    reset_meter(payload)
    if not card_enabled():
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": card_text()}}


def selftest():
    import subprocess
    import tempfile

    bad = []

    def check(ok, what):
        if not ok:
            bad.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=tmp)
        kit = os.path.join(tmp, "delegate-kit")
        os.makedirs(kit)

        def run(payload, raw=None):
            r = subprocess.run([sys.executable, "-S", os.path.abspath(__file__)],
                               input=(raw if raw is not None else json.dumps(payload)).encode(),
                               capture_output=True, env=env)
            return r.returncode, r.stdout.decode()

        code, out = run({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s"})
        got = json.loads(out)["hookSpecificOutput"] if out else {}
        check(code == 0 and got.get("hookEventName") == "SessionStart"
              and got.get("additionalContext") == card_text(), "startup emits the card")
        check("SIZE-HANDBACK" in got.get("additionalContext", ""), "card carries the handback rule")
        check(run({"hook_event_name": "SessionStart", "agent_id": "a1"}) == (0, ""), "subagent payload: nothing")
        check(run(None, raw="not json") == (0, ""), "bad input: fail-open")
        check(run(None, raw="") == (0, ""), "empty input: fail-open")
        with open(os.path.join(kit, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"card": {"enabled": False}}, fh)
        check(run({"hook_event_name": "SessionStart", "source": "startup"}) == (0, ""), "card.enabled=false: nothing")
        with open(os.path.join(kit, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        check(run({"hook_event_name": "SessionStart", "source": "resume"})[1] != "", "bad config: card still on")
        # compact: the main meter's levels are reset (state file rewritten with zeroed levels)
        shutil_src = os.path.join(HERE, "meter.py")
        if os.path.isfile(shutil_src):
            sd = os.path.join(kit, "state")
            os.makedirs(sd)
            sp = os.path.join(sd, "main-sx.json")
            with open(sp, "w", encoding="utf-8") as fh:
                json.dump({"levels": {"delegate": True, "compact": True, "repeat": 2}, "calls": 9}, fh)
            run({"hook_event_name": "SessionStart", "source": "compact", "session_id": "sx"})
            with open(sp, encoding="utf-8") as fh:
                st = json.load(fh)
            check(st.get("levels") == {"delegate": False, "compact": False, "repeat": 0}, "compact resets meter levels")
    print("card selftest: %d failed" % len(bad))
    for b in bad:
        print("FAIL: " + b)
    return 1 if bad else 0


def main(argv):
    if "--selftest" in argv:
        return selftest()
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        out = handle(json.loads(raw) if raw.strip() else None)
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
