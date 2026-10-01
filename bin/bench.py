#!/usr/bin/env python3
"""bench.py - per-agent cost report from real Claude Code transcripts.

    python bin/bench.py PATH [--json] [--plan partition.json] [--price sonnet=2,10,2.5,0.2 ...]

PATH is one of
  - a subagent transcript  (.../<sessionId>/subagents/agent-<id>.jsonl)
  - a session dir          (.../<sessionId>: the main jsonl plus its subagents/)
  - a main session jsonl   (same as the session dir)
  - a sessionId            (looked up under $CLAUDE_CONFIG_DIR or ~/.claude /projects/*/)

One row per agent: type, description, model, requests, tool calls (+ histogram),
peak context, cumulative input, output, wall clock, files read / edited, cost.

--plan partition.json (from `partition.py plan|auto`) adds a per-cluster table: agents are grouped
by the leading `[Wn]` tag of their description (the orchestrator starts every spawned agent's
description with its tag), and each cluster shows estimated vs actual peak, tool calls, cumulative
input, output and cost. Without --plan the output is unchanged.

Transcript quirks handled here: one API response is split over several lines
sharing a `message.id` (input usage is identical, `output_tokens` is a
placeholder until the last line, so the max is taken), and tool calls are
counted as distinct `tool_use` ids, not one per message.

Unfinalized responses: a response normally ends with a line whose usage has the final
`output_tokens` (it also carries `iterations` and a `stop_reason`). When that line was never written
- the response that ends in a handback tool call (`SubagentHandback`), an interrupted run, and in
some Claude Code versions most tool-call responses - every line keeps the streaming placeholder (`output_tokens` 1-7) however long the thinking
and tool input were, so the real figure is not in the file. For those responses output is
estimated as EST_TOK_PER_CHAR x the characters of the thinking/text/tool_use blocks (a lower bound:
thinking text is a summary of the real thinking), and a row where the estimate is >= 10% of its
output is marked `~`. `--json` always carries `output_estimated` and `unfinalized_requests`.

Stdlib only, Python >= 3.8.
"""
import argparse
import collections
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import tempfile

# $/MTok: input, output, cache write, cache read. UNVERIFIED defaults.
PRICES = collections.OrderedDict([
    ("sonnet", (2.0, 10.0, 2.50, 0.20)),
    ("opus", (4.0, 20.0, 5.0, 0.20)),
    ("fable", (10.0, 50.0, 12.50, 0.25)),
    ("haiku", (1.0, 5.0, 1.25, 0.10)),
])


# Output tokens per character of thinking/text/tool_use content. Calibrated on finalized responses
# (median 0.38 for text/tool input, 0.7-0.8 when thinking is present), so this errs low.
EST_TOK_PER_CHAR = 0.4
EST_FLAG_SHARE = 0.10


def price_for(model, prices):
    m = (model or "").lower()
    for key, p in prices.items():
        if key in m:
            return p
    return None


def parse_ts(s):
    try:
        s = s.replace("Z", "+00:00")
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, 1)
        return datetime.datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


def analyze(path, prices):
    """Stream one transcript; return a dict of stats."""
    groups = collections.OrderedDict()  # message.id -> dict
    tool_ids = set()
    hist = collections.Counter()
    read_files, edit_files = set(), set()
    t_first = t_last = None
    anon = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = parse_ts(d.get("timestamp"))
            if ts is not None:
                if t_first is None or ts < t_first:
                    t_first = ts
                if t_last is None or ts > t_last:
                    t_last = ts
            if d.get("type") != "assistant":
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            model = msg.get("model")
            if model == "<synthetic>":
                continue
            mid = msg.get("id") or d.get("requestId")
            if not mid:
                anon += 1
                mid = "anon-%d" % anon
            u = msg.get("usage") or {}
            g = groups.get(mid)
            if g is None:
                g = groups[mid] = {"model": model, "in": 0, "cr": 0, "cc": 0, "out": 0, "final": False, "chars": 0, "seen": set()}
            g["model"] = g["model"] or model
            g["in"] = max(g["in"], u.get("input_tokens") or 0)
            g["cr"] = max(g["cr"], u.get("cache_read_input_tokens") or 0)
            g["cc"] = max(g["cc"], u.get("cache_creation_input_tokens") or 0)
            g["out"] = max(g["out"], u.get("output_tokens") or 0)
            if msg.get("stop_reason") or "iterations" in u:
                g["final"] = True
            occ = collections.Counter()  # per-line occurrence of each block key
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    body = b.get("thinking") or ""
                elif bt == "text":
                    body = b.get("text") or ""
                elif bt == "tool_use":
                    body = json.dumps(b.get("input") or {}, sort_keys=True)
                else:
                    body = None
                # A block repeated on a later line of the same response (a growing
                # snapshot) is counted once. Identical blocks within ONE line are
                # distinct blocks, so the key carries their occurrence index on the
                # line. Only a digest is kept, not the body.
                if body is not None:
                    base = (bt, b.get("id"), hashlib.sha1(body.encode("utf-8", "replace")).digest())
                    key = base + (occ[base],)
                    occ[base] += 1
                    if key not in g["seen"]:
                        g["seen"].add(key)
                        g["chars"] += len(body)
                if bt != "tool_use":
                    continue
                tid = b.get("id")
                if tid in tool_ids:
                    continue
                tool_ids.add(tid)
                name = b.get("name") or "?"
                hist[name] += 1
                inp = b.get("input") or {}
                fp = inp.get("file_path") if isinstance(inp, dict) else None
                if fp:
                    if name == "Read":
                        read_files.add(fp)
                    elif name in ("Edit", "Write"):
                        edit_files.add(fp)

    est_out = unfinal = 0
    for g in groups.values():
        if g["final"]:
            continue
        unfinal += 1
        est = int(g["chars"] * EST_TOK_PER_CHAR)
        if est > g["out"]:
            est_out += est
            g["out"] = est
    models = collections.Counter(g["model"] for g in groups.values() if g["model"])
    cost, unpriced = 0.0, False
    for g in groups.values():
        p = price_for(g["model"], prices)
        if p is None:
            unpriced = True
            continue
        cost += (g["in"] * p[0] + g["out"] * p[1] + g["cc"] * p[2] + g["cr"] * p[3]) / 1e6
    ctx = [g["in"] + g["cr"] + g["cc"] for g in groups.values()]
    wall = (t_last - t_first).total_seconds() if t_first and t_last else 0.0
    return {
        "model": models.most_common(1)[0][0] if models else None,
        "requests": len(groups),
        "tool_calls": len(tool_ids),
        "tools": dict(hist.most_common()),
        "peak_context": max(ctx) if ctx else 0,
        "cumulative_input": sum(ctx),
        "output": sum(g["out"] for g in groups.values()),
        "output_estimated": est_out,
        "unfinalized_requests": unfinal,
        "wall_seconds": wall,
        "files_read": len(read_files),
        "files_edited": len(edit_files),
        "cost": None if (unpriced and cost == 0.0) else cost,
        "content_chars": sum(g["chars"] for g in groups.values()),
        "unpriced_requests": sum(1 for g in groups.values() if price_for(g["model"], prices) is None),
        "start": t_first.isoformat() if t_first else "",
    }


def load_meta(jsonl):
    mp = jsonl[:-len(".jsonl")] + ".meta.json"
    try:
        with open(mp, encoding="utf-8", errors="replace") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def resolve(arg):
    """-> list of (kind, jsonl_path); kind is 'main' or 'agent'."""
    p = arg
    if not os.path.exists(p):
        root = os.path.join(config_dir(), "projects")
        hits = glob.glob(os.path.join(root, "*", arg + ".jsonl"))
        hits += [h for h in glob.glob(os.path.join(root, "*", arg)) if os.path.isdir(h)]
        if not hits:
            sys.exit("bench: cannot find %r (not a path, and no session with that id under %s)" % (arg, root))
        p = hits[0]
    p = os.path.abspath(p)
    if os.path.isfile(p):
        if os.path.basename(os.path.dirname(p)) == "subagents":
            return [("agent", p)]
        sdir = p[:-len(".jsonl")] if p.endswith(".jsonl") else p
        return session_items(p, sdir)
    # directory: <slug>/<sessionId>
    p = p.rstrip("\\/")
    return session_items(p + ".jsonl", p)


def session_items(main, sdir):
    items = []
    if os.path.isfile(main):
        items.append(("main", main))
    items += [("agent", f) for f in sorted(glob.glob(os.path.join(sdir, "subagents", "*.jsonl")))]
    if not items:
        sys.exit("bench: no transcripts found for %s" % sdir)
    return items


def fmt_wall(s):
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def fmt_tools(tools, n=3):
    items = list(tools.items())
    parts = ["%s:%d" % (k.split("__")[-1] if k.startswith("mcp__") else k, v) for k, v in items[:n]]
    rest = sum(v for _, v in items[n:])
    if rest:
        parts.append("other:%d" % rest)
    return " ".join(parts) or "-"


def est_flag(r):
    return r["output_estimated"] > 0 and r["output_estimated"] >= EST_FLAG_SHARE * r["output"]


def fmt_out(r):
    return ("~" if est_flag(r) else "") + "{:,}".format(r["output"])


def fmt_cost(c, r):
    if c is None:
        return "UNPRICED"
    return "$%.2f%s" % (c, "+UNPRICED" if r["unpriced_requests"] else "")


TAG_RE = re.compile(r"^\s*\[([^\]\s]+)\]")


def kfmt(n):
    if n is None:
        return "-"
    n = float(n)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return "%d" % n


def cluster_rows(agents, plan):
    """Group non-main agents by leading [Wn] tag onto the plan's clusters (a tag matches a cluster's
    wus or commit_tag). Clusters nobody ran get agents=0; tags the plan does not know, and agents
    without a tag, get their own trailing rows."""
    tag_cluster = {}
    for c in plan.get("clusters") or []:
        for w in c.get("wus") or []:
            tag_cluster[w] = c["id"]
        t = (c.get("commit_tag") or "").strip("[]")
        if t:
            tag_cluster[t] = c["id"]
    groups = collections.OrderedDict((c["id"], []) for c in plan.get("clusters") or [])
    extra = collections.OrderedDict()
    for r in agents:
        if r["type"] == "main":
            continue
        m = TAG_RE.match(r["description"] or "")
        tag = m.group(1) if m else None
        cid = tag_cluster.get(tag)
        if cid is not None:
            groups[cid].append(r)
        else:
            extra.setdefault("[%s]" % tag if tag else "(untagged)", []).append(r)

    def actual(rs):
        costs = [r["cost"] for r in rs if r["cost"] is not None]
        return {"agents": len(rs), "peak_context": max([r["peak_context"] for r in rs] or [0]),
                "tool_calls": sum(r["tool_calls"] for r in rs),
                "cumulative_input": sum(r["cumulative_input"] for r in rs),
                "output": sum(r["output"] for r in rs),
                "cost": sum(costs) if costs else None}

    out = []
    for c in plan.get("clusters") or []:
        rs = groups[c["id"]]
        row = {"cluster": c["id"], "tag": c.get("commit_tag") or "-", "wus": c.get("wus") or [],
               "est_peak": c.get("est_tokens"), "est_tool_calls": c.get("est_hops"),
               "est_cum_input": c.get("est_cum_input"), "est_output": c.get("est_output"),
               "est_cost": c.get("est_cost")}
        row.update(actual(rs))
        out.append(row)
    for tag, rs in extra.items():
        row = {"cluster": "-", "tag": tag, "wus": [], "est_peak": None, "est_tool_calls": None,
               "est_cum_input": None, "est_output": None, "est_cost": None}
        row.update(actual(rs))
        out.append(row)
    return out


def usd(c):
    return "-" if c is None else "$%.2f" % c


def emit(line):
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def print_clusters(rows):
    hdr = ("cluster", "tag", "agents", "peak est/act", "tools est/act", "cum_input est/act",
           "output est/act", "cost est/act")
    table = [hdr]
    for r in rows:
        ran = r["agents"] > 0

        def pair(est, act, f):
            return "%s/%s" % (f(est), f(act) if ran else "-")

        table.append((r["cluster"], r["tag"], "%d" % r["agents"],
                      pair(r["est_peak"], r["peak_context"], kfmt),
                      pair(r["est_tool_calls"], r["tool_calls"], lambda v: "-" if v is None else "%d" % v),
                      pair(r["est_cum_input"], r["cumulative_input"], kfmt),
                      pair(r["est_output"], r["output"], kfmt),
                      pair(r["est_cost"], r["cost"], usd)))
    widths = [max(len(row[i]) for row in table) for i in range(len(hdr))]
    for n, row in enumerate(table):
        cells = [c.rjust(widths[i]) if i >= 2 else c.ljust(widths[i]) for i, c in enumerate(row)]
        emit("  ".join(cells).rstrip())
        if n == 0:
            emit("  ".join("-" * w for w in widths))


def selftest():
    """Content-size estimate for unfinalized responses: dedup across split lines."""
    def line(mid, blocks):
        return {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"id": mid, "model": "claude-sonnet-x",
                            "usage": {"input_tokens": 1, "output_tokens": 2}, "content": blocks}}
    th = {"type": "thinking", "thinking": "t" * 1000}
    tx = {"type": "text", "text": "x" * 500}
    tu1 = {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"a": 1, "b": "y" * 200}}
    tu1r = {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"b": "y" * 200, "a": 1}}
    one = len(json.dumps(tu1["input"], sort_keys=True))
    cases = [
        ("snapshot repeat counted once", [line("m", [th]), line("m", [th]), line("m", [tx])], 1500),
        ("distinct blocks on split lines", [line("m", [th]), line("m", [tx])], 1500),
        ("identical blocks in one line both count", [line("m", [tx, tx])], 1000),
        ("snapshot of a two-identical-block line", [line("m", [tx, tx]), line("m", [tx, tx])], 1000),
        ("tool input key order ignored", [line("m", [tu1]), line("m", [tu1r])], one),
        ("separate responses not merged", [line("m1", [th]), line("m2", [th])], 2000),
    ]
    failed = 0
    tmp = tempfile.mkdtemp(prefix="bench-selftest-")
    for i, (name, lines, want_chars) in enumerate(cases):
        fp = os.path.join(tmp, "agent-%d.jsonl" % i)
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines))
        got = analyze(fp, PRICES).get("content_chars")
        if got != want_chars:
            failed += 1
            print("FAIL %s: chars %r, want %r" % (name, got, want_chars))
    print("bench selftest: %d cases, %d failed" % (len(cases), failed))
    return 1 if failed else 0


def main():
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(selftest())
    ap = argparse.ArgumentParser(description="Per-agent cost report from Claude Code transcripts.")
    ap.add_argument("path", metavar="PATH", help="subagent .jsonl, session dir / main .jsonl, or sessionId")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--plan", metavar="PLAN_JSON",
                    help="partition.json: add a per-cluster est-vs-actual table, agents grouped by their [Wn] tag")
    ap.add_argument("--price", action="append", default=[], metavar="MODEL=IN,OUT,CW,CR",
                    help="override $/MTok for a model substring, e.g. sonnet=2,10,2.5,0.2 (repeatable)")
    args = ap.parse_args()

    prices = collections.OrderedDict()
    for spec in args.price:
        try:
            k, v = spec.split("=", 1)
            nums = tuple(float(x) for x in v.split(","))
            if len(nums) != 4 or not k.strip():
                raise ValueError
        except ValueError:
            ap.error("bad --price %r (want model=in,out,cache_write,cache_read)" % spec)
        prices[k.strip().lower()] = nums
    for k, v in PRICES.items():
        prices.setdefault(k, v)

    rows = []
    for kind, f in resolve(args.path):
        r = analyze(f, prices)
        meta = load_meta(f) if kind == "agent" else {}
        r["type"] = "main" if kind == "main" else (meta.get("agentType") or "?")
        r["description"] = meta.get("description") or ""
        r["model"] = r["model"] or meta.get("model")
        r["file"] = f
        rows.append(r)
    rows.sort(key=lambda r: (r["type"] != "main", r["start"]))

    tot = {k: sum(r[k] for r in rows) for k in
           ("requests", "tool_calls", "cumulative_input", "output", "files_read", "files_edited", "output_estimated", "unfinalized_requests")}
    tot["peak_context"] = max(r["peak_context"] for r in rows)
    tot["cost"] = sum(r["cost"] or 0.0 for r in rows)
    tot["unpriced_agents"] = sum(1 for r in rows if r["cost"] is None or r["unpriced_requests"])

    crows = None
    if args.plan:
        try:
            with open(args.plan, encoding="utf-8") as fh:
                plan = json.load(fh)
            if not isinstance(plan, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            sys.exit("bench: cannot read --plan %s: %s" % (args.plan, e))
        crows = cluster_rows(rows, plan)

    if args.json:
        out = {"prices_per_mtok": {k: list(v) for k, v in prices.items()},
               "prices_note": "default prices are unverified",
               "agents": rows, "totals": tot}
        if crows is not None:
            out["plan"] = args.plan
            out["clusters"] = crows
        print(json.dumps(out, indent=2))
        return

    print("bench: %s" % args.path)
    print("prices $/MTok in/out/cache-write/cache-read (DEFAULTS UNVERIFIED, override with --price): "
          + "; ".join("%s=%s" % (k, "/".join("%g" % x for x in v)) for k, v in prices.items()))
    hdr = ("type", "description", "model", "req", "tools", "peak_ctx", "cum_input", "output",
           "wall", "rd/ed", "cost", "tool histogram")
    table = [hdr]
    for r in rows:
        desc = r["description"]
        desc = desc if len(desc) <= 32 else desc[:29] + "..."
        table.append((r["type"], desc, r["model"] or "?", "%d" % r["requests"], "%d" % r["tool_calls"],
                      "{:,}".format(r["peak_context"]), "{:,}".format(r["cumulative_input"]),
                      fmt_out(r), fmt_wall(r["wall_seconds"]),
                      "%d/%d" % (r["files_read"], r["files_edited"]), fmt_cost(r["cost"], r),
                      fmt_tools(r["tools"])))
    table.append(("TOTAL", "%d agent(s)" % len(rows), "", "%d" % tot["requests"], "%d" % tot["tool_calls"],
                  "{:,}".format(tot["peak_context"]), "{:,}".format(tot["cumulative_input"]),
                  "{:,}".format(tot["output"]), "", "%d/%d" % (tot["files_read"], tot["files_edited"]),
                  "$%.2f%s" % (tot["cost"], " (partial: UNPRICED)" if tot["unpriced_agents"] else ""), ""))
    widths = [max(len(row[i]) for row in table) for i in range(len(hdr))]
    numeric = {3, 4, 5, 6, 7, 8, 9, 10}
    for n, row in enumerate(table):
        cells = [c.rjust(widths[i]) if i in numeric else c.ljust(widths[i]) for i, c in enumerate(row)]
        line = "  ".join(cells).rstrip()
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))
        if n == 0 or n == len(table) - 2:
            print("  ".join("-" * w for w in widths))
    if any(est_flag(r) for r in rows):
        print()
        print("~ output estimated from content size (>= %d%% of this row): the transcript kept only the "
              "streaming placeholder usage for %d unfinalized response(s) (e.g. a handback call). "
              "A lower bound; cost is computed from it." % (
                  EST_FLAG_SHARE * 100, sum(r["unfinalized_requests"] for r in rows if est_flag(r))))
    if crows is not None:
        print()
        print("clusters by [Wn] tag (plan %s): estimate/actual" % args.plan)
        print_clusters(crows)


if __name__ == "__main__":
    main()
