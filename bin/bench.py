#!/usr/bin/env python3
"""bench.py - per-agent cost report from real Claude Code transcripts.

    python bin/bench.py PATH [--json] [--price sonnet=2,10,2.5,0.2 ...]

PATH is one of
  - a subagent transcript  (.../<sessionId>/subagents/agent-<id>.jsonl)
  - a session dir          (.../<sessionId>: the main jsonl plus its subagents/)
  - a main session jsonl   (same as the session dir)
  - a sessionId            (looked up under $CLAUDE_CONFIG_DIR or ~/.claude /projects/*/)

One row per agent: type, description, model, requests, tool calls (+ histogram),
peak context, cumulative input, output, wall clock, files read / edited, cost.

Transcript quirks handled here: one API response is split over several lines
sharing a `message.id` (input usage is identical, `output_tokens` is a
placeholder until the last line, so the max is taken), and tool calls are
counted as distinct `tool_use` ids, not one per message.

Stdlib only, Python >= 3.8.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import sys

# $/MTok: input, output, cache write, cache read. UNVERIFIED defaults.
PRICES = collections.OrderedDict([
    ("sonnet", (2.0, 10.0, 2.50, 0.20)),
    ("opus", (4.0, 20.0, 5.0, 0.20)),
    ("fable", (10.0, 50.0, 12.50, 0.25)),
    ("haiku", (1.0, 5.0, 1.25, 0.10)),
])


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
                g = groups[mid] = {"model": model, "in": 0, "cr": 0, "cc": 0, "out": 0}
            g["model"] = g["model"] or model
            g["in"] = max(g["in"], u.get("input_tokens") or 0)
            g["cr"] = max(g["cr"], u.get("cache_read_input_tokens") or 0)
            g["cc"] = max(g["cc"], u.get("cache_creation_input_tokens") or 0)
            g["out"] = max(g["out"], u.get("output_tokens") or 0)
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
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
        "wall_seconds": wall,
        "files_read": len(read_files),
        "files_edited": len(edit_files),
        "cost": None if (unpriced and cost == 0.0) else cost,
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


def fmt_cost(c, r):
    if c is None:
        return "UNPRICED"
    return "$%.2f%s" % (c, "+UNPRICED" if r["unpriced_requests"] else "")


def main():
    ap = argparse.ArgumentParser(description="Per-agent cost report from Claude Code transcripts.")
    ap.add_argument("path", metavar="PATH", help="subagent .jsonl, session dir / main .jsonl, or sessionId")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
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
           ("requests", "tool_calls", "cumulative_input", "output", "files_read", "files_edited")}
    tot["peak_context"] = max(r["peak_context"] for r in rows)
    tot["cost"] = sum(r["cost"] or 0.0 for r in rows)
    tot["unpriced_agents"] = sum(1 for r in rows if r["cost"] is None or r["unpriced_requests"])

    if args.json:
        out = {"prices_per_mtok": {k: list(v) for k, v in prices.items()},
               "prices_note": "default prices are unverified",
               "agents": rows, "totals": tot}
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
                      "{:,}".format(r["output"]), fmt_wall(r["wall_seconds"]),
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


if __name__ == "__main__":
    main()
