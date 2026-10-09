#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash|PowerShell) - deny destructive git commands and all pushes to subagents.

Reads the hook JSON on stdin. Acts only when the input carries an `agent_id`
(i.e. the call comes from a subagent); the user's own session is never touched.
On a match it prints a permissionDecision=deny JSON and exits 0. Anything else
(any other command, parse error, crash) prints nothing and exits 0: pass-through,
no decision, so the normal permission flow applies. This hook never grants permission.

Pure regex/lexing, stdlib only, no git subprocess. This is a guardrail against
habitual commands, not a sandbox: `python -c "subprocess..."` bypasses it.

Main-session context meter: when the call has no `agent_id` and nothing was denied, this hook also
asks meter.py (same directory) for a main-agent nudge and, if there is one, emits it as a PreToolUse
pass-through `additionalContext` (no permission decision). The guard decision is computed and
serialized FIRST; the meter runs inside try/except and can never change a deny or raise. Set
`meter.main.enabled: false` in ~/.claude/delegate-kit/config.json to turn it off.

    python git_guard.py --selftest      # exits non-zero on any mismatch
"""
import json
import os
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

# Every push from a subagent is denied; pushes run in the main session.
PUSH_DENY = "push runs in the main session"
PUSH_MESSAGE = (
    "git_guard: pushes run in the main session. Commit your work, then report "
    "`ready to push: <branch> @ <sha>` instead of pushing."
)

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
GIT_NAMES = ("git", "git.exe")
BASH_SHELLS = {n + x for n in ("sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh") for x in ("", ".exe")}
PS_SHELLS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
CMD_SHELLS = {"cmd", "cmd.exe"}
EVALS = {"eval"}
PS_EVALS = {"iex", "invoke-expression"}
SCRIPT_RUNNERS = BASH_SHELLS | PS_SHELLS | CMD_SHELLS | EVALS | PS_EVALS
HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z0-9_]+)\2")
PS_HERESTRING = re.compile(r"@(['\"])[ \t]*\r?\n.*?\r?\n\1@", re.S)
ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
               "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


# ---------------------------------------------------------------- lexing

class Tok(str):
    """A shell word. `subs` holds the `$(...)` / backtick bodies found in its double-quoted parts."""

    subs = ()


def ansi_c(s):
    """Decode the body of a bash `$'...'` string."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = s[i + 1]
        if d in ANSI_SIMPLE:
            out.append(ANSI_SIMPLE[d])
            i += 2
        elif d in "01234567":
            m = re.match(r"[0-7]{1,3}", s[i + 1:])
            out.append(chr(int(m.group(), 8) & 0xFF))
            i += 1 + len(m.group())
        elif d in "xuU" and re.match(r"[0-9a-fA-F]", s[i + 2:i + 3]):
            m = re.match(r"[0-9a-fA-F]{1,%d}" % {"x": 2, "u": 4, "U": 8}[d], s[i + 2:])
            try:
                out.append(chr(int(m.group(), 16)))
            except (ValueError, OverflowError):
                out.append("?")
            i += 2 + len(m.group())
        else:
            out.append("\\" + d)
            i += 2
    return "".join(out)


def substitutions(raw, ps):
    """Bodies of `$(...)` and backtick substitutions inside the raw text of a double-quoted string."""
    esc = "`" if ps else "\\"
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c == esc:
            i += 2
        elif c == "$" and raw[i + 1:i + 2] == "(":
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (raw[j] == "(") - (raw[j] == ")")
                j += 1
            out.append(raw[i + 2:j - 1] if depth == 0 else raw[i + 2:j])
            i = j
        elif c == "`" and not ps:
            j = i + 1
            while j < n and raw[j] != "`":
                j += 2 if raw[j] == "\\" else 1
            out.append(raw[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


def strip_heredocs(cmd, ps=False):
    """Drop heredoc bodies (and their terminator lines) so prose is not scanned.

    PowerShell has here-strings (`@'...'@`) instead; their bodies are dropped the same way.
    Known gap: a body that is itself run as a script (`bash <<EOF`) is not scanned.
    """
    if ps:
        return PS_HERESTRING.sub("", cmd)
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


def lex(cmd, ps=False):
    """Quote-aware split into segments (lists of tokens), unquoted the way the shell would.

    Segments end at && || ; | & newline ( ) and (bash only) backtick outside quotes.
    Words are unquoted like bash does: an unquoted backslash is dropped and `$'...'` is
    ANSI-C decoded. With ps=True (PowerShell) backslash is a literal path separator and
    backtick is the escape character instead.
    A token's `.subs` lists the `$(...)`/backtick bodies of its double-quoted parts; the
    caller scans those. Other quoted content is plain data and is not re-scanned.
    """
    segs, toks = [], []
    buf, has, subs = [], False, []
    i, n = 0, len(cmd)
    seps = "\n;|()" if ps else "\n;|()`"

    def flush():
        nonlocal buf, has, subs
        if has or buf:
            t = Tok("".join(buf))
            t.subs = tuple(subs)
            toks.append(t)
        buf, has, subs = [], False, []

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
        elif c in seps:
            end_seg()
        elif c == "&":
            prev = cmd[i - 1] if i else ""
            nxt = cmd[i + 1] if i + 1 < n else ""
            if nxt == "&" or ((not prev or prev not in "<>") and nxt != ">"):
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
        elif c == "$" and not ps and cmd[i + 1:i + 2] == "'":
            has = True
            j = i + 2
            while j < n and cmd[j] != "'":
                j += 2 if cmd[j] == "\\" else 1
            buf.append(ansi_c(cmd[i + 2:j]))
            i = j
        elif c == "$" and not ps and cmd[i + 1:i + 2] == '"':
            pass  # $"..." (locale string) is an ordinary double-quoted string
        elif c == '"':
            has = True
            i += 1
            start = i
            while i < n and cmd[i] != '"':
                if ps:
                    if cmd[i] == "`" and i + 1 < n:
                        i += 1
                elif cmd[i] == "\\" and i + 1 < n and cmd[i + 1] in '"\\$`\n':
                    i += 1
                buf.append(cmd[i])
                i += 1
            subs.extend(substitutions(cmd[start:i], ps))
        elif c == "\\" and not ps:
            if i + 1 < n:  # bash drops an unquoted backslash and keeps the next character
                i += 1
                if cmd[i] != "\n":
                    buf.append(cmd[i])
        elif c == "`" and ps:
            if i + 1 < n:
                i += 1
                if cmd[i] not in "\r\n":
                    buf.append(cmd[i])
        else:
            buf.append(c)
        i += 1
    end_seg()
    return segs


# ------------------------------------------------------------- git parsing

def basename(tok):
    return re.split(r"[\\/]", tok)[-1].lower()


def find_command(tokens, names):
    """Index of the command token if this segment runs one of `names`, else -1.

    Skips leading env assignments and wrappers (`sudo`, `env`, `timeout 5`, `then`...).
    """
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if ENV_ASSIGN.match(t):
            i += 1
        elif basename(t) in WRAPPERS:
            i += 1
            while i < n and tokens[i].startswith("-") and basename(tokens[i]) not in names:
                i += 1
            if basename(t) in SCAN_WRAPPERS:
                # Positional args (`timeout 5`, `nice -n 5`): look ahead for the command.
                j = i
                while j < n and basename(tokens[j]) not in names:
                    if basename(tokens[j]) in SCAN_STOP:
                        j = n
                    else:
                        j += 1
                if j < n:
                    i = j
        else:
            break
    if i < n and basename(tokens[i]) in names:
        return i
    return -1


def git_args_of(tokens):
    """If this segment is a git invocation, return the tokens after `git`."""
    i = find_command(tokens, GIT_NAMES)
    return tokens[i + 1:] if i >= 0 else None


def nested_scripts(seg):
    """Script text a shell/eval wrapper in this segment will run: [(text, is_powershell)].

    Only these wrapper arguments are re-scanned as commands; any other quoted word is data.
    """
    i = find_command(seg, SCRIPT_RUNNERS)
    if i < 0:
        return []
    name, args = basename(seg[i]), list(seg[i + 1:])
    if name in EVALS:
        return [(" ".join(args), False)]
    if name in PS_EVALS:
        return [(" ".join(args), True)]
    if name in BASH_SHELLS:
        j = 0
        while j < len(args):
            a = args[j]
            if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", a):
                return [(args[j + 1], False)] if j + 1 < len(args) else []
            if a in ("-o", "+o"):
                j += 2
            elif a[:1] in ("-", "+"):
                j += 1
            else:
                break
        return []
    if name in PS_SHELLS:
        for j, a in enumerate(args):
            low = a.lower()
            if low.startswith("-c") and ("-command".startswith(low) or "-commandwithargs".startswith(low)):
                return [(" ".join(args[j + 1:]), True)]
        return []
    for j, a in enumerate(args):  # cmd.exe
        if a.lower() in ("/c", "/k", "/r"):
            return [(" ".join(args[j + 1:]), True)]
    return []


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
    """Long option match including git's unambiguous-prefix abbreviations.

    `minlen` counts the leading `--`; per call site it is the shortest prefix that is
    still unique among that subcommand's options (git rejects ambiguous ones anyway).
    """
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
            if fl and (opt_is(a, "--hard", 3) or opt_is(a, "--merge", 4)):
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
            if fl and (cluster_has(a, "Au") or opt_is(a, "--all", 3) or opt_is(a, "--update", 3)):
                return "add explicit paths only (`git add <path> ...`), never the whole tree"
            if not fl and ADD_ALL_PATHSPEC.match(a):
                return "add explicit paths only (`git add <path> ...`), never the whole tree"
    elif sub == "push":
        return PUSH_DENY
    return None


def describe(sub, rest):
    text = " ".join(["git", sub] + rest)
    return text if len(text) <= 160 else text[:157] + "..."


def check_command(cmd, depth=0, ps=False):
    """Return a deny-reason string for `cmd`, or None (pass-through, no decision)."""
    if depth > MAX_DEPTH or not cmd:
        return None
    if depth == 0:
        cmd = strip_heredocs(cmd, ps)
    for seg in lex(cmd, ps):
        gargs = git_args_of(seg)
        if gargs is not None:
            sub, rest = split_global(gargs)
            if sub:
                why = judge(sub, rest)
                if why == PUSH_DENY:
                    return PUSH_MESSAGE
                if why:
                    return (
                        "git_guard: `%s` is blocked for subagents (%s). Do not run it or "
                        "work around it; stop and report back to the caller what you "
                        "needed instead." % (describe(sub, rest), why)
                    )
        # Only command substitutions and shell/eval wrapper scripts are re-scanned;
        # any other quoted word (`grep "git push" docs/`) is data.
        scripts = [(text, ps) for tok in seg for text in tok.subs]
        scripts += nested_scripts(seg)
        for text, sub_ps in scripts:
            why = check_command(text, depth + 1, sub_ps)
            if why:
                return why
    return None


def evaluate(payload):
    """Hook payload dict -> deny reason or None."""
    if not isinstance(payload, dict) or not payload.get("agent_id"):
        return None  # main session (or unknown): never interfere
    tool = payload.get("tool_name")
    if tool not in (None, "Bash", "PowerShell"):
        return None
    tin = payload.get("tool_input")
    cmd = tin.get("command") if isinstance(tin, dict) else None
    if not isinstance(cmd, str):
        return None
    return check_command(cmd, ps=(tool == "PowerShell"))


# ---------------------------------------------------------------- selftest

PASS = [
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
    "git reset HEAD a.py",
    "git reset --soft HEAD~1",
    "git reset --mixed HEAD~1",
    "git reset --keep HEAD~1",
    "git reset --help",
    "git reset --mi HEAD",
    "git commit --all -m x",
    "git commit --allow-empty -m x",
    "git commit --author=x -m y",
    "git add --dry-run a.py",
    "git add --patch a.py",
    "git add --intent-to-add a.py",
    "echo git push",
    "git commit -m 'run git push later'",
    "git log --oneline origin/main..HEAD",
    "git branch --format=%(refname)",
    "git branch --delete merged",
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
    # quoted words are data unless they are a shell/eval script or a command substitution
    "grep \"git push\" docs/",
    "grep -rn 'git stash' .",
    "git grep \"git reset --hard\"",
    "rg \"git push origin main\" README.md",
    "echo \"git push\"",
    "echo \"a; git stash\"",
    "git log --grep=\"git push\"",
    "git log --grep \"git checkout\"",
    "bash script.sh \"git push\"",
    "echo \"\\$(git stash)\"",
    "echo '$(git stash)'",
    "echo \"cost is \\`git push\\`\"",
    "git commit -m \"see (git push) docs\"",
    "git add src\\x.py",  # unquoted backslash is dropped, still not a banned form
]

DENY = [
    # quoted command substitutions and wrapper scripts are still scanned
    "echo \"$(git push)\"",
    "echo \"x `git stash` y\"",
    "git commit -m \"$(git push origin main)\"",
    "eval \"git push\"",
    "eval git stash",
    "zsh -c \"git stash\"",
    "env FOO=1 sh -c 'git push'",
    "timeout 5 bash -c \"git push origin main\"",
    "bash -c \"git push\" arg0",
    "bash -o pipefail -c 'git stash'",
    "xargs -I{} sh -c 'git push'",
    "powershell -NoProfile -Command \"git push\"",
    "pwsh -c 'git stash'",
    "cmd /c git push",
    # bash unquoting: backslash and ANSI-C strings
    "git \\push origin main",
    "git pu\\sh origin main",
    "\\git push",
    "g\\it stash",
    "git $'push' origin main",
    "git $'pu\\x73h'",
    "git $'\\160ush'",
    "git $'\\u0070ush'",
    "git $\"push\"",
    "git \"pu\"sh",
    "git reset $'--hard'",
    "git re\\set --ha\\rd",
    "git add \\.",
    "git ch\\eckout main",
    "git stash",
    "git stash",
    "git -C . stash",
    "sh -c \"git stash\"",
    "bash -lc 'cd x && git stash pop'",
    "FOO=1 git.exe reset --hard",
    "FOO=1 BAR=2 git stash",
    "git reset --hard HEAD~1",
    "git reset --merge",
    "git reset --h HEAD",
    "git reset --ha HEAD~1",
    "git reset --har",
    "git reset --me",
    "git commit --am",
    "git commit --ame --no-edit",
    "git add --a",
    "git add --al",
    "git add --u",
    "git add --upd",
    "git push --de origin old",
    "git push --dele origin old",
    "git push --m",
    "git push --mir",
    "git branch --forc x HEAD",
    "git branch --fo x HEAD",
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
    "git push",
    "git push origin main",
    "git push -u origin feature",
    "git push --dry-run",
    "git push --follow-tags origin main",
    "git push --set-upstream origin main",
    "git push origin HEAD:refs/heads/main",
    "git status && git push",
    "git commit -m x; git push origin main",
    "git log | head; git push",
    "git -C sub push origin main",
    "git -c core.pager=cat push",
    "sudo git push",
    "timeout 60 git push origin main",
    "FOO=1 git push",
    "env GIT_TERMINAL_PROMPT=0 git push",
    "/usr/bin/git push origin main",
    "git.exe push",
    "sh -c \"git push origin main\"",
    "bash -lc 'cd x && git push'",
    "(git push)",
    "if true; then git push; fi",
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
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}, True),
    ({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}, False),  # main session
    ({"tool_name": "Bash", "tool_input": {"command": "git stash"}}, False),  # main session
    ({"agent_id": "", "tool_name": "Bash", "tool_input": {"command": "git stash"}}, False),
    ({"agent_id": "a1", "tool_name": "Read", "tool_input": {"command": "git stash"}}, False),
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": {}}, False),
    ({"agent_id": "a1", "tool_name": "Bash", "tool_input": None}, False),
    ([], False),
    # PowerShell tool: same guard, PowerShell lexing
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git push origin main"}}, True),
    ({"tool_name": "PowerShell", "tool_input": {"command": "git push origin main"}}, False),  # main session
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git status; git push"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git status && git stash"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git status | Select-Object -First 3"}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "& git push"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "& \"C:\\Program Files\\Git\\bin\\git.exe\" push"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "C:\\Git\\bin\\git.exe stash"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git `push origin main"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git reset --hard HEAD~1"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "Write-Host \"$(git push)\""}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "Write-Host $(git push)"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "iex \"git push\""}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "Invoke-Expression 'git stash'"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "powershell -Command \"git push\""}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "bash -c 'git push'"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git add -A"}}, True),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git add src\\a.py"}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git add .\\src\\a.py"}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "Get-ChildItem C:\\Git\\bin"}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "Select-String \"git push\" docs\\x.md"}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git commit -m \"fix git push docs\""}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git commit -m \"say `\"git push`\"\""}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "git commit -m @'\ngit push is banned\ngit stash too\n'@"}}, False),
    ({"agent_id": "a1", "tool_name": "PowerShell", "tool_input": {"command": "Write-Host 'git push'"}}, False),
    ({"agent_id": "a1", "tool_name": "Write", "tool_input": {"command": "git push"}}, False),
]


def selftest():
    bad = 0
    total = 0
    total += 1
    if check_command("git push origin main") != PUSH_MESSAGE:
        bad += 1
        print("FAIL (push deny reason text)")
    for cmd in PASS:
        total += 1
        r = check_command(cmd)
        if r is not None:
            bad += 1
            print("FAIL (expected pass-through): %r -> %s" % (cmd, r))
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
            print("FAIL (payload, expected %s): %r" % ("deny" if want else "pass-through", payload))
    n, b = selftest_meter()
    total += n
    bad += b
    print("git_guard selftest: %d cases, %d failed" % (total, bad))
    return 1 if bad else 0


def _old_deny_bytes(reason):
    """The deny line exactly as this hook wrote it before the meter existed (independent of deny_output)."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}}) + "\n"


def selftest_meter():
    """Deny output is byte-identical whatever the meter does; subagents never reach the meter."""
    global meter_nudge, _load_meter
    real, real_loader, calls, bad, total = meter_nudge, _load_meter, [], 0, 0
    main_ls = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}}

    def boom(payload):
        calls.append(payload)
        raise RuntimeError("meter exploded")

    def says(payload):
        calls.append(payload)
        return "delegate-kit context meter: test"

    def silent(payload):
        calls.append(payload)
        return None

    def bad_loader():
        raise ImportError("no meter")

    def expect(ok, what):
        nonlocal bad, total
        total += 1
        if not ok:
            bad += 1
            print("FAIL (meter): " + what)

    try:
        # 1. every deny case: output == the pre-meter bytes whatever the meter would say or do
        denied = [(p, evaluate(p)) for p, want in PAYLOADS if want]
        denied += [({"agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": c}}, check_command(c))
                   for c in DENY]
        for impl in (real, silent, says, boom):
            meter_nudge = impl
            for payload, reason in denied:
                expect(bool(reason) and handle(json.dumps(payload)) == _old_deny_bytes(reason),
                       "deny bytes changed for %r" % (payload,))
        _load_meter = bad_loader  # the real wrapper's own try/except, with the import failing
        meter_nudge = real
        for payload, reason in denied[:5]:
            expect(handle(json.dumps(payload)) == _old_deny_bytes(reason), "deny changed when meter import fails")
        expect(handle(json.dumps(main_ls)) == "", "pass-through changed when meter import fails")
        _load_meter = real_loader
        # 2. a raising meter leaves a main pass-through unchanged
        meter_nudge = boom
        expect(handle(json.dumps(main_ls)) == "", "raising meter must leave pass-through unchanged")
        # 3. subagent payloads never call the meter
        calls.clear()
        meter_nudge = says
        for payload in (
            {"agent_id": "a1", "session_id": "s", "tool_name": "Bash", "tool_input": {"command": "git status"}},
            {"agent_id": "a1", "session_id": "s", "tool_name": "Bash", "tool_input": {"command": "git push"}},
            {"agent_id": "a1", "tool_name": "Read", "tool_input": {}},
        ):
            expect(handle(json.dumps(payload)) in ("", _old_deny_bytes(PUSH_MESSAGE)),
                   "unexpected subagent output: %r" % (payload,))
        expect(not calls, "meter was called for a subagent payload")
        # 4. a main-session pass-through carries the nudge as PreToolUse additionalContext, no decision
        got = handle(json.dumps(dict(main_ls, tool_input={"command": "git push"})))
        out = json.loads(got)["hookSpecificOutput"] if got else {}
        expect(out == {"hookEventName": "PreToolUse", "additionalContext": "delegate-kit context meter: test"}
               and got.endswith("\n"), "main nudge output shape: %r" % got)
        expect(len(calls) == 1, "meter should be called once for a main pass-through")
        # 5. silent meter and junk input give no output
        meter_nudge = silent
        expect(handle(json.dumps(main_ls)) == "", "silent meter: empty output")
        expect(handle("not json") == "" and handle("") == "" and handle("[]") == "", "junk input: empty output")
        # 6. the real meter, end to end: kill switch off => nothing; a 250k-context transcript => nudge
        meter_nudge = real
        expect(_real_meter_roundtrip(), "real meter round trip through handle()")
    finally:
        meter_nudge, _load_meter = real, real_loader
    return total, bad


def _real_meter_roundtrip():
    import tempfile

    if not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter.py")):
        return True
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(tmp, "cfg")
        try:
            tp = os.path.join(tmp, "t.jsonl")
            with open(tp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {
                    "id": "m1", "model": "x", "usage": {"input_tokens": 10, "cache_read_input_tokens": 250000}}}) + "\n")
            payload = {"session_id": "rt", "transcript_path": tp, "hook_event_name": "PreToolUse",
                       "tool_name": "Bash", "tool_input": {"command": "ls"}}
            first = handle(json.dumps(payload))
            ctx = (json.loads(first).get("hookSpecificOutput") or {}).get("additionalContext", "") if first else ""
            again = handle(json.dumps(payload))  # once per crossing
            os.makedirs(os.path.join(tmp, "cfg", "delegate-kit"), exist_ok=True)
            with open(os.path.join(tmp, "cfg", "delegate-kit", "config.json"), "w", encoding="utf-8") as fh:
                json.dump({"meter": {"main": {"enabled": False}}}, fh)
            payload["session_id"] = "rt2"
            off = handle(json.dumps(payload))
            return "context meter" in ctx and again == "" and off == ""
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old


# -------------------------------------------------------------------- main

def deny_output(reason):
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    ) + "\n"


def nudge_output(text):
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}
    ) + "\n"


def _load_meter():
    """Import meter.py from this file's directory (lazy: only a main-session pass-through gets here).

    Compiling meter.py costs ~10 ms per call, so its bytecode is cached under <config>/delegate-kit/pyc
    (sys.pycache_prefix, even when PYTHONDONTWRITEBYTECODE is set) instead of beside the plugin files.
    """
    import importlib.util

    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(os.path.expanduser("~"), ".claude")
    old = (sys.pycache_prefix, sys.dont_write_bytecode)
    try:
        sys.pycache_prefix = os.path.join(cfg, "delegate-kit", "pyc")
        sys.dont_write_bytecode = False
        spec = importlib.util.spec_from_file_location(
            "delegate_kit_meter", os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.pycache_prefix, sys.dont_write_bytecode = old


def meter_nudge(payload):
    """Main-agent nudge text for this payload, or None. Never raises."""
    try:
        return _load_meter().main_nudge(payload)
    except Exception:
        return None


def handle(raw):
    """Hook stdin text -> stdout text ('' = no decision). The deny is decided and serialized first."""
    try:
        payload = json.loads(raw) if raw.strip() else None
        reason = evaluate(payload)
        if reason:
            return deny_output(reason)
    except Exception:
        return ""  # on error: no decision (pass-through)
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return ""  # subagent (or unknown): the meter is never consulted
    try:
        text = meter_nudge(payload)
        return nudge_output(text) if isinstance(text, str) and text else ""
    except Exception:
        return ""


def main():
    if "--selftest" in sys.argv[1:]:
        try:
            return selftest()
        except Exception:
            import traceback

            traceback.print_exc()
            print("git_guard selftest: crashed")
            return 1  # a crashing selftest must not look green
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        out = handle(raw)
    except Exception:
        return 0
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
