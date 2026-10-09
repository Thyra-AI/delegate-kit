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
DENY_REASON = ("Size budget reached: finish edits to files you already changed, commit (mark unverified "
               "if checks didn't run), return SIZE-HANDBACK now.")


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
        if n.endswith(".json"):
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass


def load_state(key):
    try:
        with open(state_path(key), encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return None


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
    os.replace(tmp, p)


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
_DIFF_BAD = ("--output", "--ext-diff", "--open-files-in-pager", "-O")


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


def bash_allowed(cmd):
    """True only for `git status|diff|add <explicit paths>|commit` as ONE simple command."""
    toks = split_simple(cmd) if isinstance(cmd, str) else None
    if not toks or len(toks) < 2 or toks[0] != "git":
        return False
    sub, args = toks[1], toks[2:]
    if sub == "status":
        return True
    if sub == "diff":
        return not any(a == b or a.startswith(b + "=") for a in args for b in _DIFF_BAD)
    if sub == "add":
        paths = [a for a in args if a != "--"]
        return bool(paths) and not any(a.startswith("-") or _ADD_BAD.search(a) for a in paths)
    if sub == "commit":
        i, has_msg = 0, False
        while i < len(args):
            a = args[i]
            if a == "--":
                paths = args[i + 1:]
                return has_msg and not any(_ADD_BAD.search(p) or p.startswith("-") for p in paths)
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
                if _ADD_BAD.search(a):
                    return False
                i += 1
        return has_msg
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
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        st = load_state(payload["agent_id"]) or {}
        t = _touched_path(payload)
        allowed = bool(t) and t in (st.get("touched") or [])
    elif tool == "Bash":
        allowed = isinstance(ti, dict) and bash_allowed(ti.get("command"))
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
    import subprocess
    import tempfile

    failed, total = [0], [0]

    def check(name, ok, detail=""):
        total[0] += 1
        if not ok:
            failed[0] += 1
            print("FAIL %s %s" % (name, detail))

    tmp = tempfile.mkdtemp(prefix="meter-selftest-")
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(tmp, "cfg")
    sd = state_dir()

    def asst(mid, inp=0, cc=0, cr=0, out=1, side=False, model="claude-sonnet-x", final=False):
        u = {"input_tokens": inp, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
             "output_tokens": out}
        m = {"id": mid, "model": model, "usage": u, "content": []}
        if final:
            m["stop_reason"] = "end_turn"
            u["iterations"] = []
        return {"type": "assistant", "isSidechain": side, "message": m}

    def write(path, lines, raw_tail=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines) + "\n" + raw_tail)
        return path

    def wipe():
        for n in os.listdir(sd) if os.path.isdir(sd) else []:
            os.remove(os.path.join(sd, n))

    def events():
        p = os.path.join(sd, "events.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.isfile(p) else []

    # --- context reader, cross-checked against bin/bench.py's transcript model
    bench = None
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "bin"), here):
        if os.path.isfile(os.path.join(cand, "bench.py")):
            sys.path.insert(0, cand)
            try:
                import bench
            except Exception:
                bench = None
            break
    tdir = os.path.join(tmp, "t")
    big = [asst("m1", 5, 1000, 60000), asst("m2", 6, 0, 90000)]
    split = [asst("m1", 5, 1000, 60000), asst("m2", 6, 0, 90000, out=1), asst("m2", 6, 0, 90000, out=1),
             asst("m2", 6, 0, 90000, out=400, final=True)]
    p = write(os.path.join(tdir, "split.jsonl"), split)
    check("split lines share message.id: counted once", read_context(p) == 90006, read_context(p))
    if bench:
        check("matches bench peak on split lines", bench.analyze(p, bench.PRICES)["peak_context"] == read_context(p))
    else:
        check("bench.py importable for the cross-check", False)
    p = write(os.path.join(tdir, "last.jsonl"), [asst("m1", 0, 0, 150000), asst("m2", 0, 0, 40000)])
    check("last message, not the peak", read_context(p) == 40000)
    p = write(os.path.join(tdir, "syn.jsonl"), [asst("m1", 0, 0, 70000), asst("m2", 0, 0, 5, model="<synthetic>")])
    check("synthetic last message skipped", read_context(p) == 70000)
    p = write(os.path.join(tdir, "side.jsonl"), [asst("m1", 0, 0, 30000, side=True)])
    check("sidechain line ignored for main", read_context(p) is None)
    check("sidechain line read for subagent", read_context(p, sidechain=True) == 30000)
    p = write(os.path.join(tdir, "cmp.jsonl"), [asst("m1", 0, 0, 90000),
                                                 {"type": "system", "subtype": "compact_boundary"}])
    check("compact boundary after last usage -> unknown", read_context(p) is None)
    p = write(os.path.join(tdir, "cmp2.jsonl"), [{"type": "system", "subtype": "compact_boundary"},
                                                  asst("m9", 0, 0, 12000)])
    check("usage after boundary wins", read_context(p) == 12000)
    p = write(os.path.join(tdir, "txt.jsonl"), [asst("m1", 0, 0, 33000),
                                                 {"type": "user", "message": {"content": "grep compact_boundary"}}])
    check("the word in prose is not a boundary", read_context(p) == 33000)
    p = write(os.path.join(tdir, "wide.jsonl"), [asst("m1", 0, 0, 55000),
              {"type": "user", "message": {"content": "x" * (700 * 1024)}}])
    check("window widens past a huge tool result", read_context(p) == 55000)
    p = write(os.path.join(tdir, "part.jsonl"), [asst("m1", 0, 0, 44000)], raw_tail='{"type": "assistant", "mess')
    check("partial last line skipped", read_context(p) == 44000)
    p = os.path.join(tdir, "empty.jsonl")
    open(p, "w").close()
    check("empty transcript -> None", read_context(p) is None)

    # --- executer ladder
    sid, aid = "sess1", "agent1"
    tp = os.path.join(tmp, "proj", sid + ".jsonl")
    sub = os.path.join(tmp, "proj", sid, "subagents", "agent-%s.jsonl" % aid)
    check("subagent transcript path", transcript_for({"transcript_path": tp, "session_id": sid, "agent_id": aid},
                                                     "executer") == (sub, True))

    def post(calls_ctx, tool="Read", fp=None, n=1):
        """n PostToolUse events with the subagent transcript at calls_ctx tokens; returns list of outputs."""
        write(sub, [asst("a", 0, 0, calls_ctx, side=True)])
        outs = []
        for _ in range(n):
            pl = {"hook_event_name": "PostToolUse", "session_id": sid, "agent_id": aid, "agent_type": "executer",
                  "transcript_path": tp, "cwd": tmp, "tool_name": tool, "tool_input": {"file_path": fp} if fp else {}}
            outs.append(run(pl, "executer"))
        return outs

    wipe()
    o = post(10000, n=39)
    check("no nudge before 40 calls", not any(o))
    o = post(10000, n=1)
    check("L1 at call 40", o[0] and "Commit the parts that are complete" in o[0]["hookSpecificOutput"]["additionalContext"]
          and o[0]["hookSpecificOutput"]["hookEventName"] == "PostToolUse", o)
    o = post(10000, n=19)
    check("L1 once per crossing", not any(o))
    o = post(10000, n=1)
    txt = o[0]["hookSpecificOutput"]["additionalContext"] if o[0] else ""
    check("L2 at call 60 (calls)", "Size budget (60 calls / 120k) is reached" in txt and "SIZE-HANDBACK" in txt
          and "after 60 tool calls" in txt, txt)
    o = post(10000, n=30)
    check("L2 once, L1 silent after L2", not any(o))
    check("events logged", [e["level"] for e in events()] == ["L1", "L2"] and events()[1]["calls"] == 60
          and events()[1]["role"] == "executer", events())
    wipe()
    o = post(121000, n=1)
    check("L2 at 120k context with few calls", o[0] and "~121k tokens after 1 tool calls" in
          o[0]["hookSpecificOutput"]["additionalContext"], o)
    check("L2 not repeated", not any(post(140000, n=3)))
    wipe()
    st_pl = {"hook_event_name": "PostToolUse", "session_id": sid, "agent_id": "noctx", "agent_type": "executer",
             "transcript_path": os.path.join(tmp, "missing", "x.jsonl"), "tool_name": "Read", "tool_input": {}}
    outs = [run(dict(st_pl), "executer") for _ in range(60)]
    check("missing transcript: calls-based L2 still fires", outs[-1] and "60 tool calls" in
          outs[-1]["hookSpecificOutput"]["additionalContext"] and not any(outs[:-1][:39]), outs[-1])
    check("main-agent payload (no agent_id) ignored by executer role",
          run({"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": "Read"}, "executer") is None)

    # --- L3 gate
    wipe()
    post(160000, tool="Edit", fp=os.path.join(tmp, "a.py"))
    post(160000, tool="Write", fp="rel/b.py")

    def gate(tool, ti, ctx=160000, aid_=aid):
        write(sub, [asst("a", 0, 0, ctx, side=True)])
        pl = {"hook_event_name": "PreToolUse", "session_id": sid, "agent_id": aid_, "transcript_path": tp,
              "cwd": tmp, "tool_name": tool, "tool_input": ti}
        return run(pl, "executer", gate=True)

    def denied(o):
        h = (o or {}).get("hookSpecificOutput", {})
        return h.get("permissionDecision") == "deny" and h.get("permissionDecisionReason") == DENY_REASON

    a_py = os.path.join(tmp, "a.py")
    cases = [
        ("edit touched file allowed", ("Edit", {"file_path": a_py}), False),
        ("edit touched file via relative path allowed", ("MultiEdit", {"file_path": "rel/b.py"}), False),
        ("write untouched file denied", ("Write", {"file_path": os.path.join(tmp, "new.py")}), True),
        ("Read denied", ("Read", {"file_path": a_py}), True),
        ("Grep denied", ("Grep", {"pattern": "x"}), True),
        ("serena denied", ("mcp__serena__find_symbol", {"name_path_pattern": "x"}), True),
        ("git status allowed", ("Bash", {"command": "git status"}), False),
        ("git status -sb allowed", ("Bash", {"command": "git status -sb"}), False),
        ("git diff allowed", ("Bash", {"command": "git diff --stat -- src/a.py"}), False),
        ("git diff --output denied", ("Bash", {"command": "git diff --output=x.txt"}), True),
        ("git add explicit paths allowed", ("Bash", {"command": "git add hooks/meter.py bin/check.py"}), False),
        ("git add -- path allowed", ("Bash", {"command": "git add -- hooks/meter.py"}), False),
        ("git add -A denied", ("Bash", {"command": "git add -A"}), True),
        ("git add . denied", ("Bash", {"command": "git add ."}), True),
        ("git add -u denied", ("Bash", {"command": "git add -u"}), True),
        ("git add :/ denied", ("Bash", {"command": "git add :/"}), True),
        ("git add glob denied", ("Bash", {"command": "git add src/*.py"}), True),
        ("git add dir/ denied", ("Bash", {"command": "git add src/"}), True),
        ("git add no paths denied", ("Bash", {"command": "git add"}), True),
        ("git commit -m allowed", ("Bash", {"command": 'git commit -m "[W15] meter (partial)"'}), False),
        ("git commit with pathspec allowed", ("Bash", {"command": "git commit -m 'x' -- hooks/meter.py"}), False),
        ("git commit multi-line message allowed", ("Bash", {"command": 'git commit -m "line1\n\nline2"'}), False),
        ("git commit without message denied", ("Bash", {"command": "git commit"}), True),
        ("git commit -a denied", ("Bash", {"command": 'git commit -a -m "x"'}), True),
        ("git commit --amend denied", ("Bash", {"command": 'git commit --amend -m "x"'}), True),
        ("git commit --no-verify denied", ("Bash", {"command": 'git commit --no-verify -m "x"'}), True),
        ("git commit subst denied", ("Bash", {"command": 'git commit -m "$(cat f)"'}), True),
        ("chained && denied", ("Bash", {"command": "git add a.py && git commit -m x"}), True),
        ("chained ; denied", ("Bash", {"command": "git status; ls"}), True),
        ("pipe denied", ("Bash", {"command": "git diff | head"}), True),
        ("redirect denied", ("Bash", {"command": "git status > out.txt"}), True),
        ("subshell denied", ("Bash", {"command": "git status $(rm -rf x)"}), True),
        ("newline chain denied", ("Bash", {"command": "git status\nrm -rf x"}), True),
        ("git push denied", ("Bash", {"command": "git push"}), True),
        ("git -C denied", ("Bash", {"command": "git -C /x status"}), True),
        ("non-git bash denied", ("Bash", {"command": "ls"}), True),
        ("pytest denied", ("Bash", {"command": "python -m pytest"}), True),
        ("bash without command denied", ("Bash", {}), True),
    ]
    for name, (tool, ti), want_deny in cases:
        o = gate(tool, ti)
        check("L3 " + name, denied(o) == want_deny, o)
    check("below stop size nothing is gated", gate("Read", {"file_path": "x"}, ctx=100000) is None)
    check("unknown context: not gated", run({"hook_event_name": "PreToolUse", "agent_id": aid, "tool_name": "Read",
                                              "transcript_path": os.path.join(tmp, "nope", "x.jsonl"),
                                              "session_id": sid}, "executer", gate=True) is None)
    check("deny logged as L3", any(e["level"] == "L3" for e in events()))
    check("--gate ignores non-PreToolUse events", run({"hook_event_name": "PostToolUse", "agent_id": aid,
                                                       "tool_name": "Read"}, "executer", gate=True) is None)

    # --- main ladder
    wipe()
    msid = "mainsess"
    mtp = os.path.join(tmp, "proj", msid + ".jsonl")

    def mpl(ctx_lines, ev="UserPromptSubmit", **kw):
        write(mtp, ctx_lines)
        d = {"hook_event_name": ev, "session_id": msid, "transcript_path": mtp, "cwd": tmp}
        d.update(kw)
        return d

    r = run(mpl([asst("a", 0, 0, 150000)]), "main")
    check("main: nothing below 200k", r is None)
    r = run(mpl([asst("a", 0, 0, 230000)]), "main")
    t = r["hookSpecificOutput"]["additionalContext"] if r else ""
    check("main: delegate nudge at 200k", "~230k tokens" in t and "Delegate any chunk" in t and
          r["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit", r)
    check("main: delegate nudge once", run(mpl([asst("a", 0, 0, 240000)]), "main") is None)
    r = run(mpl([asst("a", 0, 0, 310000)], ev="PostToolUse", tool_name="Agent"), "main")
    check("main: compact nudge at 300k", r and "/compact" in r["hookSpecificOutput"]["additionalContext"] and
          ".delegate-kit/" in r["hookSpecificOutput"]["additionalContext"], r)
    check("main: compact nudge once", run(mpl([asst("a", 0, 0, 330000)]), "main") is None)
    r = run(mpl([asst("a", 0, 0, 455000)]), "main")
    check("main: repeat every +150k", r and "still above the compact threshold" in
          r["hookSpecificOutput"]["additionalContext"], r)
    check("main: repeat once per step", run(mpl([asst("a", 0, 0, 500000)]), "main") is None)
    check("main: next repeat at +300k", bool(run(mpl([asst("a", 0, 0, 610000)]), "main")))
    run(mpl([asst("a", 0, 0, 610000)], ev="SessionStart", source="compact"), "main")
    check("main: unknown context right after compaction is silent",
          run(mpl([asst("a", 0, 0, 610000), {"type": "system", "subtype": "compact_boundary"}]), "main") is None)
    check("main: below 200k after compaction is silent", run(mpl([asst("b", 0, 0, 30000)]), "main") is None)
    check("main: levels reset on compact, 200k fires again",
          bool(run(mpl([asst("c", 0, 0, 210000)]), "main")))
    check("main: subagent payload ignored by main role",
          run(mpl([asst("a", 0, 0, 900000)], agent_id="x", ev="PostToolUse"), "main") is None)
    check("main_nudge returns text for the importable path",
          isinstance(main_nudge(mpl([asst("a", 0, 0, 460000)])), str))
    lv = [e["level"] for e in events() if e["role"] == "main"]
    check("main events logged", lv[:3] == ["delegate", "compact", "repeat"] and len(lv) >= 5, lv)
    for junk in (None, 5, "x", [], {}, {"session_id": 5, "transcript_path": 5}, {"session_id": "s", "transcript_path": {}},
                 {"hook_event_name": "UserPromptSubmit", "session_id": "s", "transcript_path": tmp}):
        try:
            ok = main_nudge(junk) is None
        except Exception:
            ok = False
        check("main_nudge never raises on %r" % (junk,), ok)

    # --- cleanup + prune
    wipe()
    post(10000, n=2)
    check("state file written", os.path.isfile(state_path(aid)))
    run({"hook_event_name": "SubagentStop", "agent_id": aid, "session_id": sid}, "executer", do_cleanup=True)
    check("--cleanup deletes the agent state", not os.path.exists(state_path(aid)))
    old = state_path("stale")
    save_state("stale", {})
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    save_state("fresh-agent", {})
    check("state older than 7 days is pruned on new state", not os.path.exists(old) and
          os.path.exists(state_path("fresh-agent")))

    # --- config + kill switches
    def cfgwrite(obj):
        os.makedirs(kit_dir(), exist_ok=True)
        with open(os.path.join(kit_dir(), "config.json"), "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    cfgwrite("{not json")
    check("invalid config -> defaults", load_config("main") == DEFAULTS["meter"]["main"])
    cfgwrite({"meter": {"main": {"delegate_at": "lots", "compact_at": 250000, "enabled": "no"}}})
    c = load_config("main")
    check("bad values fall back per key, good ones apply", c["delegate_at"] == 200000 and c["compact_at"] == 250000
          and c["enabled"] is True, c)
    wipe()
    cfgwrite({"meter": {"main": {"enabled": False}}})
    check("meter.main.enabled=false silences main", run(mpl([asst("a", 0, 0, 400000)]), "main") is None)
    wipe()
    cfgwrite({"meter": {"executer": {"enabled": False}}})
    check("meter.executer.enabled=false: no nudge", not any(post(200000, n=61)))
    check("meter.executer.enabled=false: no gate", gate("Read", {"file_path": "x"}) is None)
    cfgwrite({"meter": {"executer": {"wrapup_calls": 5, "commit_every_calls": 2}}})
    wipe()
    check("thresholds come from config", [bool(x) for x in post(1000, n=6)] ==
          [False, True, False, True, True, False])

    # --- CLI: garbage in => no output, exit 0; real payload round trip
    def cli(args, stdin):
        r = subprocess.run([sys.executable, "-S", os.path.abspath(__file__)] + args, input=stdin,
                           capture_output=True, env=dict(os.environ))
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()

    cfgwrite({})
    for stdin in (b"", b"not json", b"\xff\xfe\x00", b"[1,2]", b"null", b'{"hook_event_name": 5}', b"{" * 5000):
        for args in (["--role", "main"], ["--role", "executer"], ["--role", "executer", "--gate"],
                     ["--role", "executer", "--cleanup"]):
            rc, out = cli(args, stdin)
            check("garbage stdin %r %s -> exit 0, no output" % (stdin[:12], " ".join(args)), rc == 0 and out == "",
                  (rc, out))
    rc, out = cli(["--role"], b"{}")
    check("dangling --role fails open", rc == 0 and out == "")
    wipe()
    write(sub, [asst("a", 0, 0, 125000, side=True)])
    pl = {"hook_event_name": "PostToolUse", "session_id": sid, "agent_id": aid, "agent_type": "executer",
          "transcript_path": tp, "cwd": tmp, "tool_name": "Edit", "tool_input": {"file_path": a_py}}
    rc, out = cli(["--role", "executer"], json.dumps(pl).encode())
    j = json.loads(out) if out else {}
    check("CLI executer PostToolUse JSON shape", rc == 0 and j.get("hookSpecificOutput", {}).get("hookEventName") ==
          "PostToolUse" and "~125k tokens after 1 tool calls" in j["hookSpecificOutput"].get("additionalContext", ""), out)
    write(sub, [asst("a", 0, 0, 155000, side=True)])
    pl.update({"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "x"}})
    rc, out = cli(["--role", "executer", "--gate"], json.dumps(pl).encode())
    j = json.loads(out) if out else {}
    check("CLI gate deny JSON shape", rc == 0 and j.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
          and j["hookSpecificOutput"]["permissionDecisionReason"] == DENY_REASON, out)
    pl.update({"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": a_py}})
    rc, out = cli(["--role", "executer", "--gate"], json.dumps(pl).encode())
    check("CLI gate lets a touched-file edit through", rc == 0 and out == "", out)
    write(mtp, [asst("a", 0, 0, 205000)])
    rc, out = cli(["--role", "main"], json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "cli-main",
                                                  "transcript_path": mtp}).encode())
    j = json.loads(out) if out else {}
    check("CLI main UserPromptSubmit JSON shape", j.get("hookSpecificOutput", {}).get("hookEventName") ==
          "UserPromptSubmit" and "~205k" in j["hookSpecificOutput"].get("additionalContext", ""), out)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("meter selftest: %d cases, %d failed" % (total[0], failed[0]))
    return 1 if failed[0] else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
