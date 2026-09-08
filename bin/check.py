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
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "agents"
# things whose value would differ between two composes
VOLATILE = re.compile(r"\{\{|\$\{|%\(|\buuid\b|\bdatetime\b|\btime\.time\b|<timestamp>", re.I)

failures = []


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

        for path in first:
            if path.stem != "director":
                continue
            fm = path.read_text(encoding="utf-8").split("---", 2)[1]
            m = re.search(r"^tools:(.*)$", fm, re.M)
            got = [t.strip() for t in (m.group(1) if m else "").split(",") if t.strip()]
            if sorted(got) != ["Agent", "Task"]:
                failures.append(
                    "director composed with tools %r; it must hold only Task and "
                    "Agent, or its no-file-access guarantee is void" % (got,)
                )

    for tpl in sorted(TEMPLATES.glob("*.md")):
        hit = VOLATILE.search(tpl.read_text(encoding="utf-8"))
        if hit:
            failures.append(
                "%s contains %r, which would vary between composes and break the "
                "cache prefix" % (tpl.name, hit.group(0))
            )

    if failures:
        print("FAIL (%d)" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("ok: composition is deterministic; every agent has a real tools: grant")
    return 0


if __name__ == "__main__":
    sys.exit(main())
