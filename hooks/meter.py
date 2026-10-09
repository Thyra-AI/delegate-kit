#!/usr/bin/env python3
"""delegate-kit context meter - hook script. Stdlib only; run as `<python> -S meter.py ...`.

Reads one hook JSON object on stdin, measures the agent's context from its transcript
(last non-synthetic assistant message: input + cache_creation + cache_read tokens, tail read),
and emits a once-per-crossing nudge (additionalContext) or, past the executer stop size, a
PreToolUse allowlist deny. FAIL-OPEN: any internal error exits 0 with no output.

    meter.py --role main                 UserPromptSubmit | PostToolUse | PreToolUse | SessionStart
    meter.py --role executer             PostToolUse (counts calls, records touched files, L1/L2)
    meter.py --role executer --gate      PreToolUse (L3 allowlist past stop_tokens)
    meter.py [--role executer] --cleanup SubagentStop (deletes that agent's state file)
    meter.py --selftest

Importable: main_nudge(payload) -> str | None   (main-agent ladder; never raises).

State  <config>/delegate-kit/state/<agent_id | main-<session_id>>.json  (atomic writes, pruned after 7 days)
Events <config>/delegate-kit/state/events.jsonl  (one line per nudge / deny)
Config <config>/delegate-kit/config.json         (defaults when missing or invalid; every flag is a kill switch)
<config> is $CLAUDE_CONFIG_DIR or ~/.claude.
"""
import json
import os
import re
import sys
import time

TAIL = 128 * 1024
TAIL_MAX = 4 * 1024 * 1024
PRUNE_DAYS = 7

DEFAULTS = {
    "card": {"enabled": True},
    "meter": {
        "main": {"enabled": True, "delegate_at": 200000, "compact_at": 300000, "repeat_every": 150000},
        "executer": {"enabled": True, "commit_every_calls": 40, "wrapup_calls": 60,
                     "wrapup_tokens": 120000, "stop_tokens": 150000},
    },
}

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
DENY_REASON = ("Size budget reached: only single commands are allowed now - `git status`, "
               "`git diff --stat` (or `--name-only` / `--name-status`, optionally `--cached`), `git log -1` "
               "(or `-n <=5`), `git add <explicit file paths>`, `git commit -m '<msg>'` (run add and commit as "
               "separate calls; no `&&`, `cd`, heredoc, `$()`, `-a`, globs, directories). Finish edits to files "
               "you already changed, commit, then return SIZE-HANDBACK.")


# ------------------------------------------------------------------ paths, config, state

def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(os.path.expanduser("~"), ".claude")


def kit_dir():
    return os.path.join(config_dir(), "delegate-kit")


def state_dir():
    return os.path.join(kit_dir(), "state")


def load_config(section):
    """DEFAULTS[meter][section] overlaid with valid values from config.json (per key)."""
    cfg = dict(DEFAULTS["meter"][section])
    try:
        with open(os.path.join(kit_dir(), "config.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        got = raw["meter"][section]
        for k, dv in DEFAULTS["meter"][section].items():
            v = got.get(k)
            if isinstance(dv, bool):
                if isinstance(v, bool):
                    cfg[k] = v
            elif isinstance(v, int) and not isinstance(v, bool) and v > 0:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:120] or "x"


def state_path(key):
    return os.path.join(state_dir(), safe_name(key) + ".json")


def prune_state(d):
    cutoff = time.time() - PRUNE_DAYS * 86400
    for n in os.listdir(d):
        if n.endswith(".json") or n.endswith(".tmp"):  # .tmp: orphans of a crashed save_state
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass


def load_state(key, retries=1):
    """The state dict, or None when missing/unreadable. A read that fails for any reason other than
    'no such file' (Windows scanner, concurrent replace) is retried `retries` times."""
    for attempt in range(retries + 1):
        try:
            with open(state_path(key), encoding="utf-8") as fh:
                st = json.load(fh)
            if isinstance(st, dict):
                return st
            return None
        except FileNotFoundError:
            return None
        except Exception:
            if attempt < retries:
                time.sleep(0.05)
    return None


def _unlink_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def save_state(key, st):
    d = state_dir()
    fresh = not os.path.exists(state_path(key))
    os.makedirs(d, exist_ok=True)
    if fresh:
        prune_state(d)
    p = state_path(key)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    for attempt in range(4):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 3:
                _unlink_quiet(tmp)
                raise
            time.sleep(0.05)
        except BaseException:
            _unlink_quiet(tmp)
            raise


def log_event(payload, role, level, context, calls):
    try:
        os.makedirs(state_dir(), exist_ok=True)
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
                "role": role, "level": level, "context": context, "calls": calls}
        with open(os.path.join(state_dir(), "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ context from transcript

def _usage_tokens(u):
    return ((u.get("input_tokens") or 0), (u.get("cache_creation_input_tokens") or 0),
            (u.get("cache_read_input_tokens") or 0))


def read_context(path, sidechain=False, tail=TAIL):
    """Current context tokens of the transcript at `path`, or None when unknown.

    Same message model as bin/bench.py: one API response may be split over several lines that
    share `message.id` (input usage identical; the max per field is taken), `<synthetic>`
    messages are skipped. Only the file tail is read; the window widens x4 up to 4 MB when a huge
    tool-result line pushed the last usage out. A compaction boundary / summary newer than the
    last assistant message means the context is unknown (the old usage is stale) -> None.
    """
    sz = os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            n = min(tail, sz)
            f.seek(sz - n)
            lines = f.read(n).split(b"\n")
            if n < sz:
                lines = lines[1:]  # first line is probably cut
            mid, best = None, None
            for ln in reversed(lines):
                is_asst = b'"usage"' in ln and b'"assistant"' in ln
                if not is_asst and b"compact" not in ln.lower():
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                if mid is None and (r.get("subtype") == "compact_boundary" or r.get("isCompactSummary")):
                    return None
                if r.get("type") != "assistant" or (r.get("isSidechain") and not sidechain):
                    continue
                m = r.get("message")
                u = m.get("usage") if isinstance(m, dict) else None
                if not u or m.get("model") == "<synthetic>":
                    continue
                key = m.get("id") or r.get("requestId") or ("anon", id(r))
                if mid is None:
                    if not sum(_usage_tokens(u)):
                        continue
                    mid, best = key, [0, 0, 0]
                elif key != mid:
                    break
                for i, v in enumerate(_usage_tokens(u)):
                    best[i] = max(best[i], v)
            if best:
                return sum(best)
            if n >= sz or tail >= TAIL_MAX:
                return None
            tail = min(tail * 4, TAIL_MAX)


def transcript_for(payload, role):
    """-> (path, sidechain) for the agent this payload belongs to, or (None, False)."""
    tp = payload.get("transcript_path")
    if role == "executer":
        aid, sid = payload.get("agent_id"), payload.get("session_id")
        if not (isinstance(tp, str) and tp and aid and sid):
            return None, True
        return os.path.join(os.path.dirname(tp), str(sid), "subagents", "agent-%s.jsonl" % aid), True
    return (tp if isinstance(tp, str) and tp else None), False


def context_of(payload, role):
    path, side = transcript_for(payload, role)
    if not path or not os.path.isfile(path):
        return None
    try:
        return read_context(path, sidechain=side)
    except Exception:
        return None


# ------------------------------------------------------------------ nudge text

def k(n):
    return "~%dk" % round(n / 1000.0)


def main_text(level, ctx, cfg):
    if level == "delegate":
        return ("delegate-kit context meter: this session's context is %s tokens. Every tool call you make "
                "re-reads that context, which costs 2-3x what a worker agent's call costs. Delegate any chunk "
                "of more than 5 tool calls to a subagent; a single read, edit or test run can stay inline."
                % k(ctx))
    if level == "compact":
        return ("delegate-kit context meter: this session's context is %s tokens (compact threshold %s). "
                "Write a short state summary under .delegate-kit/ (done, in progress, next steps, key file "
                "paths), then offer the user /compact or a fresh session."
                % (k(ctx), k(cfg["compact_at"])))
    return ("delegate-kit context meter: this session's context is %s tokens, still above the compact "
            "threshold. Keep the state summary under .delegate-kit/ current and ask the user to /compact or "
            "start a fresh session." % k(ctx))


def executer_text(level, ctx, calls, cfg):
    where = "this agent is at %s tokens after %d tool calls" % (k(ctx), calls) if ctx else \
        "this agent has made %d tool calls" % calls
    if level == "L1":
        return ("delegate-kit context meter: %s. Commit the parts that are complete and checked now "
                "(explicit paths, the commit tag from your brief), then continue. See 'Size budget' in your "
                "agent definition." % where)
    return ("delegate-kit context meter: %s. Your Size budget (%d calls / %dk) is reached - finish the step in "
            "progress, run the scoped check, commit (explicit paths, the brief's tag), then return a "
            "SIZE-HANDBACK report: done / remaining (ordered, with file:line anchors) / commits / unverified "
            "items, ending with the line 'Orchestrator: spawn a FRESH executer with the remaining list; do "
            "not resume this agent.' See 'Size budget' in your agent definition."
            % (where, cfg["wrapup_calls"], cfg["wrapup_tokens"] // 1000))


# ------------------------------------------------------------------ main-agent ladder

def _main_decide(st, ctx, cfg):
    """Mutates st['levels'] (once per crossing; a level re-arms when context falls back below it)."""
    lv = st.setdefault("levels", {"delegate": False, "compact": False, "repeat": 0})
    if ctx < cfg["delegate_at"]:
        lv["delegate"] = False
    if ctx < cfg["compact_at"]:
        lv["compact"] = False
    rep = (ctx - cfg["compact_at"]) // cfg["repeat_every"] if ctx >= cfg["compact_at"] else 0
    if rep < lv.get("repeat", 0):
        lv["repeat"] = rep
    if ctx >= cfg["compact_at"] and not lv["compact"]:
        lv["compact"] = lv["delegate"] = True
        lv["repeat"] = rep
        return "compact"
    if ctx >= cfg["compact_at"] and rep > lv.get("repeat", 0):
        lv["repeat"] = rep
        return "repeat"
    if ctx >= cfg["delegate_at"] and not lv["delegate"]:
        lv["delegate"] = True
        return "delegate"
    return None


MAIN_NUDGE_EVENTS = ("UserPromptSubmit", "PostToolUse", "PreToolUse")


def main_nudge(payload):
    """Main-agent ladder for one hook payload. Returns the nudge text or None. Never raises."""
    try:
        if not isinstance(payload, dict) or payload.get("agent_id"):
            return None
        cfg = load_config("main")
        if not cfg["enabled"]:
            return None
        sid = payload.get("session_id")
        if not sid:
            return None
        key = "main-%s" % sid
        event = payload.get("hook_event_name")
        if event == "SessionStart":
            if payload.get("source") == "compact":
                save_state(key, {"levels": {"delegate": False, "compact": False, "repeat": 0}, "calls": 0})
            return None
        if event not in MAIN_NUDGE_EVENTS:
            return None  # only events whose nudge can be delivered may load or persist a crossed level
        st = load_state(key) or {"calls": 0}
        if event == "PostToolUse":
            st["calls"] = st.get("calls", 0) + 1
        ctx = context_of(payload, "main")
        text, level = None, None
        if ctx:
            level = _main_decide(st, ctx, cfg)
            if level:
                text = main_text(level, ctx, cfg)
        save_state(key, st)
        if level:
            log_event(payload, "main", level, ctx, st.get("calls", 0))
        return text
    except Exception:
        return None


# ------------------------------------------------------------------ executer ladder + gate

def _norm(p, cwd):
    if cwd and not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normcase(os.path.normpath(p))


def _touched_path(payload):
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return None
    p = ti.get("file_path") or ti.get("notebook_path")
    return _norm(p, payload.get("cwd")) if isinstance(p, str) and p else None


def executer_post(payload):
    """PostToolUse inside an executer -> nudge text or None."""
    if not payload.get("agent_id"):
        return None
    cfg = load_config("executer")
    if not cfg["enabled"]:
        return None
    key = payload["agent_id"]
    st = load_state(key) or {"calls": 0, "touched": [], "l1": 0, "l2": False}
    st["calls"] = st.get("calls", 0) + 1
    if payload.get("tool_name") in EDIT_TOOLS:
        t = _touched_path(payload)
        if t and t not in st.setdefault("touched", []):
            st["touched"].append(t)
    calls = st["calls"]
    ctx = context_of(payload, "executer")
    level = None
    if not st.get("l2") and (calls >= cfg["wrapup_calls"] or (ctx or 0) >= cfg["wrapup_tokens"]):
        level, st["l2"] = "L2", True
    elif not st.get("l2") and calls // cfg["commit_every_calls"] > st.get("l1", 0):
        level = "L1"
    st["l1"] = max(st.get("l1", 0), calls // cfg["commit_every_calls"])
    save_state(key, st)
    if not level:
        return None
    log_event(payload, "executer", level, ctx, calls)
    return executer_text(level, ctx, calls, cfg)


_META = re.compile(r"[;&|<>`(){}\n\r\\#$]")
_ADD_BAD = re.compile(r"^(\.{1,2}/*|:.*|~.*)$|[*?\[]|[/\\]$")
_COMMIT_FLAGS = {"-q", "--quiet", "-s", "--signoff"}
_GLOB = re.compile(r"[*?\[]")
_STATUS_FLAGS = {"-s", "--short", "-b", "--branch", "-sb", "-bs", "--porcelain", "--porcelain=v1",
                 "--porcelain=v2", "--long", "--no-color", "-uno", "-unormal", "--untracked-files=no",
                 "--untracked-files=normal"}
_DIFF_BOUNDED = ("--stat", "--name-only", "--name-status")
_DIFF_FLAGS = set(_DIFF_BOUNDED) | {"--cached", "--staged", "--no-color"}


def split_simple(cmd):
    """Quote-aware split of one simple command; None when it chains, redirects or substitutes."""
    toks, buf, has, i, n = [], [], False, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return None
            buf.append(cmd[i + 1:j])
            has, i = True, j + 1
        elif c == '"':
            j, has = i + 1, True
            while True:
                if j >= n:
                    return None
                ch = cmd[j]
                if ch == "\\" and j + 1 < n and cmd[j + 1] in '"\\':
                    buf.append(cmd[j + 1])
                    j += 2
                elif ch == '"':
                    break
                elif ch == "`" or (ch == "$" and cmd[j + 1:j + 2] in ("(", "{")):
                    return None
                else:
                    buf.append(ch)
                    j += 1
            i = j + 1
        elif c in " \t":
            if has or buf:
                toks.append("".join(buf))
            buf, has, i = [], False, i + 1
        elif _META.match(c):
            return None
        else:
            buf.append(c)
            has, i = True, i + 1
    if has or buf:
        toks.append("".join(buf))
    return toks


_LOG_FORMATS = ("--oneline", "--stat", "--name-only", "--name-status", "--no-color")


def _log_ok(args):
    """`git log -1 [fmt]` or `git log [--oneline] -n <=5` (also `-<n>`, `-n<n>`, `--max-count=<n>`)."""
    n, i = None, 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--max-count") and i + 1 < len(args):
            v, i = args[i + 1], i + 2
        elif a.startswith("--max-count="):
            v, i = a[12:], i + 1
        elif a.startswith("-n") and len(a) > 2:
            v, i = a[2:], i + 1
        elif len(a) > 1 and a[0] == "-" and a[1:].isascii() and a[1:].isdigit():
            v, i = a[1:], i + 1
        elif a in _LOG_FORMATS:
            i += 1
            continue
        else:
            return False
        if not (v.isascii() and v.isdigit()) or not 1 <= int(v) <= 5 or n is not None:
            return False
        n = int(v)
    return n is not None


def _plain_args(args, flags):
    """Every arg is `--`, a listed flag, or a glob-free operand that does not start with `-`."""
    for a in args:
        if a == "--":
            continue
        if _GLOB.search(a) or (a.startswith("-") and a not in flags):
            return False
    return True


def _is_dir(p, cwd):
    return os.path.isdir(os.path.join(cwd, p) if cwd else p)


def bash_allowed(cmd, cwd=None):
    """True only for `git status|diff --stat|log -1|add <explicit file paths>|commit` as ONE simple command.
    May raise on malformed input; the gate treats an exception as a deny."""
    toks = split_simple(cmd) if isinstance(cmd, str) else None
    if not toks or len(toks) < 2 or toks[0] != "git":
        return False
    sub, args = toks[1], toks[2:]
    if sub == "status":
        return _plain_args(args, _STATUS_FLAGS)
    if sub == "diff":
        return _plain_args(args, _DIFF_FLAGS) and any(a in _DIFF_BOUNDED for a in args)
    if sub == "log":
        return _log_ok(args)
    if sub == "add":
        paths = [a for a in args if a != "--"]
        return bool(paths) and not any(a.startswith("-") or _ADD_BAD.search(a) or _is_dir(a, cwd) for a in paths)
    if sub == "commit":
        i, has_msg = 0, False
        while i < len(args):
            a = args[i]
            if a == "--":
                paths = args[i + 1:]
                return has_msg and not any(_ADD_BAD.search(p) or p.startswith("-") or _is_dir(p, cwd)
                                           for p in paths)
            if a in ("-m", "--message"):
                if i + 1 >= len(args):
                    return False
                has_msg, i = True, i + 2
            elif a.startswith("--message=") or (a.startswith("-m") and len(a) > 2 and a[2] != "-"):
                has_msg, i = True, i + 1
            elif a in _COMMIT_FLAGS:
                i += 1
            elif a.startswith("-"):
                return False
            else:
                if _ADD_BAD.search(a) or _is_dir(a, cwd):
                    return False
                i += 1
        return has_msg
    return False


def transcript_edited(payload, target):
    """True when this agent's own transcript shows an earlier, successful Edit/Write/MultiEdit/NotebookEdit
    of `target` (normalised path). The current call (payload tool_use_id) is excluded; tool_use blocks
    without a non-error tool_result (e.g. an edit this gate denied) do not count."""
    path, _ = transcript_for(payload, "executer")
    if not path or not os.path.isfile(path):
        return False
    cwd, cur = payload.get("cwd"), payload.get("tool_use_id")
    cands, ok = set(), set()
    with open(path, "rb") as fh:
        for ln in fh:
            has_use, has_res = b'"tool_use"' in ln, b'"tool_result"' in ln
            if not (has_use or has_res):
                continue
            if has_res and not has_use and not (cands and any(c.encode() in ln for c in cands)):
                continue
            try:
                r = json.loads(ln)
                content = r["message"]["content"]
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS and b.get("id") != cur:
                    ti = b.get("input")
                    p = (ti.get("file_path") or ti.get("notebook_path")) if isinstance(ti, dict) else None
                    if isinstance(p, str) and p and b.get("id") and _norm(p, cwd) == target:
                        cands.add(b["id"])
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in cands and not b.get("is_error"):
                    ok.add(b["tool_use_id"])
    return bool(ok)


def _edit_allowed(payload):
    """An edit tool call is allowed past the stop size only for a file this agent already changed.
    State is the fast path; a missing/unreadable/stale state falls back to the agent's own transcript."""
    t = _touched_path(payload)
    if not t:
        return False
    st = load_state(payload["agent_id"]) or {}
    if t in (st.get("touched") or []):
        return True
    try:
        return transcript_edited(payload, t)
    except Exception:
        return False


def executer_gate(payload):
    """PreToolUse inside an executer -> deny reason or None."""
    if not payload.get("agent_id"):
        return None
    cfg = load_config("executer")
    if not cfg["enabled"]:
        return None
    ctx = context_of(payload, "executer")
    if not ctx or ctx < cfg["stop_tokens"]:
        return None
    tool, ti = payload.get("tool_name"), payload.get("tool_input")
    allowed = False
    if tool in EDIT_TOOLS:
        allowed = _edit_allowed(payload)
    elif tool == "Bash":
        try:
            allowed = isinstance(ti, dict) and bash_allowed(ti.get("command"), payload.get("cwd"))
        except Exception:
            allowed = False  # a command that does not parse is denied; fail-open is for an unevaluable payload
    if allowed:
        return None
    st = load_state(payload["agent_id"]) or {}
    log_event(payload, "executer", "L3", ctx, st.get("calls", 0))
    return DENY_REASON


def cleanup(payload):
    aid = payload.get("agent_id")
    if aid:
        try:
            os.remove(state_path(aid))
        except OSError:
            pass


# ------------------------------------------------------------------ CLI

def hook_output(event, text=None, deny=None):
    inner = {"hookEventName": event}
    if deny:
        inner.update({"permissionDecision": "deny", "permissionDecisionReason": deny})
    else:
        inner["additionalContext"] = text
    return {"hookSpecificOutput": inner}


def run(payload, role, gate=False, do_cleanup=False):
    """-> output dict or None."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    if do_cleanup:
        cleanup(payload)
        return None
    if role == "executer":
        if gate:
            if event != "PreToolUse":
                return None
            reason = executer_gate(payload)
            return hook_output("PreToolUse", deny=reason) if reason else None
        if event == "PostToolUse":
            text = executer_post(payload)
            return hook_output("PostToolUse", text) if text else None
        return None
    if role == "main":
        text = main_nudge(payload)
        if text and event in ("UserPromptSubmit", "PostToolUse", "PreToolUse", "SessionStart"):
            return hook_output(event, text)
    return None


def main(argv):
    if "--selftest" in argv:
        return selftest()
    try:
        role = argv[argv.index("--role") + 1] if "--role" in argv else "main"
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw.strip() else None
        out = run(payload, role, gate="--gate" in argv, do_cleanup="--cleanup" in argv)
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    return 0


# ------------------------------------------------------------------ selftest

def selftest():
    # The cases live in meter_selftest.py (dev/CI only, not installed); delegate to the sibling file.
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(here, "meter_selftest.py")):
        print("meter selftest: meter_selftest.py not found beside meter.py")
        return 1
    sys.path.insert(0, here)
    try:
        import meter_selftest
        return meter_selftest.selftest(sys.modules[__name__])
    except Exception:  # __main__ swallows everything to exit 0 (fail-open); a crashed selftest must fail
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
