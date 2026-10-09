#!/usr/bin/env python3
"""delegate-kit context meter - self-test. Dev/CI only: compose does NOT install this file.

    python hooks/meter_selftest.py       run the self-test against the sibling meter.py
    python hooks/meter.py --selftest     same thing; meter.py delegates here

Kept out of meter.py so the script that runs as a hook on every tool call stays small.
`selftest(mod)` takes the meter module under test (meter.py passes itself); the module's names are
used unqualified below, and monkeypatching goes through `setattr(mod, ...)` so the module sees it.
"""
import json
import os
import sys


def selftest(mod=None):
    if mod is None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import meter as mod
    globals().update({k: v for k, v in vars(mod).items() if not k.startswith("__")})
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
        if not os.path.isfile(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh]

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
    os.makedirs(os.path.join(tmp, "adir"), exist_ok=True)
    cases = [
        ("edit touched file allowed", ("Edit", {"file_path": a_py}), False),
        ("edit touched file via relative path allowed", ("MultiEdit", {"file_path": "rel/b.py"}), False),
        ("write untouched file denied", ("Write", {"file_path": os.path.join(tmp, "new.py")}), True),
        ("Read denied", ("Read", {"file_path": a_py}), True),
        ("Grep denied", ("Grep", {"pattern": "x"}), True),
        ("serena denied", ("mcp__serena__find_symbol", {"name_path_pattern": "x"}), True),
        ("git status allowed", ("Bash", {"command": "git status"}), False),
        ("git status -sb allowed", ("Bash", {"command": "git status -sb"}), False),
        ("git diff --stat allowed", ("Bash", {"command": "git diff --stat -- src/a.py"}), False),
        ("git diff --name-only allowed", ("Bash", {"command": "git diff --name-only"}), False),
        ("git diff --cached --name-status allowed", ("Bash", {"command": "git diff --cached --name-status"}), False),
        ("git diff --staged --stat allowed", ("Bash", {"command": "git diff --staged --stat"}), False),
        ("git diff rev --stat allowed", ("Bash", {"command": "git diff HEAD~1 --stat"}), False),
        ("git diff full patch denied", ("Bash", {"command": "git diff"}), True),
        ("git diff path-only patch denied", ("Bash", {"command": "git diff -- hooks/meter.py"}), True),
        ("git diff --cached patch denied", ("Bash", {"command": "git diff --cached"}), True),
        ("git diff -p --stat denied", ("Bash", {"command": "git diff -p --stat"}), True),
        ("git diff -U unknown flag denied", ("Bash", {"command": "git diff -U9999 --stat"}), True),
        ("git diff --stat=N denied", ("Bash", {"command": "git diff --stat=500"}), True),
        ("git diff glob denied", ("Bash", {"command": "git diff --stat *"}), True),
        ("git diff ? glob denied", ("Bash", {"command": "git diff --stat -- a?.py"}), True),
        ("git diff [] glob denied", ("Bash", {"command": "git diff --stat -- a[12].py"}), True),
        ("git status glob denied", ("Bash", {"command": "git status *"}), True),
        ("git status unknown flag denied", ("Bash", {"command": "git status --output=x"}), True),
        ("git status -- path allowed", ("Bash", {"command": "git status -- hooks/meter.py"}), False),
        ("git log superscript digit denied", ("Bash", {"command": "git log -² --oneline"}), True),
        ("git log -n superscript digit denied", ("Bash", {"command": "git log -n ²"}), True),
        ("git add directory denied", ("Bash", {"command": "git add adir"}), True),
        ("git add -- directory denied", ("Bash", {"command": "git add -- adir"}), True),
        ("git add file next to directory name allowed", ("Bash", {"command": "git add adir/f.py"}), False),
        ("git commit positional directory denied", ("Bash", {"command": "git commit -m x adir"}), True),
        ("git commit pathspec directory denied", ("Bash", {"command": "git commit -m x -- adir"}), True),
        ("git log -1 allowed", ("Bash", {"command": "git log -1"}), False),
        ("git log --oneline -n 5 allowed", ("Bash", {"command": "git log --oneline -n 5"}), False),
        ("git log -3 --oneline allowed", ("Bash", {"command": "git log -3 --oneline"}), False),
        ("git log -n 6 denied", ("Bash", {"command": "git log --oneline -n 6"}), True),
        ("git log unbounded denied", ("Bash", {"command": "git log"}), True),
        ("git log --oneline unbounded denied", ("Bash", {"command": "git log --oneline"}), True),
        ("git log -p denied", ("Bash", {"command": "git log -1 -p"}), True),
        ("git log --output denied", ("Bash", {"command": "git log -1 --output=x.txt"}), True),
        ("git log path arg denied", ("Bash", {"command": "git log -1 -- a.py"}), True),
        ("git log -n 0 denied", ("Bash", {"command": "git log -n 0"}), True),
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
        ("cd && add && heredoc commit denied", ("Bash", {"command": 'cd /x && git add f && git commit -m "$(cat <<EOF\nmsg\nEOF\n)" -- f'}), True),
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
    d = gate("Read", {"file_path": a_py})["hookSpecificOutput"]["permissionDecisionReason"]
    check("deny reason names the allowed forms exactly", all(x in d for x in (
        "`git status`", "`git diff --stat`", "`--name-only`", "`--name-status`", "`git log -1`",
        "`git add <explicit file paths>`", "`git commit -m '<msg>'`", "separate calls",
        "no `&&`, `cd`, heredoc, `$()`, `-a`, globs, directories", "SIZE-HANDBACK")), d)

    check("deny logged as L3", any(e["level"] == "L3" for e in events()))
    check("--gate ignores non-PreToolUse events", run({"hook_event_name": "PostToolUse", "agent_id": aid,
                                                       "tool_name": "Read"}, "executer", gate=True) is None)

    # --- L3 gate: parse errors deny; state loss falls back to the agent's own transcript
    real_ba = bash_allowed

    def boom(*a, **kw):
        raise ValueError("unparseable")

    setattr(mod, "bash_allowed", boom)
    try:
        check("L3 allowlist exception denies the Bash call", denied(gate("Bash", {"command": "git status"})))
    finally:
        setattr(mod, "bash_allowed", real_ba)
    check("L3 allowlist restored after the exception test", not denied(gate("Bash", {"command": "git status"})))

    def tu(i, tool, fp):
        return {"type": "assistant", "isSidechain": True, "message": {"id": "u" + i, "content": [
            {"type": "tool_use", "id": i, "name": tool, "input": {"file_path": fp}}]}}

    def tr(i, err=False):
        r = {"type": "tool_result", "tool_use_id": i, "content": "ok"}
        if err:
            r["is_error"] = True
        return {"type": "user", "message": {"content": [r]}}

    def gate_tr(lines, tool, ti, cur=None, ctx=160000):
        write(sub, [asst("a", 0, 0, ctx, side=True)] + lines)
        pl = {"hook_event_name": "PreToolUse", "session_id": sid, "agent_id": aid, "transcript_path": tp,
              "cwd": tmp, "tool_name": tool, "tool_input": ti, "tool_use_id": cur}
        return run(pl, "executer", gate=True)

    wipe()  # no state at all: a lost update / unreadable state must not strand an already-edited file
    done = [tu("t1", "Edit", a_py), tr("t1")]
    check("L3 no state: edit of a file changed earlier (transcript) allowed",
          gate_tr(done, "Edit", {"file_path": a_py}, cur="t9") is None)
    check("L3 no state: relative path matches the transcript's absolute path",
          gate_tr([tu("t1", "Write", os.path.join(tmp, "rel", "b.py")), tr("t1")], "Edit",
                  {"file_path": "rel/b.py"}, cur="t9") is None)
    check("L3 no state: file never edited is denied", denied(gate_tr(done, "Edit", {"file_path": os.path.join(tmp, "n.py")}, cur="t9")))
    check("L3 no state: only a Read of the file does not count",
          denied(gate_tr([tu("t1", "Read", a_py), tr("t1")], "Edit", {"file_path": a_py}, cur="t9")))
    check("L3 no state: the current call's own tool_use does not count",
          denied(gate_tr([tu("t9", "Edit", a_py)], "Edit", {"file_path": a_py}, cur="t9")))
    check("L3 no state: an earlier edit without a result (denied) does not count",
          denied(gate_tr([tu("t1", "Edit", a_py)], "Edit", {"file_path": a_py}, cur="t9")))
    check("L3 no state: an earlier edit whose result is an error does not count",
          denied(gate_tr([tu("t1", "Edit", a_py), tr("t1", err=True)], "Edit", {"file_path": a_py}, cur="t9")))
    save_state(aid, {"calls": 3, "touched": [], "l1": 0, "l2": False})
    check("L3 state without the path: transcript fallback allows",
          gate_tr(done, "Edit", {"file_path": a_py}, cur="t9") is None)
    with open(state_path(aid), "w") as fh:
        fh.write("{corrupt")
    check("L3 corrupt state: transcript fallback allows",
          gate_tr(done, "Edit", {"file_path": a_py}, cur="t9") is None)
    check("L3 corrupt state: untouched file still denied",
          denied(gate_tr(done, "Edit", {"file_path": os.path.join(tmp, "n.py")}, cur="t9")))

    # a read that fails once is retried
    wipe()
    save_state(aid, {"calls": 7, "touched": [a_py]})
    real_load, flaky = json.load, [1]

    def flaky_load(fh, *a, **kw):
        if flaky[0]:
            flaky[0] = 0
            raise OSError("transient")
        return real_load(fh, *a, **kw)

    json.load = flaky_load
    try:
        got = load_state(aid)
    finally:
        json.load = real_load
    check("load_state retries a failed read once", got is not None and got.get("calls") == 7, got)
    check("load_state: missing file -> None without error", load_state("nonexistent-agent") is None)

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

    # --- main_nudge: only UserPromptSubmit/PostToolUse/PreToolUse may load or persist state
    wipe()
    mkey = "main-" + msid
    for ev in ("Stop", "SubagentStop", "Notification", "PreCompact", "SessionEnd", "", None, 7):
        before = os.listdir(sd) if os.path.isdir(sd) else []
        pl = mpl([asst("z", 0, 0, 460000)])
        pl["hook_event_name"] = ev
        check("main_nudge: event %r returns None" % (ev,), main_nudge(pl) is None)
        check("main_nudge: event %r writes no state" % (ev,),
              (os.listdir(sd) if os.path.isdir(sd) else []) == before and not os.path.exists(state_path(mkey)))
    check("main_nudge: SessionStart (startup) writes no state",
          main_nudge(mpl([asst("z", 0, 0, 460000)], ev="SessionStart", source="startup")) is None
          and not os.path.exists(state_path(mkey)))
    for ev in ("UserPromptSubmit", "PostToolUse", "PreToolUse"):
        wipe()
        check("main_nudge: %s still nudges and persists" % ev,
              isinstance(main_nudge(mpl([asst("z", 0, 0, 460000)], ev=ev)), str) and os.path.exists(state_path(mkey)))

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
    orphan, young = state_path("o1") + ".999.tmp", state_path("o2") + ".998.tmp"
    for f in (orphan, young):
        with open(f, "w") as fh:
            fh.write("{")
    os.utime(orphan, (time.time() - 8 * 86400,) * 2)
    save_state("fresh-two", {})
    check("orphaned .tmp older than 7 days is pruned, young .tmp kept",
          not os.path.exists(orphan) and os.path.exists(young))
    real_replace, leftovers = os.replace, []

    def deny_replace(a, b):
        leftovers.append(a)
        raise PermissionError("locked")

    os.replace = deny_replace
    try:
        try:
            save_state("locked-one", {})
            raised = False
        except PermissionError:
            raised = True
    finally:
        os.replace = real_replace
    check("save_state: failed replace raises and removes its temp file",
          raised and len(leftovers) == 4 and not os.path.exists(leftovers[0]), leftovers)

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
        r = subprocess.run([sys.executable, "-S", os.path.abspath(mod.__file__)] + args, input=stdin,
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
    sys.exit(selftest())
