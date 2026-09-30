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
   real ~/.claude) installs byte-equal copies of bin/{partition,bench,squash}.py plus a
   `dk` launcher, and that launcher runs. Each script answers `--help`.

5. **The git guard is wired.** `hooks/git_guard.py --selftest` passes, and
   `hooks/hooks.json` parses and points at a `hooks/git_guard.py` that exists.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "agents"
EXPECTED_AGENTS = 8
SCRIPTS = ("partition", "bench", "squash")
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
        dk = dest / "dk"
        if not dk.is_file():
            failures.append("compose did not write the dk launcher")
            return
        sh = shutil.which("sh")
        if not sh:
            print("note: `sh` not on PATH; skipping the dk launcher run check")
            return
        r = subprocess.run([sh, dk.as_posix(), "partition", "--help"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failures.append("`sh dk partition --help` exited %d:\n%s"
                            % (r.returncode, (r.stderr or r.stdout).strip()))


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
    manifest = ROOT / "hooks" / "hooks.json"
    try:
        text = manifest.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, ValueError) as exc:
        failures.append("hooks/hooks.json does not parse: %s" % exc)
        return
    if "hooks/git_guard.py" not in text:
        failures.append("hooks/hooks.json does not reference hooks/git_guard.py")


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

    check_install()
    check_scripts_and_guard()

    if failures:
        print("FAIL (%d)" % len(failures))
        for f in failures:
            print("  - " + f)
        return 1
    print("ok: composition is deterministic; every agent has a real tools: grant; "
          "partition tooling installs and runs; git guard wired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
