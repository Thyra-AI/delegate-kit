# delegate-kit

**A curated bench of 5 specialist subagents for Claude Code — and the playbook for when to delegate to which.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2)
![One-command setup](https://img.shields.io/badge/setup-one%20command-brightgreen)

Most agent collections give you a hundred role-experts (`react-expert`, `sql-expert`, …). **delegate-kit is the opposite: a small orchestration layer.** Five subagents organized not by domain but by *reasoning depth and context cost*, plus a `/subagents` skill that teaches the main agent **when to hand work off, and to whom** — so your expensive main context stays clean and your hard thinking gets the strongest model.

---

## Why you want this

Every long Claude Code session drowns in cheap work: fifteen greps to trace a flow, a dozen reads to gather facts, a build, a commit. Doing it inline burns the one thing you can't get back — your main context window. delegate-kit fixes the workflow, not the model:

- **Offload the grind.** Multi-hop, low-judgment work goes to a cheap agent; the tool output lands in *its* context, not yours.
- **Reserve depth for decisions.** Hard trade-offs go to a pure-reasoning agent on the strongest model, with no tool noise.
- **Map before you touch.** Understand a system via a flow report with `path:line` anchors instead of pasting whole files.

## The roster

| Agent | Model | Best at |
|-------|-------|---------|
| **super-thinker** | fable | Top-tier pure reasoning for the hardest, highest-stakes calls — subtle trade-offs, intricate plans, where depth beats speed. No tools. |
| **thinker** | opus | Everyday deep reasoning over context you already have — trade-offs, planning, debugging-by-reasoning. No tools, pure thought. |
| **researcher** | sonnet | Mapping how a system works end-to-end — **and** external research via `WebSearch`/`WebFetch` (docs, APIs, specs, changelogs). Never dumps whole files or whole pages; returns an anchored, source-cited report. |
| **executer** | sonnet 4.6 | The coding workhorse. Implements a settled plan end-to-end — writes/edits code, runs the build & tests, fixes what it broke. Full tools incl. Edit/Write. |
| **simple-tasks** | haiku | Mechanical chores (commits, pushes, builds, file ops) **and** cheap multi-hop context-saving work. |

The **`/subagents`** skill is the playbook: it gives the main agent the roster, briefing rules for each agent, and a decision guide for picking the right one — including the classic chain **researcher maps → thinker decides → executer builds, verifies & commits**.

## Install

```
/plugin marketplace add Jose-Ribeir/delegate-kit
/plugin install delegate-kit
/delegate-kit:setup
```

The third step is required, and takes one prompt. `/delegate-kit:setup` detects which optional
integrations this machine actually has, asks you to confirm, and writes the five composed agent
definitions to `~/.claude/agents/`. **Start a new session afterwards** — agent definitions load at
session start.

Re-run it whenever you add or remove an MCP server, or upgrade the plugin.

You then have:

- the **`/delegate-kit:subagents`** skill (the playbook), and
- five spawnable agents: `thinker`, `super-thinker`, `researcher`, `executer`, `simple-tasks`.

> **Why a setup step instead of shipping the agents directly?** Agent frontmatter is static — `tools:`
> is read at load time with no conditionals — so a shipped definition would have to either grant tools
> for MCP servers you may not have (dead instructions) or deny tools you do (worse navigation).
> Composing per machine solves both. It also means the plugin ships **no** `agents/` directory: if it
> did, its copies would register alongside the composed ones under the same names, and which one a
> caller spawns would be a coin flip.

## Usage

Ask the main agent to route work, or invoke the playbook directly:

```
/delegate-kit:subagents
```

Or just delegate in natural language and let Claude pick:

- *"Trace how a request flows from the API route to the DB write."* → **researcher** returns an anchored flow report.
- *"Find out what the Meta Graph API actually requires for this permission, and how our code calls it."* → **researcher** again — it has web access, so external-docs research is its job, not a fallback to a generic agent.
- *"Given that flow, should we cache at the route or the service layer? Reason it through."* → **thinker** weighs the trade-off.
- *"Implement caching at the service layer, run the tests, and commit it."* → **executer** writes the code, verifies it, and commits its own work.

## Integrations — the agents are wired for what you actually have

Out of the box the agents navigate with built-in `Grep` / `Glob` / `Read` / `Bash` (plus
`WebSearch` / `WebFetch` for the `researcher`). That works everywhere and is a complete, honest
setup on its own.

`/delegate-kit:setup` can additionally wire them for tools you have installed. Each integration is a
**fragment** — a small file declaring the tool grants it adds and the prose that teaches an agent to
use them. Two ship with the plugin:

- **[Serena](https://github.com/oraios/serena)** — MCP server for symbol-level navigation (read one
  symbol, not a whole file). Applies to `executer`, `researcher`, `simple-tasks`.
- **graphify** — flow/structure knowledge graphs (`graphify query/path/explain`). Applies to
  `researcher`; adds prose only, since it runs through Bash.

Composition is declarative: re-running setup with a smaller selection genuinely *removes* an
integration, tools and prose both.

### Writing your own

Drop a fragment in `~/.claude/delegate-kit/integrations/<name>.md` and setup will offer it alongside
the shipped ones:

```markdown
---
name: mytool
title: MyTool — what it does
detect_mcp: mytool               # or: detect_bin: mytool
agents: executer, researcher
tools: mcp__mytool__thing_one, mcp__mytool__thing_two
tools@researcher: mcp__mytool__thing_one     # narrow the grant for one agent
---

## MyTool

Prose teaching the agent how and when to use it.

## agent:researcher

Optional per-agent override, replacing the shared prose above for this agent.
```

That directory is never touched by a plugin update — which also makes it the right place for
anything you don't want in a public repo.

## Customizing

- **Models** are pinned per agent in `templates/agents/*.md` frontmatter (`model:`). Change any to a
  model you have access to — e.g. if you don't have `fable`, set `super-thinker` to `opus`. `executer`
  is pinned to the explicit id `claude-sonnet-4-6` (chosen over Sonnet 5 for token efficiency on
  coding); if that id differs in your account, update it to your Sonnet 4.6 id rather than letting it
  fall back to a heavier model. Re-run `/delegate-kit:setup` after editing a template.
- **Don't pass `model` when spawning.** An explicit `model` on the Agent call overrides the frontmatter
  pin and only accepts coarse aliases (`sonnet`/`opus`/`haiku`/`fable`), so it can't even express
  `claude-sonnet-4-6` — it silently swaps in a different model.
- **Add your own agents** alongside these and reference them from your own copy of the skill.

## How it's structured

```
delegate-kit/
├── .claude-plugin/
│   ├── marketplace.json     # marketplace registry (one plugin, source ./)
│   └── plugin.json          # plugin manifest
├── templates/agents/        # base definitions — built-in tools only
│   ├── thinker.md
│   ├── super-thinker.md
│   ├── researcher.md
│   ├── executer.md
│   └── simple-tasks.md
├── integrations/            # optional fragments (tool grants + prose)
│   ├── serena.md
│   └── graphify.md
├── bin/
│   └── compose.py           # templates + fragments -> ~/.claude/agents
├── commands/
│   └── setup.md             # /delegate-kit:setup
└── skills/
    └── subagents/
        └── SKILL.md         # the /subagents delegation playbook
```

Note there is no `agents/` directory: composed definitions are installed to `~/.claude/agents/`, so
exactly one definition per agent name exists. See the install section for why.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first contributions: a new specialist agent that fills a real gap, or sharper briefing guidance in the skill.

## License

[MIT](LICENSE) © José Ribeiro
