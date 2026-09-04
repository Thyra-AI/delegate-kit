# delegate-kit

**A curated bench of 7 specialist subagents for Claude Code — one of which runs the other six — and the playbook for when to delegate to which.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2)
![One-command setup](https://img.shields.io/badge/setup-one%20command-brightgreen)

Most agent collections give you a hundred role-experts (`react-expert`, `sql-expert`, …). **delegate-kit is the opposite: a small orchestration layer.** Seven subagents organized not by domain but by *reasoning depth and context cost*, plus a `/subagents` skill that teaches the main agent **when to hand work off, and to whom** — so your expensive main context stays clean and your hard thinking gets the strongest model. One of the seven, the **director**, is that layer made literal: run your session as it and you get a main agent that *cannot* touch a file, only delegate — so the expensive model spends its tokens deciding and the cheap ones do the work.

---

## Why you want this

Every long Claude Code session drowns in cheap work: fifteen greps to trace a flow, a dozen reads to gather facts, a build, a commit. Doing it inline burns the one thing you can't get back — your main context window. delegate-kit fixes the workflow, not the model:

- **Offload the grind.** Multi-hop, low-judgment work goes to a cheap agent; the tool output lands in *its* context, not yours.
- **Reserve depth for decisions.** Hard trade-offs go to a pure-reasoning agent on the strongest model, with no tool noise.
- **Map before you touch.** Understand a system via a flow report with `path:line` anchors instead of pasting whole files.
- **Or run the whole session on rails.** Start as the `director` and your main agent has *no file tools at all* — it can only delegate, so every read, edit and test run lands in a cheap worker. Measured on a real cross-module bug fix: **75% fewer Fable tokens for 12% more money.** [The full numbers, including where the trade goes bad.](#what-it-saves--measured)

## The roster

| Agent | Model | Best at |
|-------|-------|---------|
| **director** | fable | **The agent you talk to.** Run your session as it — `/delegate-kit:director on` for a project, or `claude --agent director` for one terminal session — and its only tool is the ability to spawn the other six — it cannot read, grep, edit or run anything. Every file touch happens in a cheaper agent's context; it decides from their reports. Measured at **−75% Fable tokens** for the same work. |
| **super-thinker** | fable | Top-tier pure reasoning for the hardest, highest-stakes calls — subtle trade-offs, intricate plans, where depth beats speed. No tools. |
| **thinker** | opus | Everyday deep reasoning over context you already have — trade-offs, planning, debugging-by-reasoning. No tools, pure thought. |
| **researcher** | sonnet | Mapping how a system works end-to-end — **and** external research via `WebSearch`/`WebFetch` (docs, APIs, specs, changelogs). Never dumps whole files or whole pages; returns an anchored, source-cited report. |
| **executer** | sonnet 4.6 | The coding workhorse. Implements a settled plan end-to-end — writes/edits code, runs the build & tests, fixes what it broke. Full tools incl. Edit/Write. |
| **simple-tasks** | haiku | Mechanical chores (commits, pushes, builds, file ops) **and** cheap multi-hop context-saving work. |
| **bug-hunter** | haiku | Hunting real defects in a listed set of files or a diff. Never edits, never comments on style. Returns findings inline — or writes a JSON shard and returns one status line, so you can fan many out across a whole codebase. |

The **`/subagents`** skill is the playbook: it gives the main agent the roster, briefing rules for each agent, and a decision guide for picking the right one — including the classic chain **researcher maps → thinker decides → executer builds, verifies & commits**. Run your session as the **director** and it applies that playbook itself, on every turn, without you having to.

## Install

```
/plugin marketplace add Jose-Ribeir/delegate-kit
/plugin install delegate-kit
/delegate-kit:setup
```

The third step is required, and takes one prompt. `/delegate-kit:setup` detects which optional
integrations this machine actually has, asks you to confirm, and writes the seven composed agent
definitions to `~/.claude/agents/`. **Start a new session afterwards** — agent definitions load at
session start.

Re-run it whenever you add or remove an MCP server, or upgrade the plugin.

You then have:

- the **`/delegate-kit:subagents`** skill (the playbook),
- **`/delegate-kit:director on`** — make this project's sessions start as the orchestrator,
- **`/delegate-kit:run <objective>`** — hand off a single objective from a normal session, and
- six spawnable specialists: `thinker`, `super-thinker`, `researcher`, `executer`, `simple-tasks`, `bug-hunter`.

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
- *"Sweep the four files we just touched for bugs."* → **bug-hunter** returns only demonstrable defects — each with a verbatim snippet, a concrete failure scenario, and a severity. Point several at different batches to review a whole subsystem at once.
- *"Add rate limiting to the public API routes."* → **director** — a multi-stage objective, so it runs the whole chain itself and comes back with one report.

### Running your session as the director

This is the main way to use it. **The director is the agent you talk to** — you start your session
as it, and from then on every read, grep, edit, test run and commit happens in a worker's context
instead of yours. Two ways in, depending on where you work:

**Any surface — a project setting.** In the project you want to direct:

```
/delegate-kit:director on
```

That writes `{ "agent": "director" }` into `.claude/settings.local.json`, and every new session
opened on that folder starts as the director. `/delegate-kit:director off` removes it; with no
argument it just tells you whether it's on — worth knowing, because this is a property of the
folder, and it is easy to forget you left it running. (The key is the whole mechanism; you can edit
the file by hand instead. Put it in `.claude/settings.json` rather than the `.local` one only if you
want everyone who clones the repo to get director mode too.)

**Terminal — a one-session flag.** From the CLI, when you want it for this session only and want no
residue afterwards:

```
claude --agent director
```

There is no per-session equivalent in the **Claude desktop app** — the agent is chosen at launch,
from a flag or from settings, and the app gives you no launch flag to pass. So on the desktop the
question isn't *when do I direct*, it's *which folders are director folders*. Pick the projects with
real multi-step work in them; use `/delegate-kit:run` (below) everywhere else.

Either way, your session now has exactly one tool — the ability to spawn the rest of the fleet. You
talk to it normally; it decides, delegates, and reports. Because it holds the conversation, the
knowledge of the work accumulates across turns the way it would with any main agent — it just never
spends context on file contents to get there. To confirm it took, ask the new session to read a
file: a director will spawn a worker rather than reading anything itself.

### Handing over one objective instead

When you're already in a normal session and want to give away a single bounded objective without
turning the whole project over:

```
/delegate-kit:run --plan-only "Add rate limiting to the public API routes"
/delegate-kit:run "Add rate limiting to the public API routes"
```

(`--plan-only` fans out researchers and returns the approach and delegation map, changing nothing.)

This stacks a director *underneath* your main agent, so you pay for both — measured at ~64% more
than running as the director directly. Read that premium as **per objective, not per session**: it
is the price of one hand-off, not a running cost. If you direct a handful of times a day, that is
far cheaper than living in a folder where every quick edit has to go through a worker — which makes
this the sensible default in the desktop app, and the reason to reserve `/delegate-kit:director on`
for projects that genuinely earn it.

Either way the nested spawns show up live as child tasks, so it isn't a black box, and you get one
consolidated result. The shape of it (illustrative):

```
Outcome:  rate limiting live on the 6 public routes
Decided:  token bucket in middleware, Redis-backed — rejected per-route (3 duplicate impls)
Changed:  src/middleware/rateLimit.ts (new), src/app.ts:41, config/limits.json — commit a3f8e21
Verified: typecheck ok, 14 tests pass, bug-hunter clean over the 3 touched files
Open:     no limit configured for /webhooks — needs a product call
Ledger:   .delegate-kit/runs/ratelimit-k4b9/
```

The **ledger** is where the full research reports and findings live — the director never loads them
into its own context, it just passes the paths between agents. Add `.delegate-kit/` to your
`.gitignore`.

The mental model: **`super-thinker` is the advisor you brief; `director` is the lead you hand the
objective to.** Below about three delegation hops, skip the director and spawn the specialist
directly — otherwise it's a Fable-priced middleman.

### What it saves — measured

The whole premise is that Fable tokens should buy judgment and nothing else. So the question worth
measuring is narrow: **for the same objective, how many Fable tokens does directing cost versus
doing it yourself?**

**Setup.** A 5-module Python checkout service with a real cross-module bug (sales tax computed on
the pre-discount base, so members were overcharged) and 2 of 4 tests failing — fixing it needs
`checkout.py` → `tax.py` → `pricing.py` → `config.py` traced. Identical objective, pristine copy of
the repo per run, headless, **two runs per arm**. Both arms are Fable at the top level: arm A is a
normal full-tool Fable session, arm B is `claude --agent director`.

| | Fable, doing it itself | Fable, as director | |
|---|---|---|---|
| **Fable tokens** | 203,876 | **51,189** | **−75%** |
| ...net of the session floor | 156,742 | **37,176** | **−76%** |
| Turns | 4.5 | **3.0** | −33% |
| **Dollar cost, everything** | $0.6625 | $0.7428 | **×1.1** |
| Total tokens, all models | 203,876 | 462,814 | ×2.3 |
| Wall clock | 54.1s | 155.8s | ×2.9 |
| Correct fix, suite green | 2/2 runs | 2/2 runs | — |

**Three quarters of the Fable tokens, for 12% more money.** The work didn't disappear — the fleet
did 2.3× the raw token volume — it moved onto models that cost a fraction as much per token. That
is the entire trade, and on this task it very nearly pays for itself in cash alone before you count
the context you got back.

Two things that make up the 75%:

- **A tool-starved agent starts cheaper.** A cold full-tool session costs **47,134** tokens before
  it does anything; the director's costs **14,013**. Tool schemas dominate a cold system prompt, and
  the director has one tool. That's ~33k tokens saved on turn one of every session, and it's why the
  floor-adjusted figure (−76%) barely differs from the raw one.
- **The ledger keeps payloads out.** Each run left ~170 lines of `diagnosis.md` and `fix.md` on
  disk. None of it entered the director's context — it passed *paths* between agents and decided
  from ≤15-line digests.

**What it costs you: latency, ×2.9 here.** Delegation is round trips, and round trips are wall
clock. If you're watching and waiting, that's the real price — not the money.

**When to skip it.** Below about three delegation hops the fixed per-spawn overhead stops
amortizing and you're paying a middleman for nothing; ask the director for a one-file change and it
will spawn an executer to do what it could have described faster. And the `/delegate-kit:run`
hand-off path stacks a director under your existing main agent, so you pay for both — that measured
**$1.21 vs $0.74**, ~63% more than just running as the director. Fine for a one-off; switch modes
if you're doing it all session.

*Stated plainly: n=2 per arm, one task, one repo. It's the shape of the trade, not a benchmark
suite. Per-spawn overhead is largely fixed, so larger objectives amortize it better than this one
did — and a one-hop task will invert it.*

## Integrations — the agents are wired for what you actually have

Out of the box the agents navigate with built-in `Grep` / `Glob` / `Read` / `Bash` (plus
`WebSearch` / `WebFetch` for the `researcher`). That works everywhere and is a complete, honest
setup on its own.

`/delegate-kit:setup` can additionally wire them for tools you have installed. Each integration is a
**fragment** — a small file declaring the tool grants it adds and the prose that teaches an agent to
use them. Three ship with the plugin:

- **[Serena](https://github.com/oraios/serena)** — MCP server for symbol-level navigation (read one
  symbol, not a whole file). Applies to `executer`, `researcher`, `simple-tasks`.
- **graphify** — flow/structure knowledge graphs (`graphify query/path/explain`). Applies to
  `researcher`; adds prose only, since it runs through Bash.
- **python** — the Python defect taxonomy (asyncio, FastAPI, SQLAlchemy, plus Python-only traps)
  for the `bug-hunter`. Applies to `bug-hunter`; adds prose only. It deliberately declares **no**
  auto-detection: `python` on your `PATH` says nothing about whether the code you review is Python,
  so setup lists it unselected and asks rather than guessing.

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

Two rules worth knowing when you write one. `agents:` is what limits where a fragment applies —
**omit it and the fragment applies to every agent**, including the ones with no tools. If such a
fragment grants tools, composition fails loudly (an agent would end up holding tools it was never
taught to use); if it's prose-only, the prose is skipped for the templates that have no splice
marker and you get a `note:` telling you which. Either way, setting `agents:` is the fix.

## Customizing

- **Spawn depth.** Run as the session agent, the director sits at depth 0 and its workers at depth 1 —
  well inside Claude Code's default maximum spawn depth of 3, and no different from any normal
  session. The `/delegate-kit:run` hand-off path needs depth 2 (main → director → workers), still
  inside the default. If a director ever reports it has no way to spawn, raise
  `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`. Nothing else in the kit can recurse: the other six declare
  explicit `tools:` lists with no spawn tool.
- **Models** are pinned per agent in `templates/agents/*.md` frontmatter (`model:`). Change any to a
  model you have access to — e.g. if you don't have `fable`, set `super-thinker` (and `director`) to
  `opus`; the director's design rests on its tool starvation, not on the specific pin. `executer`
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
│   ├── director.md
│   ├── thinker.md
│   ├── super-thinker.md
│   ├── researcher.md
│   ├── executer.md
│   ├── simple-tasks.md
│   └── bug-hunter.md
├── integrations/            # optional fragments (tool grants + prose)
│   ├── serena.md
│   ├── graphify.md
│   └── python.md
├── bin/
│   └── compose.py           # templates + fragments -> ~/.claude/agents
├── commands/
│   ├── setup.md             # /delegate-kit:setup
│   ├── run.md               # /delegate-kit:run — hand an objective to the director
│   └── director.md          # /delegate-kit:director — director mode on/off for a project
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
