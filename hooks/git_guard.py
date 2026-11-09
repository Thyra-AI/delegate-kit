#!/usr/bin/env python3
"""git_guard - deny destructive git commands to subagents (standalone, tool-agnostic).

Kimchi port of the delegate-kit git-guard, without Claude-specific hook or tool
names (the PreToolUse payload and the dedicated PowerShell tool are gone; any shell
tool's text is scanned the same way).

    python3 git_guard.py -- <command...>    # 0 = pass-through (not blocked), 1 = blocked
    python3 git_guard.py --selftest         # exits non-zero on any mismatch

Scans the raw argv words for blocked git operations: stash / restore / clean / rebase /
checkout / switch / filter-branch / filter-repo; reset --hard/--merge; commit --amend;
forced branch delete/move; `git add -A` / `-u` / `--all` / `.` / `:/` / `*`; and any push.
Command substitutions (`$(...)`), backticks, heredocs, and nested shell scripts
(sh -c / bash -c / eval / any shell tool, including powershell and cmd) are re-scanned
the same way. Pure regex/lexing, stdlib only, no git subprocess. This is a guardrail
against habitual commands, not a sandbox: `python -c "subprocess..."` bypasses it.
"""
import base64
import binascii
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
ENCODED_DENY = (
    "git_guard: an undecodable PowerShell -EncodedCommand is blocked for subagents (it cannot be checked). "
    "Run the command directly instead, or stop and report back to the caller what you needed."
)
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


def decode_encoded(b64):
    """The script in a PowerShell -EncodedCommand argument (base64 of UTF-16LE), or None if undecodable."""
    try:
        raw = re.sub(r"\s+", "", b64)
        raw += "=" * (-len(raw) % 4)
        return base64.b64decode(raw, validate=True).decode("utf-16-le")
    except (binascii.Error, ValueError, UnicodeError):
        return None


def nested_scripts(seg):
    """Script text a shell/eval wrapper in this segment will run: [(text, is_powershell)].

    Only these wrapper arguments are re-scanned as commands; any other quoted word is data.
    A text of None marks a PowerShell -EncodedCommand that could not be decoded.
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
            # -EncodedCommand: any prefix of it (-e, -en, -enc...) plus the -ec alias
            if low == "-ec" or (len(low) >= 2 and "-encodedcommand".startswith(low)):
                return [(decode_encoded(args[j + 1]), True)] if j + 1 < len(args) else []
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
            if text is None:
                return ENCODED_DENY
            why = check_command(text, depth + 1, sub_ps)
            if why:
                return why
    return None


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
    "sh $KIMCHI_DK_DIR/bin/dk squash --plan schedule.json",
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

def _enc(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def selftest():
    bad = total = 0

    def expect(ok, what):
        nonlocal bad, total
        total += 1
        if not ok:
            bad += 1
            print("FAIL: " + what)

    for cmd in PASS:
        r = check_command(cmd)
        expect(r is None, "expected pass-through: %r -> %s" % (cmd, r))
    for cmd in DENY:
        r = check_command(cmd)
        expect(r is not None, "expected deny: %r (got %r)" % (cmd, r))
    # shell tool with -EncodedCommand (base64 UTF-16LE): decoded and rescanned
    for cmd in (
        "powershell -EncodedCommand " + _enc("git push origin main"),
        "pwsh -enc " + _enc("git stash"),
        "powershell.exe -NoProfile -ec " + _enc("git push"),
        "powershell -e " + _enc("git reset --hard"),
        "POWERSHELL -ENCODEDC " + _enc("& git push"),
        "powershell -enc " + _enc("Write-Host $(git push)"),
        "powershell -enc " + _enc("powershell -Command 'git push'"),
        "powershell -enc " + _enc("git push").rstrip("="),
        "powershell -enc !!notbase64!!",
        "powershell -enc QQ==",
    ):
        expect(check_command(cmd) is not None, "expected encoded deny: %r" % (cmd[:40],))
    for cmd in (
        "powershell -enc " + _enc("git status"),
        "powershell -NoProfile -EncodedCommand " + _enc("Get-ChildItem; git log -5"),
        "powershell -enc " + _enc("Write-Host 'git push'"),
        "powershell -enc",
        "powershell -ExecutionPolicy Bypass -File x.ps1",
    ):
        expect(check_command(cmd) is None, "expected encoded pass: %r" % (cmd[:40],))
    print("git_guard selftest: %d cases, %d failed" % (total, bad))
    return 1 if bad else 0


# -------------------------------------------------------------------- main

def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        try:
            return selftest()
        except Exception:
            import traceback
            traceback.print_exc()
            print("git_guard selftest: crashed")
            return 1
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: python3 git_guard.py -- <command...>", file=sys.stderr)
        return 2
    why = check_command(" ".join(argv))
    if why:
        print(why, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
