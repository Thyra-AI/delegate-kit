---
name: director
description: Turn director mode on or off for this project — the setting that makes new sessions start as the orchestrator, with no file tools of its own. Use it in the Claude desktop app, where there is no `claude --agent director` flag to pass. With no argument, reports whether it is currently on.
argument-hint: "[on|off]"
allowed-tools: Bash, Read, Write, Edit, AskUserQuestion
---

# delegate-kit — director mode

> $ARGUMENTS

The `director` agent can be the **session agent** — the one the user talks to. There are exactly
two ways to make that happen: the `--agent director` flag at launch, and the `agent` key in
settings. The flag is per-session but terminal-only; the settings key works on every surface but is
a property of the **folder**, not of the sitting. In the desktop app the key is the only route.

This command flips that key, so the user doesn't have to hand-edit JSON or remember what they left
switched on.

## 0. If you have no file tools, you are already in a director session

Then you cannot edit settings yourself, and there is one rule: **say so, and let the user choose.**
Tell them the edit has to happen in a worker's context, and offer to spawn `simple-tasks` to do it.
Do not delegate it silently — a director quietly rewriting the setting that makes it the director is
exactly the kind of thing a user should see coming.

## 1. Read the argument

- **`on`** — enable for this project.
- **`off`** — disable.
- **anything else, or empty** — report current state and stop. Do not change anything.

## 2. Report state (always do this first)

Read `.claude/settings.local.json` and `.claude/settings.json` in the project root, and
`~/.claude/settings.json`. Report which of them carry an `agent` key and what it is set to. The
user-scope file matters: an `agent` there applies to **every** project, and would explain director
mode that seems to follow them around.

**Precedence, when more than one carries the key:** `.claude/settings.local.json` beats
`.claude/settings.json`, which beats `~/.claude/settings.json`. A `--agent` flag at launch beats all
three. Say which file is actually winning — a key that is set but shadowed is the likeliest reason
director mode "didn't work".

If the argument was empty, stop here.

## Never rewrite a file you could not parse

This applies to every write below. Read the settings file first and parse it **strictly** — the way
`JSON.parse` or Python's `json` module does. Do not reach for a lenient JSON5/JSONC parser and do not
pre-strip comments to get past this check: leniency here is exactly how a comment or a BOM silently
vanishes from someone's file. **If it does not parse — invalid JSON, a trailing comma, comments, a
stray BOM — stop.** Show the parse error and the
offending file, and ask the user how they want it handled. Do not strip, repair, reformat, or
re-serialize your way past it: these files hold permissions, hooks and env that are expensive to
lose, and a "helpful" rewrite of a file you did not understand is the one failure mode with no undo.

The same applies in reverse after any write: re-read the file, confirm it still parses, and confirm
the keys you did not touch are still there.

## 3. `on`

First check the agent exists — `~/.claude/agents/director.md` or `.claude/agents/director.md`. If it
does not, `/delegate-kit:setup` has not been run on this machine; say that and stop, because writing
the key would produce sessions that fail to start.

**Check the value before writing anything.** Read `.claude/settings.local.json` and look at the
`agent` key:

- **Not present** — proceed.
- **Already `director`** — director mode is on already. Say so and stop; do not rewrite the file.
- **Set to some other agent** — that key is not yours. Report what it is set to and ask before
  touching it. The `agent` key is shared with every other agent someone might run as the session
  agent, and this command has no business clobbering one it did not write.

Also check the other two files from step 2. If `~/.claude/settings.json` or `.claude/settings.json`
carries an `agent` key naming something else, writing the local key will work but the user should
hear about it — say which file holds what, and which one wins. This does **not** block the write: the
local key wins by precedence, so mention it and carry on. Only the *local* value gates the write.

Then write `"agent": "director"` into **`.claude/settings.local.json`**, creating the file if needed.

- **Merge, never overwrite.** That file routinely holds permissions, hooks and env. Read it, add the
  one key, write it back.
- **Local, not shared.** `.claude/settings.json` is committed, so the key would force director mode
  on everyone who clones the repo. Only use the shared file if the user explicitly asks for it, and
  tell them what it means when they do.
The two checks below are git housekeeping. **If the project is not a git repository, skip both
silently** — there is nothing to exclude from. Where either says to offer something, that means
propose it and wait for a yes before writing; do not add lines to someone's files unasked.

- Check `.claude/settings.local.json` is ignored, and offer to add it to `.gitignore` if not. **Ask
  git, not the file** — `git check-ignore -q <path>` — because a global core.excludesFile or an
  existing `.git/info/exclude` may already cover it, and offering to add a line that changes nothing
  is noise.
- Check the run ledger is excluded too, the same way — but **query it with a trailing slash**,
  `git check-ignore -q .delegate-kit/`. The ignore entry is directory-only (`.delegate-kit/`), and
  before the first run the directory does not exist yet; without the slash git cannot tell it is a
  directory, guesses "file", and reports *not ignored* for a path that plainly is. That false
  negative is how you end up offering a duplicate line. A
  director run writes artifacts to `.delegate-kit/` in the project root — logs, benchmark data,
  patches, easily megabytes. If nothing covers it, offer to append it to **`.git/info/exclude`**:
  that silences `git status` without touching a file the user commits. Left unexcluded it is not
  just noise — one `git add -A` puts the whole ledger into their history.

Then tell the user, in this order:

1. it takes effect on the **next** session, not this one;
2. **every** new session on this folder will have no file tools at all — no reading, editing, or
   running commands directly, only delegation. That is the point, but it also means quick one-off
   edits in this project now go through a worker;
3. `/delegate-kit:director off` turns it back off, and it is easiest to run that from a normal
   session.

## 4. `off`

**Check the value before removing anything.** Read the local file and look at the `agent` key:

- **Set to `director`** — proceed; this is the key this command wrote.
- **Not set anywhere** — director mode is already off. Say so and stop: nothing to remove, no file to
  change. This is the most common way `off` gets run — someone checking rather than fixing — so it is
  a normal outcome, not an error.
- **Set to some other agent** — that key is not yours; see below.

If it names some other agent, report what it is set to and ask
before touching it. The key is shared with every other agent someone might run as the session agent,
and this command has no business clobbering one it did not write.

Once confirmed, remove the key from `.claude/settings.local.json`. If that leaves the file as an
empty object, delete the file; if that leaves `.claude/` empty, remove the directory too — this
command created both, so it should be able to leave no trace.

Leave the `.gitignore` entry alone. It is correct regardless of whether director mode is on.

If the `agent` key is not in the local file but *is* in `.claude/settings.json` or
`~/.claude/settings.json`, do not silently edit either — say which file holds it and ask. The
user-scope one especially: removing it changes every project they have.

Confirm the change by naming what a new session will now have, and note that the current session is
unaffected either way.

## When to talk the user out of it

Director mode earns its keep on projects with real multi-step work in them — objectives worth
several delegation hops. On a project where the user mostly asks one-off questions or makes small
edits, a director is a middleman on the most expensive model, and they will feel it. Say so instead
of just flipping the switch. `/delegate-kit:run <objective>` is the better fit there: it hands over
one objective from a normal session, and costs the premium only for that objective instead of for
every session in the folder.
