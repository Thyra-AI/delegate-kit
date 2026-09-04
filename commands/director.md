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

Then you cannot edit settings yourself. Spawn `simple-tasks` to do the edit described below, or —
better — tell the user to run this from a normal session. Turning director mode *off* from inside a
director is the awkward case; say so plainly rather than delegating around it silently.

## 1. Read the argument

- **`on`** — enable for this project.
- **`off`** — disable.
- **anything else, or empty** — report current state and stop. Do not change anything.

## 2. Report state (always do this first)

Read `.claude/settings.local.json` and `.claude/settings.json` in the project root, and
`~/.claude/settings.json`. Report which of them carry an `agent` key and what it is set to. The
user-scope file matters: an `agent` there applies to **every** project, and would explain director
mode that seems to follow them around.

If the argument was empty, stop here.

## 3. `on`

First check the agent exists — `~/.claude/agents/director.md` or `.claude/agents/director.md`. If it
does not, `/delegate-kit:setup` has not been run on this machine; say that and stop, because writing
the key would produce sessions that fail to start.

Write `"agent": "director"` into **`.claude/settings.local.json`**, creating the file if needed.

- **Merge, never overwrite.** That file routinely holds permissions, hooks and env. Read it, add the
  one key, write it back.
- **Local, not shared.** `.claude/settings.json` is committed, so the key would force director mode
  on everyone who clones the repo. Only use the shared file if the user explicitly asks for it, and
  tell them what it means when they do.
- Check `.gitignore` covers `.claude/settings.local.json`, and offer to add it if not.

Then tell the user, in this order:

1. it takes effect on the **next** session, not this one;
2. **every** new session on this folder will have no file tools at all — no reading, editing, or
   running commands directly, only delegation. That is the point, but it also means quick one-off
   edits in this project now go through a worker;
3. `/delegate-kit:director off` turns it back off, and it is easiest to run that from a normal
   session.

## 4. `off`

Remove the `agent` key from `.claude/settings.local.json`. If that leaves the file as an empty
object, delete the file; if that leaves `.claude/` empty, remove the directory too — this command
created both, so it should be able to leave no trace.

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
