#!/usr/bin/env python3
"""Regression checks for the composed agent definitions.

Run after editing a template or compose.py:  python bin/check.py

Two properties matter enough to guard:

1. **Composition is deterministic.** Anthropic's prompt cache is content-keyed on
   a prefix, so a definition that differs between two composes turns a cheap cache
   read into a full cache write on every spawn. Nothing in a template may vary per
   run — no timestamps, no uuids, no template placeholders.

2. **The director stays tool-starved.** A fragment that omits `agents:` applies to
   every agent, so an ordinary user-authored integration can hand the director a
   file tool and silently void the one guarantee the feature is sold on. Composed
   with every integration enabled, `director` must still be exactly `Task, Agent`.

3. **No agent is left tool-less.** Measured on Claude Code 2.1.263: `tools: []`
   and `tools: ""` do NOT mean "no tools" — they resolve to every MCP tool in the
   environment, and an absent `tools:` line grants every built-in. A genuinely
   tool-less agent cannot be expressed; the harness refuses to spawn one. So an
   empty or missing grant is always a bug, never a way to say "none".

4. **The partition tooling ships whole.** The roster is eight agents including a
   `partitioner` with a real tools grant. Composing with `--home <tempdir>` (never the
   real ~/.claude) installs byte-equal copies of bin/{partition,bench,squash,events}.py plus a
   `dk` launcher, and that launcher runs. Each script answers `--help`.

5. **The executer size budget is wired.** The composed executer has `maxTurns: 150` and a one-line
   `hooks:` mapping with PreToolUse/PostToolUse/Stop commands whose interpreter and meter.py exist.

6. **The self-tests pass.** `bin/partition.py --selftest`, `bin/bench.py --selftest` and `hooks/meter.py --selftest` pass.

7. **The git guard is wired.** `hooks/git_guard.py --selftest` passes, and
   `hooks/hooks.json` parses and points at a `hooks/git_guard.py` that exists.

8. **The card and the main meter are wired.** `hooks/card.md` is at most 250 tokens (estimated as
   ceil(characters / 3.5), deliberately pessimistic: English prose runs 3.5-4 characters per token and
   there is no tokenizer in the stdlib); `hooks/hooks.json` has SessionStart (`startup|clear|compact`),
   UserPromptSubmit and PostToolUse (`Agent|Task`) entries that run through the installed `dk hook`
   launcher (never the slow `python3 ... || python` form); the launcher's `hook` subcommand runs the
   card end to end from the exact hooks.json command, and is a silent no-op when setup has not run.
   Every new hook command is fail-open: no `exec`, ends in `|| exit 0`, and exits 0 even when the
   installed launcher is stale (a launcher that exits 2, as an older `dk` without `hook` does: a
   UserPromptSubmit hook exiting 2 would erase the user's prompt). With CLAUDE_CONFIG_DIR unset, the
   launcher's `hook` branch puts state next to the launcher, not under the user's home.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "agents"
EXPECTED_AGENTS = 8
SCRIPTS = ("partition", "bench", "squash", "events")
# things whose value would differ between two composes
VOLATILE = re.compile(r"\{\{|\$\{|%\(|\buuid\b|\bdatetime\b|\btime\.time\b|<timestamp>", re.I)

failures = []
skipped = []  # checks that could not run here; a skip is never a pass (see --allow-skip)


def all_integrations():
    """Every fragment on disk, so the splice path is actually exercised."""
    d = ROOT / "integrations"
    return ",".join(sorted(p.stem for p in d.glob("*.md"))) if d.is_dir() else ""


def compose_into(outdir, enable=None):
    cmd = [sys.executable, str(ROOT / "bin" / "compose.py"), "--out", str(outdir)]
    if enable:
        cmd += ["--enable", enable]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        failures.append("compose.py exited %d:\n%s" % (r.returncode, r.stderr.strip()))
    return sorted(Path(outdir).glob("*.md"))


def run_py(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)


def check_roster(composed):
    names = sorted(p.stem for p in composed)
    if len(names) != EXPECTED_AGENTS:
        failures.append("roster has %d agents (%s), expected %d"
                        % (len(names), ", ".join(names), EXPECTED_AGENTS))
    part = [p for p in composed if p.stem == "partitioner"]
    if not part:
        failures.append("partitioner was not composed")
        return
    head = part[0].read_text(encoding="utf-8").split("---", 2)
    m = re.search(r"^tools:(.*)$", head[1] if len(head) > 2 else "", re.M)
    if not m or not [t for t in m.group(1).split(",") if t.strip()]:
        failures.append("partitioner has an empty or missing tools list")


def flow_balanced(text):
    """True when every `[`/`{` in a one-line YAML flow value closes in order, ignoring single-quoted
    scalars (`''` is an escaped quote) and double-quoted scalars (backslash escapes)."""
    stack, i, n = [], 0, len(text)
    pairs = {"]": "[", "}": "{"}
    while i < n:
        c = text[i]
        if c in "'\"":
            j = i + 1
            while j < n:
                if c == "\"" and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    if c == "'" and text[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            else:
                return False  # unterminated quote
            i = j
        elif c in "[{":
            stack.append(c)
        elif c in pairs:
            if not stack or stack.pop() != pairs[c]:
                return False
        i += 1
    return not stack


def sh_unescape(text):
    """Undo compose.sh_dq: a backslash before a backslash, dollar, backtick or double quote is dropped."""
    out, k = [], 0
    while k < len(text):
        if text[k] == "\\" and k + 1 < len(text) and text[k + 1] in '\\$`"':
            k += 1
        out.append(text[k])
        k += 1
    return "".join(out)


def check_executer_budget(home):
    """The composed executer carries maxTurns 150 and the one-line size-meter hooks, and every path
    the hook commands name exists in the throwaway home (or is the interpreter running this)."""
    path = home / "agents" / "executer.md"
    fm = path.read_text(encoding="utf-8").split("---", 2)[1] if path.is_file() else ""
    if not re.search(r"^maxTurns:\s*150\s*$", fm, re.M):
        failures.append("composed executer lacks `maxTurns: 150`")
    lines = [ln for ln in fm.splitlines() if ln.startswith("hooks:")]
    if len(lines) != 1:
        failures.append("composed executer has %d one-line `hooks:` entries, expected 1" % len(lines))
        return []
    line = lines[0]
    value = line[len("hooks:"):].strip()
    if not value.startswith("{") or not value.endswith("}") or not flow_balanced(value):
        failures.append("executer hooks line is not a balanced one-line flow mapping: %s" % line)
    try:
        import yaml  # optional: when PyYAML is around, require the line to really parse
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            doc = yaml.safe_load(value)
            if not (isinstance(doc, dict) and {"PreToolUse", "PostToolUse", "Stop"} <= set(doc)):
                failures.append("executer hooks line parsed but lacks PreToolUse/PostToolUse/Stop: %s" % line)
        except yaml.YAMLError as exc:
            failures.append("executer hooks line is not valid YAML: %s" % exc)
    if "${" in line:
        failures.append("executer hooks line contains `${`: %s" % line)
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        if event + ": [" not in line:
            failures.append("executer hooks line lacks %s" % event)
    for flag in ("--gate", "--cleanup"):
        if line.count(flag) != 1:
            failures.append("executer hooks line should carry %s exactly once" % flag)
    cmds = [c.replace("''", "'") for c in re.findall(r"command: '((?:[^']|'')*)'", line)]
    if len(cmds) != 3:
        failures.append("executer hooks line has %d commands, expected 3" % len(cmds))
    meter = (home / "delegate-kit" / "bin" / "meter.py").as_posix()
    for cmd in cmds:
        m = re.match(r'"((?:[^"\\]|\\.)+)" -S "((?:[^"\\]|\\.)+)" --role executer', cmd)
        if not m:
            failures.append("executer hook command is not `\"<python>\" -S \"<meter>\" --role executer`: %s" % cmd)
            continue
        py, mp = sh_unescape(m.group(1)), sh_unescape(m.group(2))
        if not Path(py).is_file():
            failures.append("executer hook interpreter does not exist: %s" % py)
        if mp != meter or not Path(mp).is_file():
            failures.append("executer hook meter path is wrong or missing: %s (want %s)" % (mp, meter))
        if not cmd.rstrip().endswith("|| exit 0") or re.search(r"\bexec\b", cmd):
            failures.append("executer hook command is not fail-open (no `exec`, must end in `|| exit 0`): %s" % cmd)
    return cmds


def check_executer_fail_open(cmds, sh):
    """Run every rendered executer hook command with the meter script gone, and with the interpreter
    gone: each must exit 0 (exit 2 would block every PreToolUse call / keep a Stop agent running)."""
    with tempfile.TemporaryDirectory() as empty:
        gone = (Path(empty) / "missing" / "meter.py").as_posix()
        for cmd in cmds:
            m = re.match(r'"((?:[^"\\]|\\.)+)" -S "((?:[^"\\]|\\.)+)"', cmd)
            if not m:
                continue  # already reported
            for what, broken in (("meter.py", cmd.replace(m.group(2), gone, 1)),
                                 ("interpreter", cmd.replace(m.group(1), gone + ".exe", 1))):
                r = subprocess.run([sh, "-c", broken], input="{}", capture_output=True, text=True)
                if r.returncode != 0:
                    failures.append("executer hook with missing %s must exit 0 (rc %d): %s"
                                    % (what, r.returncode, broken[-60:]))


CARD_MAX_TOKENS = 250
CHARS_PER_TOKEN = 3.5


def card_tokens(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def hook_commands(event):
    """-> [(matcher or None, command)] for one event of hooks/hooks.json ([] if absent or unparsable)."""
    try:
        doc = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        return [(e.get("matcher"), h.get("command", "")) for e in doc["hooks"].get(event, [])
                for h in e.get("hooks", [])]
    except (OSError, ValueError, AttributeError, KeyError):
        return []


def check_card_and_main_hooks():
    card = ROOT / "hooks" / "card.md"
    if not card.is_file():
        failures.append("hooks/card.md is missing")
    else:
        n = card_tokens(card.read_text(encoding="utf-8").strip())
        if n > CARD_MAX_TOKENS:
            failures.append("hooks/card.md is ~%d tokens (ceil(chars/%s)), limit %d" % (n, CHARS_PER_TOKEN, CARD_MAX_TOKENS))
    for ev, want_matcher, needle in (
        ("SessionStart", "startup|clear|compact", "hook card"),
        ("UserPromptSubmit", None, "hook meter --role main"),
        ("PostToolUse", "Agent|Task", "hook meter --role main"),
    ):
        got = hook_commands(ev)
        if len(got) != 1:
            failures.append("hooks.json %s has %d entries, expected 1" % (ev, len(got)))
            continue
        matcher, cmd = got[0]
        if matcher != want_matcher:
            failures.append("hooks.json %s matcher is %r, expected %r" % (ev, matcher, want_matcher))
        if needle not in cmd or "delegate-kit/bin/dk" not in cmd:
            failures.append("hooks.json %s should run `dk %s`: %s" % (ev, needle, cmd))
        if "python" in cmd:
            failures.append("hooks.json %s must route through the dk launcher, not python directly: %s" % (ev, cmd))
        if "${" in cmd:
            failures.append("hooks.json %s command contains `${`: %s" % (ev, cmd))
        if re.search(r"\bexec\b", cmd) or not cmd.rstrip().endswith("|| exit 0"):
            failures.append("hooks.json %s command is not fail-open (no `exec`, must end in `|| exit 0`): %s" % (ev, cmd))
    pre = hook_commands("PreToolUse")
    if [m for m, _ in pre] != ["Bash|PowerShell"] or "hooks/git_guard.py" not in pre[0][1]:
        failures.append("hooks.json PreToolUse should be exactly the Bash|PowerShell git_guard entry")
    elif not pre[0][1].rstrip().endswith("|| exit 0"):
        failures.append("hooks.json git_guard command is not fail-open (must end in `|| exit 0`): %s" % pre[0][1])


def check_hook_launcher(home, dk):
    """Run the exact hooks.json commands against the throwaway home (and against an empty one)."""
    sh = shutil.which("sh")
    if not sh:
        return  # check_install already recorded the skip
    cmds = {ev: hook_commands(ev) for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse")}
    if not all(cmds.values()):
        return  # already reported
    card_cmd, meter_cmd = cmds["SessionStart"][0][1], cmds["UserPromptSubmit"][0][1]
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "session_id": "chk"})
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(home))

    def run(cmd, env, stdin):
        return subprocess.run([sh, "-c", cmd], input=stdin, capture_output=True, text=True, env=env)

    r = run(card_cmd, env, payload)
    try:
        ctx = json.loads(r.stdout)["hookSpecificOutput"]
        ok = ctx["hookEventName"] == "SessionStart" and ctx["additionalContext"] == (ROOT / "hooks" / "card.md").read_text(encoding="utf-8").strip()
    except (ValueError, KeyError, TypeError):
        ok = False
    if r.returncode != 0 or not ok:
        failures.append("hooks.json SessionStart command did not emit the card (rc %d): %s" % (r.returncode, (r.stdout + r.stderr).strip()[:300]))
    r = run(meter_cmd, env, json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "chk"}))
    if r.returncode != 0 or r.stdout.strip():
        failures.append("hooks.json UserPromptSubmit command should be a silent no-op without a transcript (rc %d): %s"
                        % (r.returncode, (r.stdout + r.stderr).strip()[:300]))
    with tempfile.TemporaryDirectory() as stale:  # a stale launcher (older dk, no `hook`) exits 2: still exit 0
        fake = Path(stale) / "delegate-kit" / "bin" / "dk"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\necho dk: unknown command hook >&2\nexit 2\n", encoding="utf-8", newline="\n")
        for ev, entries in cmds.items():
            r = run(entries[0][1], dict(os.environ, CLAUDE_CONFIG_DIR=stale), payload)
            if r.returncode != 0 or r.stdout:
                failures.append("hooks.json %s command must exit 0 with a stale launcher that exits 2 (rc %d): %s"
                                % (ev, r.returncode, (r.stdout + r.stderr).strip()[:300]))
    with tempfile.TemporaryDirectory() as bare:  # CLAUDE_CONFIG_DIR unset: state lands next to the launcher
        shutil.copytree(home / "delegate-kit", Path(bare) / "delegate-kit")
        (Path(bare) / "delegate-kit" / "config.json").unlink(missing_ok=True)  # defaults: the main meter is on
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env.update(HOME=bare + "-home", USERPROFILE=bare + "-home")
        compact = json.dumps({"hook_event_name": "SessionStart", "source": "compact", "session_id": "d2chk"})
        r = subprocess.run([sh, (Path(bare) / "delegate-kit" / "bin" / "dk").as_posix(), "hook", "meter", "--role", "main"],
                           input=compact, capture_output=True, text=True, env=env)
        if r.returncode != 0 or not (Path(bare) / "delegate-kit" / "state" / "main-d2chk.json").is_file():
            failures.append("dk hook with CLAUDE_CONFIG_DIR unset should keep state beside the launcher (rc %d): %s"
                            % (r.returncode, (r.stdout + r.stderr).strip()[:300]))
        for junk in ("bogus", "card"):  # unknown hook name / hook script crash path: still exit 0
            r = subprocess.run([sh, (Path(bare) / "delegate-kit" / "bin" / "dk").as_posix(), "hook", junk],
                               input="not json", capture_output=True, text=True, env=env)
            if r.returncode != 0:
                failures.append("dk hook %s must exit 0 (rc %d)" % (junk, r.returncode))
    with tempfile.TemporaryDirectory() as empty:  # setup never ran: fail-open, no output, exit 0
        for cmd in (card_cmd, meter_cmd):
            r = run(cmd, dict(os.environ, CLAUDE_CONFIG_DIR=empty), payload)
            if r.returncode != 0 or r.stdout or r.stderr:
                failures.append("hook command is not a silent no-op before setup (rc %d): %s"
                                % (r.returncode, (r.stdout + r.stderr).strip()[:300]))


def check_install():
    """Compose into a throwaway --home and exercise what lands there."""
    with tempfile.TemporaryDirectory() as home:
        r = run_py(ROOT / "bin" / "compose.py", "--home", home, "--enable", "")
        if r.returncode != 0:
            failures.append("compose.py --home exited %d:\n%s" % (r.returncode, r.stderr.strip()))
            return
        dest = Path(home) / "delegate-kit" / "bin"
        for name in SCRIPTS:
            src, got = ROOT / "bin" / (name + ".py"), dest / (name + ".py")
            if not got.is_file():
                failures.append("compose did not install %s" % got.name)
            elif got.read_bytes() != src.read_bytes():
                failures.append("installed %s differs from bin/%s" % (got.name, src.name))
        meter_src, meter_got = ROOT / "hooks" / "meter.py", dest / "meter.py"
        if not meter_got.is_file():
            failures.append("compose did not install meter.py")
        elif meter_got.read_bytes() != meter_src.read_bytes():
            failures.append("installed meter.py differs from hooks/meter.py")
        if (dest / "meter_selftest.py").exists():
            failures.append("compose installed meter_selftest.py (dev/CI only; runtime gets meter.py alone)")
        exec_cmds = check_executer_budget(Path(home))
        for name in ("card.py", "card.md"):
            src, got = ROOT / "hooks" / name, dest / name
            if not got.is_file():
                failures.append("compose did not install %s" % name)
            elif got.read_bytes() != src.read_bytes():
                failures.append("installed %s differs from hooks/%s" % (name, name))
        cfg = Path(home) / "delegate-kit" / "config.json"
        try:
            defaults = json.loads(cfg.read_text(encoding="utf-8"))
            if not (defaults.get("card", {}).get("enabled") is True
                    and defaults["meter"]["main"]["delegate_at"] == 200000
                    and defaults["meter"]["executer"]["stop_tokens"] == 150000):
                failures.append("compose wrote config.json with unexpected defaults")
        except (OSError, ValueError, KeyError) as exc:
            failures.append("compose did not write a valid config.json: %s" % exc)
        else:
            cfg.write_text('{"meter": {"main": {"enabled": false}}}', encoding="utf-8")
            r = run_py(ROOT / "bin" / "compose.py", "--home", home, "--enable", "")
            if r.returncode != 0 or cfg.read_text(encoding="utf-8") != '{"meter": {"main": {"enabled": false}}}':
                failures.append("compose overwrote an existing config.json")
        dk = dest / "dk"
        if not dk.is_file():
            failures.append("compose did not write the dk launcher")
            return
        sh = shutil.which("sh")
        if not sh:
            skipped.append("`sh` not on PATH: the dk launcher, hook launcher and executer fail-open run checks did not run")
            return
        check_executer_fail_open(exec_cmds, sh)
        r = subprocess.run([sh, dk.as_posix(), "partition", "--help"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failures.append("`sh dk partition --help` exited %d:\n%s"
                            % (r.returncode, (r.stderr or r.stdout).strip()))
        check_hook_launcher(Path(home), dk)


def check_scripts_and_guard():
    for name in SCRIPTS:
        r = run_py(ROOT / "bin" / (name + ".py"), "--help")
        if r.returncode != 0:
            failures.append("bin/%s.py --help exited %d:\n%s"
                            % (name, r.returncode, r.stderr.strip()))
    guard = ROOT / "hooks" / "git_guard.py"
    if not guard.is_file():
        failures.append("hooks/git_guard.py is missing")
    else:
        r = run_py(guard, "--selftest")
        if r.returncode != 0:
            failures.append("git_guard.py --selftest failed:\n%s"
                            % (r.stdout + r.stderr).strip())
    for name in ("partition", "bench"):
        r = run_py(ROOT / "bin" / ("%s.py" % name), "--selftest")
        if r.returncode != 0:
            failures.append("%s.py --selftest failed:\n%s"
                            % (name, (r.stdout + r.stderr).strip()))
    meter = ROOT / "hooks" / "meter.py"
    if not meter.is_file():
        failures.append("hooks/meter.py is missing")
    else:
        r = run_py(meter, "--selftest")
        if r.returncode != 0:
            failures.append("meter.py --selftest failed:\n%s" % (r.stdout + r.stderr).strip())
    card = ROOT / "hooks" / "card.py"
    if not card.is_file():
        failures.append("hooks/card.py is missing")
    else:
        r = run_py(card, "--selftest")
        if r.returncode != 0:
            failures.append("card.py --selftest failed:" + chr(10) + "%s" % (r.stdout + r.stderr).strip())
    manifest = ROOT / "hooks" / "hooks.json"
    try:
        text = manifest.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, ValueError) as exc:
        failures.append("hooks/hooks.json does not parse: %s" % exc)
        return
    if "hooks/git_guard.py" not in text:
        failures.append("hooks/hooks.json does not reference hooks/git_guard.py")


def check_bench_summary():
    """`bench --summary` on synthetic transcripts holding a forked copy (a second session file that
    re-writes the parent's lines, plus its own new response) and a compaction boundary: the copy must
    not double-count requests, tool calls or cost, and --since / --project must narrow the result."""
    def asst(mid, ts, tokens_in, out, tools=(), side=False):
        rec = {"type": "assistant", "timestamp": ts, "isSidechain": side,
               "message": {"id": mid, "model": "claude-sonnet-x", "stop_reason": "end_turn",
                           "usage": {"input_tokens": tokens_in, "output_tokens": out},
                           "content": [{"type": "tool_use", "id": t, "name": n, "input": {"k": t}}
                                       for t, n in tools]}}
        return rec
    boundary = {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-03-02T10:01:30Z",
                "compactMetadata": {"trigger": "auto", "preTokens": 9, "postTokens": 1}}
    m1 = asst("m1", "2026-03-02T10:01:00Z", 1000, 50, [("t1", "Agent"), ("t2", "Bash")])
    parent = [m1, m1, boundary,  # a response written twice (split line)
              asst("m3", "2026-03-02T10:02:00Z", 3000, 20, [("t3", "Read")]),
              asst("s9", "2026-03-02T10:02:30Z", 9999, 9, [("t9", "Bash")], side=True)]
    child = [asst("a1", "2026-03-02T10:01:10Z", 500, 10, [("b1", "Bash")], side=True),
             asst("a2", "2026-03-02T10:01:20Z", 700, 10, side=True)]
    fork = parent + [asst("m4", "2026-03-02T10:03:00Z", 4000, 5, [("t4", "Bash")])]

    def put(path, recs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        pa, pb = home / "projects" / "proj-a", home / "projects" / "proj-b"
        for sid in ("S1", "S2"):  # S2 is the fork: same history, same subagent run, one new response
            put(pa / (sid + ".jsonl"), parent if sid == "S1" else fork)
            put(pa / sid / "subagents" / "agent-a1.jsonl", child)
            (pa / sid / "subagents" / "agent-a1.meta.json").write_text(
                json.dumps({"agentType": "executer", "description": "[W1] x", "toolUseId": "t1"}), encoding="utf-8")
        put(pb / "B1.jsonl", [asst("ob", "2026-01-01T00:00:00Z", 100, 1, [("o1", "Bash")]),
                              asst("nb", "2026-03-05T00:00:00Z", 100, 1, [("n1", "Bash")])])
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(home))

        def summary(*extra):
            r = subprocess.run([sys.executable, str(ROOT / "bin" / "bench.py"), "--summary", "--json", *extra],
                               capture_output=True, text=True, env=env, timeout=60)
            if r.returncode != 0:
                failures.append("bench --summary %s exited %d:\n%s" % (" ".join(extra), r.returncode, r.stderr.strip()))
                return None
            return json.loads(r.stdout)
        a = summary("--since", "2026-03-01", "--project", "proj-a")
        if a:
            main, ex = a["main"], a["by_type"].get("executer", {})
            want = [("main requests", main["requests"], 3), ("main tool calls", main["tool_calls"], 4),
                    ("executer requests", ex.get("requests"), 2), ("executer tool calls", ex.get("tool_calls"), 1),
                    ("executer runs", ex.get("runs"), 1), ("main peak p50", main["peak_p50"], 3000),
                    ("main peak p90", main["peak_p90"], 3800), ("executer peak", ex.get("peak_p50"), 700),
                    ("total requests", a["total"]["requests"], 5), ("duplicate requests", a["duplicates_removed"]["requests"], 4),
                    ("duplicate tool calls", a["duplicates_removed"]["tool_calls"], 4)]
            for name, got, exp in want:
                if got != exp:
                    failures.append("bench --summary forked-duplicate case: %s = %r, expected %r" % (name, got, exp))
            if abs(a["total"]["cost"] - 0.01935) > 1e-9:
                failures.append("bench --summary cost %r, expected 0.01935 (a forked copy was double-counted)"
                                % a["total"]["cost"])
        both = summary("--since", "2026-03-01")
        if both and both["main"]["requests"] != 4:
            failures.append("bench --summary over all projects: %r main requests, expected 4" % both["main"]["requests"])
        late = summary("--since", "2026-03-04")
        if late and (late["main"]["requests"], late["workers"]["requests"]) != (1, 0):
            failures.append("bench --summary --since 2026-03-04 kept older requests: %r" % (late["main"],))
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "bench.py"), "--summary", "--since", "2026-03-01"],
                           capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0 or "TOTAL" not in r.stdout:
            failures.append("bench --summary text output failed (%d):\n%s" % (r.returncode, (r.stdout + r.stderr).strip()))
    # unfinalized-output estimate flag, mixed aware/naive timestamps, and a malformed transcript file
    def unfin(mid, ts, chars):
        return {"type": "assistant", "timestamp": ts,
                "message": {"id": mid, "model": "claude-sonnet-x", "usage": {"input_tokens": 10, "output_tokens": 2},
                            "content": [{"type": "text", "text": "x" * chars}]}}
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        pc = home / "projects" / "proj-c"
        pc.mkdir(parents=True)
        recs = [unfin("u1", "2026-03-02T10:00:00Z", 1000), unfin("u2", "2026-03-02T10:00:05", 500)]
        (pc / "C1.jsonl").write_text("\n".join(json.dumps(x) for x in recs) + "\n", encoding="utf-8")
        bad = unfin("bad", "2026-03-02T10:00:09Z", 10)
        bad["message"]["usage"]["input_tokens"] = "not-a-number"
        (pc / "C2.jsonl").write_text(json.dumps(bad) + "\n", encoding="utf-8")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(home))
        cmd = [sys.executable, str(ROOT / "bin" / "bench.py"), "--summary", "--since", "2026-03-01"]
        r = subprocess.run(cmd + ["--json"], capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0:
            failures.append("bench --summary with mixed naive/aware timestamps or a bad file exited %d:\n%s"
                            % (r.returncode, r.stderr.strip()))
        else:
            t = json.loads(r.stdout)
            got = (t["total"]["requests"], t["total"]["unfinalized_requests"], t["total"]["output_estimated"],
                   t["total"]["output"], t["files_skipped"])
            if got != (2, 2, 600, 600, 1):
                failures.append("bench --summary estimate/skip case: (requests, unfinalized, estimated, output, "
                                "skipped) = %r, expected (2, 2, 600, 600, 1)" % (got,))
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0 or "~600" not in r.stdout or "unfinalized responses: 2" not in r.stdout \
                or "1 transcript file(s) skipped" not in r.stdout:
            failures.append("bench --summary text lacks the ~ estimate flag / unfinalized total / skipped notice (%d):\n%s"
                            % (r.returncode, (r.stdout + r.stderr).strip()))



def check_serena_fragment():
    """The Serena fragment must carry the exact 1.5.3 parameter names that agents
    habitually get wrong, the activation step, and must not send agents to
    initial_instructions (the fragment carries the essentials)."""
    path = ROOT / "integrations" / "serena.md"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for needle in ("name_path_pattern", "recursive", "activate_project"):
        if needle not in text:
            failures.append("integrations/serena.md lacks `%s`" % needle)
    head, _, body = text.partition("\n---\n")
    for line in body.splitlines():
        low = line.lower()
        if "initial_instructions" in low and not re.search("never|don't|do not", low):
            failures.append("integrations/serena.md tells agents to use initial_instructions: %r"
                            % line.strip()[:80])
    if "initial_instructions" in head:
        failures.append("integrations/serena.md grants the initial_instructions tool")


def check_events():
    """`dk events` summarises a synthetic events.jsonl, directly and through the installed launcher."""
    rows = [{"ts": "2026-10-01T10:00:00Z", "role": "main", "level": "delegate", "context": 210000, "calls": 3},
            {"ts": "2026-10-02T10:00:00Z", "role": "executer", "level": "L1", "context": 1, "calls": 40},
            {"ts": "2026-10-02T11:00:00Z", "role": "executer", "level": "L2", "context": 2, "calls": 60}]

    def verify(r, label):
        try:
            out = json.loads(r.stdout)
        except ValueError:
            failures.append("%s did not print JSON (rc %d): %s" % (label, r.returncode, (r.stdout + r.stderr).strip()[:300]))
            return
        if (r.returncode != 0 or out.get("total") != 3 or len(out.get("last", [])) != 2
                or out.get("by_role") != {"executer": 2, "main": 1}
                or out.get("by_day", {}).get("2026-10-02") != 2 or out.get("skipped") != 1):
            failures.append("%s summarised the synthetic log wrongly: %s" % (label, r.stdout.strip()[:300]))

    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "events.jsonl"
        log.write_text("\n".join(json.dumps(x) for x in rows) + "\ngarbage\n", encoding="utf-8")
        verify(run_py(ROOT / "bin" / "events.py", "--file", log, "--json", "--last", "2"), "events.py --json")
        r = run_py(ROOT / "bin" / "events.py", "--file", log, "--json", "--since", "2026-10-02")
        try:
            since_total = json.loads(r.stdout).get("total")
        except ValueError:
            since_total = None
        if r.returncode != 0 or since_total != 2:
            failures.append("events.py --since did not filter by day: %s" % (r.stdout + r.stderr).strip()[:300])
        if run_py(ROOT / "bin" / "events.py", "--since", "yesterday").returncode != 2:
            failures.append("events.py accepted a malformed --since")
        r = run_py(ROOT / "bin" / "events.py", "--file", log, "--json", "--since", "2026-10-2")
        try:
            unpadded = (json.loads(r.stdout).get("total"), json.loads(r.stdout).get("since"))
        except ValueError:
            unpadded = None
        if r.returncode != 0 or unpadded != (2, "2026-10-02"):
            failures.append("events.py --since 2026-10-2 was not normalised/compared as a date: %s"
                            % (r.stdout + r.stderr).strip()[:300])
        r = run_py(ROOT / "bin" / "events.py", "--file", d)
        if r.returncode == 0 or "cannot read" not in r.stderr:
            failures.append("events.py reported a directory --file as a clean read (rc %d): %s"
                            % (r.returncode, (r.stdout + r.stderr).strip()[:200]))
        with tempfile.TemporaryDirectory() as cfg:
            state = Path(cfg) / "delegate-kit" / "state"
            state.mkdir(parents=True)
            (state / "events.jsonl").write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
            env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
            r = subprocess.run([sys.executable, str(ROOT / "bin" / "events.py"), "--json"],
                               capture_output=True, text=True, env=env)
            try:
                from_cfg = json.loads(r.stdout).get("total")
            except ValueError:
                from_cfg = None
            if r.returncode != 0 or from_cfg != 3:
                failures.append("events.py run from the repo bin/ ignored $CLAUDE_CONFIG_DIR/delegate-kit: %s"
                                % (r.stdout + r.stderr).strip()[:300])
        if run_py(ROOT / "bin" / "events.py", "--selftest").returncode != 0:
            failures.append("events.py --selftest failed")
        sh = shutil.which("sh")
        if not sh:
            return  # already recorded as skipped by check_install
        with tempfile.TemporaryDirectory() as home:
            if run_py(ROOT / "bin" / "compose.py", "--home", home, "--enable", "").returncode != 0:
                return  # compose failure is reported by check_install
            state = Path(home) / "delegate-kit" / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "events.jsonl").write_text(log.read_text(encoding="utf-8"), encoding="utf-8")
            dk = Path(home) / "delegate-kit" / "bin" / "dk"
            r = subprocess.run([sh, dk.as_posix(), "events", "--json", "--last", "2"],
                               capture_output=True, text=True)
            verify(r, "`sh dk events --json`")


def main():
    enable = all_integrations()
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        first = compose_into(a, enable)
        second = compose_into(b, enable)

        if [p.name for p in first] != [p.name for p in second]:
            failures.append("compose produced a different set of files on two runs")
        else:
            for pa, pb in zip(first, second):
                if pa.read_bytes() != pb.read_bytes():
                    failures.append(
                        "%s is not byte-identical across two composes — the cache "
                        "prefix would change on every spawn" % pa.name
                    )

        check_roster(first)

        for path in first:
            head = path.read_text(encoding="utf-8").split("---", 2)
            fm = head[1] if len(head) > 2 else ""
            m = re.search(r"^tools:(.*)$", fm, re.M)
            if not m:
                failures.append(
                    "%s has no `tools:` line, so it inherits every built-in tool "
                    "(~29k tokens of schema per spawn)" % path.name
                )
                continue
            value = m.group(1).strip()
            if value in ("", "[]", '""', "''", "none", "None"):
                failures.append(
                    "%s declares `tools: %s`, which grants every MCP tool in the "
                    "environment rather than none" % (path.name, value or "<empty>")
                )

        starved = {"director": ["Agent", "Task"], "thinker": ["Skill"],
                   "super-thinker": ["Skill"]}
        for path in first:
            if path.stem not in starved:
                continue
            head = path.read_text(encoding="utf-8").split("---", 2)
            if len(head) <= 2:
                failures.append("%s has no parseable frontmatter" % path.name)
                continue
            m = re.search(r"^tools:(.*)$", head[1], re.M)
            got = [t.strip() for t in (m.group(1) if m else "").split(",") if t.strip()]
            if sorted(got) != sorted(starved[path.stem]):
                failures.append(
                    "%s composed with tools %r, expected %r -- a fragment has "
                    "widened an agent whose value is what it cannot do"
                    % (path.stem, got, starved[path.stem])
                )

    for tpl in sorted(TEMPLATES.glob("*.md")):
        hit = VOLATILE.search(tpl.read_text(encoding="utf-8"))
        if hit:
            failures.append(
                "%s contains %r, which would vary between composes and break the "
                "cache prefix" % (tpl.name, hit.group(0))
            )

    check_serena_fragment()
    check_install()
    check_scripts_and_guard()
    check_bench_summary()
    check_card_and_main_hooks()
    check_events()

    if failures:
        print("FAIL (%d)" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    if skipped:
        for note in skipped:
            print("SKIPPED: " + note)
        if "--allow-skip" not in sys.argv[1:]:
            print("FAIL: %d check(s) could not run; install `sh` (Git Bash) or pass --allow-skip" % len(skipped))
            return 1
        print("WARNING: passing with skipped checks (--allow-skip); this is NOT a full pass")
    print("ok: composition is deterministic; every agent has a real tools: grant; "
          "partition tooling installs and runs; git guard wired; card and main-meter hooks wired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
