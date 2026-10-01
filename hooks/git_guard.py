#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash) - deny destructive git commands to subagents.

Reads the hook JSON on stdin. Acts only when the input carries an `agent_id`
(i.e. the call comes from a subagent); the user's own session is never touched.
On a match it prints a permissionDecision=deny JSON and exits 0. Anything else
(allowed command, parse error, crash) prints nothing and exits 0 = allow.

Pure regex/lexing, stdlib only, no git subprocess. This is a guardrail against
habitual commands, not a sandbox: `python -c "subprocess..."` bypasses it.

    python git_guard.py --selftest      # exits non-zero on any mismatch
"""
import json
import re
import sys

MAX_DEPTH = 4

# Subcommands denied regardless of arguments.
ALWAYS_DENY = {
    "stash": "stash hides other agents' work in the shared tree",
    "restore": "restore discards working-tree/index changes",
    "clean": "clean deletes untracked files",
    "rebase": "history rewrite (only the final squash step may rewrite history)",
    "filter-branch": "history rewrite",
    "filter-repo": "history rewrite",
    "checkout": "checkout switches branches / discards file changes in a shared tree",
    "switch": "switch changes the branch of a shared tree",
}

WRAPPERS = {
    "sudo", "doas", "env", "command", "exec", "time", "nice", "nohup", "xargs",
    "builtin", "then", "do", "else", "elif", "if", "while", "until", "!", "{",
    "$", "timeout", "stdbuf", "setsid",
}
GLOBAL_OPTS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--config-env",
}
# Wrappers that take positional arguments of their own (`timeout 5`, `nice -n 5`,
# `sudo -u root`), so git may sit several tokens after them. Shell keywords and
# pass-through wrappers (then, do, command, time...) are deliberately excluded:
# the token after those is already the command.
SCAN_WRAPPERS = {"timeout", "nice", "stdbuf", "sudo", "doas", "env", "xargs"}
# Commands whose arguments are prose; the forward scan never looks past them.
SCAN_STOP = {"echo", "printf"}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ADD_ALL_PATHSPEC = re.compile(r"^(\./*|:/|\*)$")
SHORT_CLUSTER = re.compile(r"^-[A-Za-z]+$")
HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z0-9_]+)\2")


# ---------------------------------------------------------------- lexing

def strip_heredocs(cmd):
    """Drop heredoc bodies (and their terminator lines) so prose is not scanned."""
    out, pending = [], []
    for line in cmd.split("\n"):
        if pending:
            delim, dash = pending[0]
            probe = line.rstrip("\r")
            if dash:
                probe = probe.lstrip("\t")
            if probe == delim:
                pending.pop(0)
            continue
        out.append(line)
        for m in HEREDOC.finditer(line):
            pending.append((m.group(3), m.group(1) == "-"))
    return "\n".join(out)


def lex(cmd):
    """Quote-aware split into segments (lists of tokens).

    Segments end at && || ; | & newline ( ) and backtick outside quotes.
    Quoted content stays inside its token (scanned again by the caller).
    """
    segs, toks = [], []
    buf, has = [], False
    i, n = 0, len(cmd)

    def flush():
        nonlocal buf, has
        if has or buf:
            toks.append("".join(buf))
        buf, has = [], False

    def end_seg():
        nonlocal toks
        flush()
        if toks:
            segs.append(toks)
        toks = []

    while i < n:
        c = cmd[i]
        if c in " \t\r":
            flush()
        elif c in "\n;|()`":
            end_seg()
        elif c == "&":
            prev = cmd[i - 1] if i else ""
            nxt = cmd[i + 1] if i + 1 < n else ""
            if nxt == "&" or (prev not in "<>" and nxt != ">"):
                end_seg()
            else:
                buf.append(c)
        elif c == "#" and not buf and not has and (i == 0 or cmd[i - 1] in " \t\n;|&("):
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        elif c == "'":
            has = True
            j = cmd.find("'", i + 1)
            if j < 0:
                j = n
            buf.append(cmd[i + 1:j])
            i = j
        elif c == '"':
            has = True
            i += 1
            while i < n and cmd[i] != '"':
                if cmd[i] == "\\" and i + 1 < n and cmd[i + 1] in '"\\$`\n':
                    i += 1
                buf.append(cmd[i])
                i += 1
        elif c == "\\":
            if i + 1 < n and cmd[i + 1] in " \t\"'$\\`\n;&|()<>":
                i += 1
                if cmd[i] != "\n":
                    buf.append(cmd[i])
            else:
                buf.append(c)  # Windows path separator, keep literally
        else:
            buf.append(c)
        i += 1
    end_seg()
    return segs


# ------------------------------------------------------------- git parsing

def basename(tok):
    return re.split(r"[\\/]", tok)[-1].lower()


def git_args_of(tokens):
    """If this segment is a git invocation, return the tokens after `git`."""
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if ENV_ASSIGN.match(t):
            i += 1
        elif basename(t) in WRAPPERS:
            i += 1
            while i < n and tokens[i].startswith("-") and basename(tokens[i]) != "git":
                i += 1
            if basename(t) in SCAN_WRAPPERS:
                # Positional args (`timeout 5`, `nice -n 5`): look ahead for git.
                j = i
                while j < n and basename(tokens[j]) not in ("git", "git.exe"):
                    if basename(tokens[j]) in SCAN_STOP:
                        j = n
                    else:
                        j += 1
                if j < n:
                    i = j
        else:
            break
    if i < n and basename(tokens[i]) in ("git", "git.exe"):
        return tokens[i + 1:]
    return None


def split_global(args):
    """Skip git's global options; return (subcommand, rest)."""
    j, n = 0, len(args)
    while j < n:
        t = args[j]
        if t in GLOBAL_OPTS_WITH_VALUE:
            j += 2
        elif t.startswith("-"):
            j += 1
        else:
            return t, args[j + 1:]
    return None, []


def opt_is(arg, name, minlen):
    """Long option match including git's unambiguous-prefix abbreviations."""
    return arg.startswith("--") and len(arg) >= minlen and name.startswith(arg.split("=", 1)[0])


def flags(rest):
    """Yield (arg, is_flag) honouring a `--` terminator."""
    past = False
    for a in rest:
        if a == "--" and not past:
            past = True
            continue
        yield a, (a.startswith("-") and not past)


def cluster_has(arg, letters):
    return bool(SHORT_CLUSTER.match(arg)) and any(ch in arg[1:] for ch in letters)


def judge(sub, rest):
    """Return a reason string if `git <sub> <rest>` must be denied, else None."""
    if sub in ALWAYS_DENY:
        return ALWAYS_DENY[sub]
    if sub == "reset":
        for a, fl in flags(rest):
            if fl and (opt_is(a, "--hard", 4) or opt_is(a, "--merge", 4)):
                return "reset --hard/--merge discards changes in a shared tree"
    elif sub == "commit":
        for a, fl in flags(rest):
            if fl and opt_is(a, "--amend", 4):
                return "commit --amend rewrites history (make a new commit instead)"
    elif sub == "branch":
        for a, fl in flags(rest):
            if fl and (cluster_has(a, "DfMC") or opt_is(a, "--force", 4)):
                return "forced branch delete/move"
    elif sub == "add":
        for a, fl in flags(rest):
            if fl and (cluster_has(a, "Au") or opt_is(a, "--all", 4) or opt_is(a, "--update", 4)):
                return "add explicit paths only (`git add <path> ...`), never the whole tree"
            if not fl and ADD_ALL_PATHSPEC.match(a):
                return "add explicit paths only (`git add <path> ...`), never the whole tree"
    elif sub == "push":
        for a, fl in flags(rest):
            if fl and (
                cluster_has(a, "fd")
                or a.startswith("--force")
                or opt_is(a, "--force", 4)
                or opt_is(a, "--delete", 5)
                or opt_is(a, "--mirror", 4)
            ):
                return "force/delete/mirror push"
            if not fl and len(a) > 1 and a[0] in "+:":
                return "forced or deleting refspec push"
    return None


def describe(sub, rest):
    text = " ".join(["git", sub] + rest)
    return text if len(text) <= 160 else text[:157] + "..."


def check_command(cmd, depth=0):
    """Return a deny-reason string for `cmd`, or None to allow."""
    if depth > MAX_DEPTH or not cmd:
        return None
    if depth == 0:
        cmd = strip_heredocs(cmd)
    for seg in lex(cmd):
        gargs = git_args_of(seg)
        if gargs is not None:
            sub, rest = split_global(gargs)
            if sub:
                why = judge(sub, rest)
                if why:
                    return (
                        "git_guard: `%s` is blocked for subagents (%s). Do not run it or "
                        "work around it; stop and report back to the caller what you "
                        "needed instead." % (describe(sub, rest), why)
                    )
        for tok in seg:
            if re.search(r"[\s;&|()`<>]", tok):
                why = check_command(tok, depth + 1)
                if why:
                    return why
    return None


def evaluate(payload):
    """Hook payload dict -> deny reason or None."""
    if not isinstance(payload, dict) or not payload.get("agent_id"):
        return None  # main session (or unknown): never interfere
    tool = payload.get("tool_name")
    if tool not in (None, "Bash"):
        return None
    tin = payload.get("tool_input")
    cmd = tin.get("command") if isinstance(tin, dict) else None
    if not isinstance(cmd, str):
        return None
    return check_command(cmd)


# ---------------------------------------------------------------- selftest

ALLOW = [
    "git add a.py",
    "git add ./src/x.py",
    "git add src/a.py src/b.py",
    "git add -- a.py",
    "git add -p a.py",
    "git commit -m 'fix: x'",
    "git commit -m \"don't run git stash here\"",
    "git -C sub commit -m '[W1] x'",
    "git log",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git status --short",
    "git show HEAD",
    "git grep -n foo",
    "git rev-parse HEAD",
    "git push origin main",
    "git push -u origin feature",
    "git reset HEAD a.py",
    "git reset --soft HEAD~1",
    "git branch --list",
    "git branch -d merged",
    "cd x && git status",
    "echo git stash",
    "ls | grep stash",
    "command -v git",
    "timeout 10 git add a.py",
    "timeout 5 echo git stash",
    "nice -n 5 echo git stash",
    "timeout 5 ls",
    "sh ~/.claude/delegate-kit/bin/dk squash --plan schedule.json",
    "python3 bin/check.py 2>&1",
    "git commit -F - <<'EOF'\ngit stash and git reset --hard are banned\nEOF",
    "git commit -m \"$(cat <<'EOF'\n[W2] add thing\n\ngit checkout is denied\nEOF\n)\"",
    "git",
]

DENY = [
    "git stash",
    "git -C . stash",
    "sh -c \"git stash\"",
    "bash -lc 'cd x && git stash pop'",
    "FOO=1 git.exe reset --hard",
    "FOO=1 BAR=2 git stash",
    "git reset --hard HEAD~1",
    "git reset --merge",
    "/usr/bin/git stash",
    "\"C:\\Program Files\\Git\\bin\\git.exe\" stash",
    "git --no-pager -c core.pager=cat stash list",
    "git --git-dir=.git --work-tree=. stash",
    "git add -A",
    "git add --all",
    "git add -u",
    "git add .",
    "git add ./",
    "git add :/",
    "git add '*'",
    "git add -- .",
    "git commit --amend",
    "git commit -m x --amend --no-edit",
    "git checkout main",
    "git checkout -- file.py",
    "git switch -c new",
    "git restore --staged x",
    "git restore x",
    "git clean -fd",
    "git rebase -i HEAD~3",
    "git filter-branch --all",
    "git branch -D old",
    "git branch -M stable",
    "git branch -C x y",
    "git branch --force x HEAD",
    "git push -f",
    "git push --force-with-lease origin main",
    "git push origin --delete old",
    "git push origin +main",
    "git push origin :old",
    "git push --mirror",
    "git status && git stash",
    "echo hi; git stash",
    "true || git checkout main",
    "git log | head; git clean -n",
    "sudo git stash",
    "timeout 5 git stash",
    "nice -n 5 git stash",
    "stdbuf -o0 git reset --hard",
    "sudo -u root git stash",
    "timeout -s KILL 5 git checkout main",
    "(git stash)",
    "echo $(git stash)",
    "if true; then git reset --hard; fi",
]

PAYLOADS = [
    # (payload, expect_deny)
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "git -C . stash"}}, True),
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "git add a.py"}}, False),
    ({"tool_name": "Bash", "tool_input": {"command": "git stash"}}, False),  # main session
    ({"agent_id": "", "tool_name": "Bash", "tool_input": {"command": "git stash"}}, False),
    ({"agent_id": "a1", "tool_name": "Read", "tool_input": {"command": "git stash"}}, False),
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": {}}, False),
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": None}, False),
    ([], False),
]


def selftest():
    bad = 0
    total = 0
    for cmd in ALLOW:
        total += 1
        r = check_command(cmd)
        if r is not None:
            bad += 1
            print("FAIL (expected allow): %r -> %s" % (cmd, r))
    for cmd in DENY:
        total += 1
        if check_command(cmd) is None:
            bad += 1
            print("FAIL (expected deny): %r" % (cmd,))
    for payload, want in PAYLOADS:
        total += 1
        got = evaluate(payload) is not None
        if got != want:
            bad += 1
            print("FAIL (payload, expected %s): %r" % ("deny" if want else "allow", payload))
    print("git_guard selftest: %d cases, %d failed" % (total, bad))
    return 1 if bad else 0


# -------------------------------------------------------------------- main

def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw.strip() else None
        reason = evaluate(payload)
    except Exception:
        return 0  # fail open
    if reason:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
