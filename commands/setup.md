---
name: setup
description: Install or reconfigure the delegate-kit subagents, picking which optional integrations (Serena, graphify, or your own) they are wired for. Detects what is actually available in this environment, confirms the picks with you, then writes the composed agent definitions to ~/.claude/agents. Run this after installing the plugin, after adding or removing an MCP server, and after upgrading the plugin.
allowed-tools: Bash, Read, AskUserQuestion
---

# delegate-kit — setup

Compose the seven subagent definitions from the shipped templates plus whichever
optional integrations this environment actually has, and install them to
`~/.claude/agents`.

**Why this exists.** Agent frontmatter is static — `tools:` is read at load time, with
no conditionals. An agent granted a tool whose MCP server isn't installed carries dead
instructions; an agent denied a tool the user *does* have navigates worse than it could.
So the tool grants and the matching prose are composed per machine instead of shipped
fixed.

**The plugin deliberately ships no `agents/` directory.** If it did, its copies would
register alongside the composed ones under the same names, and which one a caller
spawned would be a coin flip. Composing into `~/.claude/agents` keeps exactly one
definition per agent.

## 1. Discover

```
python "${CLAUDE_PLUGIN_ROOT}/bin/compose.py" --list
```

This prints every fragment found, in both the plugin's `integrations/` and the user's
`~/.claude/delegate-kit/integrations/` (same name = the user's wins), each with a
`detect_mcp` (an MCP server name), a `detect_bin` (an executable on PATH), or neither.

If `python` isn't found, try `python3`, then `py -3`. If none work, stop and tell the
user delegate-kit's setup needs Python 3 on PATH — don't try to compose by hand.

## 2. Detect what's actually here

For each fragment, work out whether its dependency is present:

- **`detect_mcp`** — check your own available tools for `mcp__<server>__*`. They may be
  deferred rather than loaded, so search for them before concluding a server is absent.
  Deferred-but-listed counts as present.
- **`detect_bin`** — check PATH, e.g. `command -v <bin>`.
- **Neither** — some fragments declare no detection because presence isn't the question.
  The shipped `python` fragment is one: `python` on PATH says nothing about whether the
  code this user reviews is Python. Treat these as *ask, never assume* — list them
  unselected and let the user say.

Report what you found as a short list: present, absent, and the one case worth calling
out — a fragment whose dependency you could **not** determine either way.

## 3. Confirm with the user

Detection is a starting proposal, not the answer. Ask with `AskUserQuestion`, multi-select,
listing every discovered fragment with its title and whether you detected it. Pre-select the
detected ones. Two reasons the user may override you: they may have a server configured per
project rather than globally, and they may simply not want an agent wired for something they
have.

If nothing is discoverable at all, say so and offer to compose with no integrations — that
is a valid, working setup, just built-ins only.

## 4. Compose

```
python "${CLAUDE_PLUGIN_ROOT}/bin/compose.py" --enable <comma,separated,names>
```

Pass `--enable ""` for none. Add `--dry-run` first if the user wants to see what changes
before it lands. Existing files in `~/.claude/agents` are backed up to
`~/.claude/agents/.delegate-kit-backup` before being overwritten.

Composition is **declarative, not incremental**: the output is a function of the templates
plus the enabled set, so re-running with a smaller set genuinely removes an integration —
tools and prose both. That's how you uninstall one.

## 5. Report

Tell the user:

- which agents were written, and which integrations each ended up with (the script prints
  this per file — `thinker`, `super-thinker` and `director` take none by design: the first two
  have no tools, and the director's only tool is spawning the others);
- that **a new session is required** — agent definitions load at session start, so the
  current session still has the old ones;
- **how to use the `director`** — it is not spawned like the others, it's the agent you talk to.
  Its only tool is spawning the other six, so every read and edit lands in a worker's context
  instead of the expensive one. Give the project-settings route first — it is the only one that
  works everywhere, the desktop app included, where there is no command line to pass a flag to:
  `{ "agent": "director" }` in the project's `.claude/settings.json`. The `claude --agent director`
  flag is the terminal-only, one-session alternative. (`/delegate-kit:run` hands it a single
  objective from a normal session instead, at a higher cost — mention it as the one-off path, not
  the default.) If a director ever reports it cannot spawn, raise
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`;
- if any `~/.claude/agents/*.md` existed before, that the originals are in
  `.delegate-kit-backup`.

Then check for the duplicate-definition trap and warn if it applies: if a **project**
`.claude/agents/` directory defines any of the same seven names, or an older delegate-kit
install is still enabled with its own `agents/`, those register alongside these and the
model may spawn either. Name the conflicting paths; let the user decide what to remove.

## 6. Offer to turn on director mode here

Composing the agents does not make any of them the session agent — `director` included. Since the
settings route is the only one available in the desktop app, offer it rather than leaving the user
to find it:

> Want new sessions in this project to start as the `director`? I can add `{ "agent": "director" }`
> to `.claude/settings.json`.

Only if they say yes: merge the `agent` key into the project's `.claude/settings.json`, creating
the file if it does not exist. **Merge, never overwrite** — that file routinely holds permissions,
hooks and env the user cares about.

Be straight about the trade before writing it, because it is easy to be surprised by:

- every new session on this folder has **no file tools at all** — no reading, editing, or running
  commands directly, only delegation. That is the entire point, but it is a real change to how the
  project feels to work in;
- it takes effect on the **next** session, not this one;
- undoing it is removing the one key.

Suggest it for projects with real multi-step work in them. For a project where the user mostly asks
one-off questions, the director is a Fable-priced middleman — say so instead of pushing it.

Also check `.gitignore` covers `.delegate-kit/` (the run ledger). If it does not, offer to add it.

## Writing a new integration

To wire the agents for an MCP server that has no fragment yet, drop a file in
`~/.claude/delegate-kit/integrations/<name>.md`:

```markdown
---
name: mytool
title: MyTool — what it does
detect_mcp: mytool          # or: detect_bin: mytool
agents: executer, researcher, simple-tasks
tools: mcp__mytool__thing_one, mcp__mytool__thing_two
tools@simple-tasks: mcp__mytool__thing_one
---

## MyTool

Prose teaching the agent how and when to use it. This is spliced in at the
`<!-- delegate-kit:integrations -->` marker in the template.

## agent:researcher

Optional per-agent override — replaces the shared prose above for this one agent.
```

`agents:` limits which agents the fragment applies to. `tools@<agent>` narrows the grant
for one agent (the runner doesn't need the full symbol-navigation set the researcher does).
A fragment with an empty `tools:` adds prose only — right for a CLI tool that runs through Bash.

Fragments in `~/.claude/delegate-kit/integrations/` are never touched by a plugin update,
which also makes that the place for anything you don't want in a public repo.
