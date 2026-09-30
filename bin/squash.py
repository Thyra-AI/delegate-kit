#!/usr/bin/env python3
"""squash.py - fold `[Wn]`-tagged commits into one commit per tag.

Run once, at the very end of a partitioned run, from inside the repo:

    sh ~/.claude/delegate-kit/bin/dk squash --plan schedule.json
    sh ~/.claude/delegate-kit/bin/dk squash --base <sha> [--dry-run]

What it does (see .delegate-kit/plans/partition-0.8.0.md, section 2):
  1. base = --base / base_sha from --plan; if `@{upstream}` exists and is not an
     ancestor of base, base = `@{upstream}`. Nothing already pushed is touched.
  2. Preconditions: clean tracked tree, no rebase/merge/cherry-pick in progress,
     non-empty range, no merge commits in the range. Otherwise it reports and
     changes nothing (exit 2).
  3. Groups base..HEAD by the `^[W<digits>]` subject tag, ordered by first
     appearance. Untagged commits keep their place.
  4. Rebase todo: per group `pick <first>` + `fixup <rest>`; runs
     `git rebase -i <base>` with GIT_SEQUENCE_EDITOR pointing back at this script
     in --write-todo mode.
  5. Conflict -> `git rebase --abort`, names the conflicting commit, exit 1;
     history is unchanged.
  6. Prints before/after `git log --oneline`.

Exit codes: 0 squashed (or nothing to do), 1 rebase failed/conflict (aborted),
2 refused (preconditions / bad input).

Stdlib only, Python >= 3.8. Untracked files are ignored (they do not affect a
rebase).
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

TAG_RE = re.compile(r"^\[(W\d+)\]")
GIT_PREFIX = ["git", "-c", "core.quotepath=false", "--no-pager"]


def out(msg=""):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def err(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def git(args, env=None, check=False):
    """Run git with an arg list; returns CompletedProcess (text, utf-8)."""
    proc = subprocess.run(
        GIT_PREFIX + list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc


def git_out(args):
    return git(args, check=True).stdout.strip()


def rev(ref):
    """Resolve ref to a full commit sha, or None."""
    p = git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def is_ancestor(a, b):
    return git(["merge-base", "--is-ancestor", a, b]).returncode == 0


# --------------------------------------------------------------------------
# --write-todo mode (invoked by git as GIT_SEQUENCE_EDITOR)
# --------------------------------------------------------------------------

def write_todo(plan_path, todo_path):
    with open(plan_path, "r", encoding="utf-8") as fh:
        steps = json.load(fh)["steps"]
    with open(todo_path, "r", encoding="utf-8", errors="replace") as fh:
        existing = fh.read().splitlines()
    # Sanity check: git's todo must list exactly the commits we planned for.
    got = []
    for line in existing:
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("pick", "p"):
            got.append(parts[1])
    want = [s["sha"] for s in steps]
    ok = len(got) == len(want) and all(
        any(w.startswith(g) for w in want) for g in got
    )
    if not ok:
        err("squash: git's rebase todo does not match the planned commits; aborting")
        return 1
    lines = ["%s %s %s" % (s["action"], s["sha"], s["subject"]) for s in steps]
    with open(todo_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def read_plan_base(plan_file):
    with open(plan_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    base = data.get("base_sha") if isinstance(data, dict) else None
    if not base:
        raise ValueError("no top-level base_sha in %s" % plan_file)
    return base


def resolve_base(base_arg):
    """Apply the @{upstream} guard; returns (base_sha, note)."""
    base = rev(base_arg) if base_arg else None
    if base_arg and not base:
        raise ValueError("cannot resolve base %r to a commit" % base_arg)
    upstream = rev("@{upstream}")
    note = ""
    if base is None:
        if upstream is None:
            raise ValueError("no --base/--plan given and no upstream branch to fall back on")
        base, note = upstream, "no base given; using @{upstream}"
    elif upstream is not None and not is_ancestor(upstream, base):
        base, note = upstream, "base is behind/diverged from @{upstream}; using @{upstream}"
    head = rev("HEAD")
    if not is_ancestor(base, head):
        # e.g. upstream diverged from local history: only rewrite what is unpushed
        mb = git(["merge-base", base, head])
        if mb.returncode != 0 or not mb.stdout.strip():
            raise ValueError("base %s is not related to HEAD" % base[:10])
        base = mb.stdout.strip()
        note = (note + "; " if note else "") + "base not an ancestor of HEAD; using merge-base"
    return base, note


def list_commits(base):
    """Commits base..HEAD oldest first as dicts; refuses merges via 'merge' key."""
    fmt = "%H%x1f%P%x1f%s"
    text = git_out(["log", "--reverse", "--format=" + fmt, base + "..HEAD"])
    commits = []
    for line in text.splitlines():
        if not line.strip():
            continue
        sha, parents, subject = (line.split("\x1f", 2) + ["", ""])[:3]
        commits.append(
            {"sha": sha, "subject": subject.rstrip("\r"), "merge": len(parents.split()) > 1}
        )
    return commits


def build_steps(commits):
    """Return todo steps: group by tag, order by first appearance."""
    groups = {}
    order = []
    for c in commits:
        m = TAG_RE.match(c["subject"])
        c["tag"] = m.group(1) if m else None
        if c["tag"]:
            groups.setdefault(c["tag"], []).append(c)
    steps = []
    for c in commits:
        if c["tag"] is None:
            steps.append({"action": "pick", "sha": c["sha"], "subject": c["subject"]})
        elif groups[c["tag"]][0] is c:
            for i, g in enumerate(groups[c["tag"]]):
                steps.append(
                    {
                        "action": "pick" if i == 0 else "fixup",
                        "sha": g["sha"],
                        "subject": g["subject"],
                    }
                )
        # later members of a group were emitted with the first one
    order = [s["sha"] for s in steps]
    unchanged = order == [c["sha"] for c in commits] and all(
        s["action"] == "pick" for s in steps
    )
    return steps, groups, unchanged


def precondition_problem():
    """Return a string describing why we must not rewrite, or None."""
    st = git(["status", "--porcelain", "--untracked-files=no"])
    if st.returncode != 0:
        return "git status failed: %s" % st.stderr.strip()
    if st.stdout.strip():
        return "working tree is not clean (commit or set aside your changes first)"
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        p = git(["rev-parse", "--git-path", name])
        path = p.stdout.strip()
        if p.returncode == 0 and path and os.path.exists(path):
            return "a rebase/merge/cherry-pick/revert is in progress (%s)" % name
    return None


def oneline(base):
    return git(["log", "--oneline", base + "..HEAD"]).stdout.rstrip()


# --------------------------------------------------------------------------
# main flow
# --------------------------------------------------------------------------

def run(args):
    if git(["rev-parse", "--git-dir"]).returncode != 0:
        err("squash: not inside a git repository")
        return 2

    base_arg = args.base
    try:
        if not base_arg and args.plan:
            base_arg = read_plan_base(args.plan)
        base, note = resolve_base(base_arg)
    except (ValueError, OSError) as e:
        err("squash: %s" % e)
        return 2
    if note:
        out("note: %s" % note)

    commits = list_commits(base)
    if not commits:
        out("squash: nothing to do (no commits in %s..HEAD)" % base[:10])
        return 0
    if any(c["merge"] for c in commits):
        err("squash: refusing - range %s..HEAD contains merge commits" % base[:10])
        return 2

    steps, groups, unchanged = build_steps(commits)
    problem = precondition_problem()

    if args.dry_run:
        out("# base %s (%d commits)" % (base[:10], len(commits)))
        for s in steps:
            out("%s %s %s" % (s["action"], s["sha"][:10], s["subject"]))
        if problem:
            out("# NOTE: a real run would refuse: %s" % problem)
        elif unchanged:
            out("# nothing to squash (each tag already a single commit, in order)")
        return 0

    if problem:
        err("squash: refusing - %s" % problem)
        return 2
    if unchanged:
        out("squash: nothing to squash (each tag already a single commit, in order)")
        return 0

    head_before = rev("HEAD")
    tree_before = git_out(["rev-parse", "HEAD^{tree}"])
    out("before (%s..HEAD):" % base[:10])
    out(oneline(base))

    fd, plan_path = tempfile.mkstemp(prefix="dk-squash-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"steps": steps}, fh)
        seq_editor = " ".join(
            shlex.quote(p)
            for p in (
                sys.executable.replace("\\", "/"),
                os.path.abspath(__file__).replace("\\", "/"),
                "--write-todo",
                plan_path.replace("\\", "/"),
            )
        )
        env = dict(os.environ)
        env["GIT_SEQUENCE_EDITOR"] = seq_editor
        env["GIT_EDITOR"] = ":"
        proc = git(
            ["-c", "core.editor=:", "-c", "rebase.autoStash=false", "rebase", "-i", base],
            env=env,
        )
        if proc.returncode != 0:
            return handle_failure(proc, head_before, base)
    finally:
        try:
            os.remove(plan_path)
        except OSError:
            pass

    head_after = rev("HEAD")
    out("after (%s..HEAD):" % base[:10])
    out(oneline(base))
    tree_after = git_out(["rev-parse", "HEAD^{tree}"])
    if tree_after != tree_before:
        err(
            "WARNING: final tree differs from before the squash. The previous tip was %s "
            "(recover with `git reset --hard %s` after inspecting `git diff %s HEAD`)."
            % (head_before[:10], head_before[:10], head_before[:10])
        )
        return 1
    out("squash: %d commits -> %d (tree unchanged)" % (len(commits), len(steps) - sum(
        1 for s in steps if s["action"] == "fixup")))
    return 0


def handle_failure(proc, head_before, base):
    in_progress = any(
        os.path.exists(git(["rev-parse", "--git-path", n]).stdout.strip() or "\0")
        for n in ("rebase-merge", "rebase-apply")
    )
    subject = ""
    files = []
    if in_progress:
        rh = rev("REBASE_HEAD")
        if rh:
            subject = git(["log", "-1", "--format=%h %s", rh]).stdout.strip()
        files = [
            f for f in git(["diff", "--name-only", "--diff-filter=U"]).stdout.splitlines() if f
        ]
        git(["rebase", "--abort"])
    err("squash: rebase failed; aborted, history left unchanged (unsquashed).")
    if subject:
        err("  conflicting commit: %s" % subject)
    if files:
        err("  conflicted files: %s" % ", ".join(files))
    detail = (proc.stderr or proc.stdout).strip()
    if detail and not subject:
        err("  git said: %s" % detail.splitlines()[-1])
    if rev("HEAD") != head_before:
        err("WARNING: HEAD moved (was %s); inspect `git reflog`." % head_before[:10])
    return 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(
        description="Fold [Wn]-tagged commits into one commit per tag (run once, at the end)."
    )
    ap.add_argument("--base", help="base commit; commits after it are grouped")
    ap.add_argument("--plan", help="schedule.json / partition.json holding top-level base_sha")
    ap.add_argument("--dry-run", action="store_true", help="print the rebase todo only")
    ap.add_argument("--write-todo", metavar="PLANFILE", help=argparse.SUPPRESS)
    ap.add_argument("todo", nargs="?", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.write_todo:
        if not args.todo:
            err("squash: --write-todo needs the todo file")
            return 2
        return write_todo(args.write_todo, args.todo)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
