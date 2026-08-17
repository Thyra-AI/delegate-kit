---
name: subagents
description: "Roster of the specialized subagents the main agent can spawn, and when to use each. Five types: thinker (pure reasoning, no tools), super-thinker (pure reasoning, top-tier model, no tools — for the hardest calls), researcher (flow-mapping, never dumps whole files), executer (the coding workhorse — writes/edits code, runs the build & tests, on an efficient model), simple-tasks (mechanical chores incl. commits/pushes AND cheap multi-hop context-saving work). Use when deciding how to delegate work — pick the right agent, give it a brief sized to its job. Also covers fleet mechanics: pointers-not-payloads briefs for tool-having agents (packed context is for tool-less ones only), spawning fresh vs. the compounding cost of resuming via SendMessage, parallelizing independent agents, and a verification bar scaled to blast radius. Offload implementation to executer and many-hop low-judgment work to simple-tasks to keep the expensive main context clean."
---

# /subagents

The roster of specialized subagents you can spawn, what each is for, and how to brief it. Use this to route work to the right agent instead of defaulting to a generic one. Each is a real `subagent_type` (composed into `~/.claude/agents/` by `/delegate-kit:setup`) with its tools and model already pinned — you don't set the model, just pass `subagent_type` and a good prompt. Passing an explicit `model` overrides the pin and can only express coarse aliases, so it silently defeats the point; leave it off.

If a `subagent_type` below doesn't exist, `/delegate-kit:setup` hasn't been run on this machine — say so rather than falling back to a generic agent.

## The roster

| Agent | Model | Tools | Use it for |
|-------|-------|-------|------------|
| **thinker** | opus | none | Deep reasoning over context you already have: analysis, trade-offs, planning, debugging-by-reasoning, hard decisions. |
| **super-thinker** | fable | none | Same as thinker, but the top tier — reach for it when the reasoning is hardest or the call is highest-stakes and you want maximum depth. |
| **researcher** | sonnet | Read/Grep/Glob/Bash (+ Serena & graphify when installed) | Understanding how something works: the full flow through the system and the nodes involved. |
| **executer** | sonnet 4.6 | Full: Edit/Write, Bash, Read/Grep/Glob (+ Serena when installed) | Implementing a settled plan end-to-end: write/edit code, run the project's build & tests, fix what it broke, report verified results. |
| **simple-tasks** | haiku | Read/Grep/Glob/Bash (+ Serena when installed) | Mechanical chores (commits, pushes, commands, builds, file ops) **and** multi-hop low-judgment work — chains of dependent steps that would otherwise burn the expensive main context. |

> **Two modes for researcher, executer & simple-tasks.** They work with **zero setup** using built-in `Read`/`Grep`/`Glob`/`Bash` (Standard mode). If the project has [Serena](https://github.com/oraios/serena) (symbol-precise navigation) and/or graphify (flow/structure graphs), they automatically prefer those for cheaper, more precise navigation (Power mode). Nothing to configure either way.

---

## thinker — the reasoning engine

**What it is:** pure thought. It reasons only over the context in your prompt and has **no tools** — it cannot read files, search, or run anything.

**Use when** the hard part is *thinking*, not *finding*: weighing trade-offs, untangling a tricky decision, reasoning through a bug from facts you already have, designing an approach in your head.

**Do NOT use it** to gather information — it can't. If facts are missing, get them (researcher) first, then hand them to thinker.

**Briefing rule — pack the context.** thinker only knows what you tell it. Put every relevant fact, constraint, code snippet, and option into the prompt. A thin brief gets thin reasoning. Ask it for a conclusion *and* the reasoning path. (This is a *no-tools* rule — it applies to thinker and super-thinker only. Agents with repo access get pointers, not payloads; see [Running the fleet](#running-the-fleet).)

## super-thinker — the top-tier reasoning engine

**What it is:** the same pure-thought engine as thinker, but running on **Fable** — the strongest reasoning model. Identical constraints: **no tools** (can't read, search, or run), reasons only over the context you hand it. (No access to Fable? Change the agent's `model:` field to one you have.)

**Use when** the reasoning is genuinely hard or the call is high-stakes and you want maximum depth: a subtle architectural trade-off, an intricate multi-factor decision, a gnarly bug you must reason out from the facts, a plan where a wrong call is expensive. When plain thinker would do, use thinker; reach for super-thinker when the extra reasoning depth is worth it.

**Briefing rule — same as thinker, and it matters even more here.** super-thinker only knows what you tell it, so pack every relevant fact, constraint, snippet, and option into the prompt. The deeper the model, the more it rewards a complete brief — and the more a missing fact quietly caps the quality. Ask for a conclusion *and* the reasoning path. (Like thinker's, this is a *no-tools* rule — it does not generalize to agents that can read the repo themselves.)

## researcher — the flow mapper

**What it is:** the research specialist. It maps how a system works — the end-to-end flow and the symbols/modules/services (nodes) involved.

**The defining constraint:** it **never dumps a whole file.** In Power mode it navigates with **graphify** (`query` / `path` / `explain`) for flow and structure and **Serena symbol tools** for targeted reads; in Standard mode it uses **Grep** to locate and **Read** for targeted line ranges only. Either way it locates precisely and reads just what it needs, which keeps research cheap and focused.

**Use when** you need to understand something before acting: "how does X flow end to end", "where does Y live and what touches it", "trace the path from A to B".

**Briefing rule — name the target and the question, and include the project root.** Tell it exactly what to understand and what to return, and pass the project root (e.g. `cwd: /path/to/project`) so it scopes its navigation correctly (and, in Power mode, points graphify/Serena at the right project). It comes back with a structured flow report: ordered path, key nodes, connections, and `path:line` anchors for everything — so you (or the next agent) can act from the report alone.

## executer — the coding workhorse

**What it is:** the primary implementation agent — the main hands behind coding. Full tools including Edit/Write, running Sonnet 4.6 (deliberately: Sonnet 5 burns far more tokens for the same code, making it worse cost/perf than even Opus for this role; 4.6 is the efficient workhorse). Hand it a settled plan or a described change and it builds it end-to-end: reads the surrounding code, matches the repo's conventions, edits, runs the project's own build/tests/typecheck, fixes what it broke, and reports back verified results with verbatim output for anything that failed.

**Use it when** the decision is made and code needs to exist. Any implementation work you'd otherwise do inline — feature code, bug fixes, refactors with a known shape, wiring a spec into the codebase — goes to the executer so your own context never gets spent on the edit–run–fix loop.

**The boundaries.** Versus **simple-tasks** — simple-tasks executes a known route with zero judgment and never edits code; the executer implements a *described change* and makes the normal small calls a competent engineer makes (structure, reuse, edge cases, style). Rule of thumb: if the briefing is a command list, that's simple-tasks; if it's a change description, that's executer. Versus **thinker / super-thinker** — they decide, the executer builds; it will not re-open settled architecture, and if it hits a genuinely load-bearing ambiguity it stops and bounces the question back with a recommendation instead of guessing. It **commits the work it implements** (with the message you give it, pushing only when asked); reach for simple-tasks for standalone git chores that aren't tied to an implementation.

**Briefing rule — the change, the settled decisions, and the verification bar; pointers, not payloads.** State what to build and why, and name the decisions that are already made so it doesn't re-open them. Point it at the entry files or symbols — it has full repo access and reads the surrounding code itself, so don't paste in what it can open; anchors (`path:line`, symbol names) beat transcribed code. Size the brief to the change: a three-line fix needs a few lines of brief (what, where, done-when) — an 80-line spec for it costs more than the change and adds nothing. Define "done" proportional to blast radius (see [Scale verification to blast radius](#scale-verification-to-blast-radius)) — don't demand the full suite for a cosmetic fix. Don't hand it open design questions — settle those with a thinker first, or expect them bounced back. Pass the project root (e.g. `cwd: /path/to/project`) so it works in the right place.

## simple-tasks — the runner

**What it is:** the cheap, fast hands. It executes mechanical, well-defined work and reports back condensed but complete. It runs all the shell/CLI work and **fully owns git, including commits and pushes.**

**Use it for two kinds of work:**
1. **Clear mechanical chores** — commit & push, run a build, move files, run a script. The thinking is already done; this is execution.
2. **Multi-hop, context-saving work** — tasks that take many sequential, dependent tool calls (read a value → follow it to the next file → gather a fact across a dozen places → run a command and act on its output). This is the *main reason to reach for it even when the route isn't fully known*: each hop's tool output lands in **its** context, not yours, so the expensive main context stays clean. Anything low-judgment that would otherwise have you make 15 reads/greps in a row is a candidate to offload here.

**The line that matters:** simple-tasks does *finding and doing*, never *deciding*. Navigation, collection, and mechanical execution toward a clear goal — yes. Design calls, correctness judgments, choosing between approaches, or guessing intent — no; for those it must stop and report. If the hard part is *thinking*, that's **thinker**; if it's *understanding a flow to brief someone*, that's **researcher**.

**Briefing rule — give it the goal, the route, and the project root.** For pure chores, hand it a complete ordered route with exact paths/commands — the more precise, the more reliable. For multi-hop work, give it a crisp goal, the starting point, and the stopping condition (what "done" looks like, what to bring back); it will chase the trail itself. For commits, give the exact message (verbatim, with any required trailer) and what to stage. Pass the project root (e.g. `cwd: /path/to/project`) so it operates in the right place. Always tell it to stop and report if it hits a real decision point.

**Reporting contract:** it reports condensed but with all the important content — commands run, exit status, commit SHA, files changed, and for gather tasks the **findings with exact `path:line` anchors** so you can act without re-walking the trail. It pastes **verbatim output for anything that failed**, and must never claim success for a command it didn't actually run and verify.

---

## Picking the right one

- Need to **think** about what you already know → **thinker** (or **super-thinker** on Fable when the call is hardest / highest-stakes).
- Need to **find out / understand** how something works, with a flow report to brief the next step → **researcher**.
- Need to **build** something — write or change code and verify it — once the approach is settled → **executer**.
- Need to **do** a clear mechanical task, **or** grind through many low-judgment hops to keep your own context clean → **simple-tasks**.

**Default to offloading.** If a task would cost *you* (the expensive main agent) a long string of reads/greps/commands or a grind of edit–run–fix cycles, hand it off — implementation to **executer**, low-judgment hops and chores to **simple-tasks** — with a clear goal and stopping condition, even if you can't pre-write every step. The work burns their cheaper context instead of yours, and you get back a condensed report. Reserve your own context for the decisions only you can make.

**executer vs. simple-tasks:** if the briefing is a *change to implement* (needs ordinary engineering judgment and code edits), that's **executer** — and it commits the work it implements. If it's a *command list or mechanical route* with no design calls — including standalone git chores not tied to an implementation — that's **simple-tasks**.

**researcher vs. simple-tasks for multi-hop:** pick **researcher** when the output is *understanding* — a structured flow map of how a system works. Pick **simple-tasks** when the output is *a concrete result or collected facts* and the path is mechanical (gather all X, apply Y everywhere, run Z and report).

---

## Running the fleet

Routing picks the right agent; these rules keep the fleet fast. In practice, ignoring them — not mis-routing — is where delegation time actually goes.

### Brief for the agent's tools, sized to the job

The roster splits in two. **Tool-less agents (thinker, super-thinker)** know only what's in the prompt — for them, pack the context: every fact, constraint, snippet, and option. **Tool-having agents (researcher, executer, simple-tasks)** can read the repo themselves — give them *pointers, not payloads*: the goal, the settled decisions, and exact anchors (`path:line`, symbol names) to start from. Don't transcribe code they can open.

Then scale length to the job, not to the agent's capability. A three-line fix needs a three-line brief; writing a spec for it costs more than the change. Save the long brief for work that earns it — many settled decisions, subtle constraints, a wide surface. "More context is always better" is true only for the tool-less agents.

### Spawn fresh by default; resume sparingly

You can continue a previous agent with `SendMessage`, and it keeps its whole transcript — that continuity is exactly the cost: **every resume re-processes the entire accumulated transcript before any new work starts**, so a long-lived agent gets slower and more expensive with every exchange. A deep transcript (100k+ tokens) can take minutes just to chew through before the first new tool call.

Resume only when the agent holds state that is genuinely expensive to rebuild: it's mid-implementation with half-applied changes, or you're drilling one follow-up into a research thread it just built. If you can restate what the next task needs in a paragraph or two, **spawn fresh with that distilled brief** — a new agent with distilled context beats an old agent with all of it, almost every time. Re-apply that test before *each* resume, not just the first: the exceptions are one-shot follow-ups, and past the second exchange with the same agent you should distill what it knows and respawn. Never keep a "pet" agent as the default channel for a stream of loosely related tasks; that converts every small task's cost into the whole history's cost.

### Parallelize independent work

Agents launched in a single message run concurrently — so when units of work don't depend on each other's output, **send them together**: three researchers mapping three subsystems, one executer per independent fix, the researcher mapping the *next* piece while an executer builds the current one. Serializing independent agents is pure wall-clock waste.

Serialize only along real data dependencies. researcher → thinker → executer is a dependency chain *within one topic*; separate topics run their chains side by side, and stages can pipeline across topics. One caution: don't point parallel executers at the same files — they will collide. Split by disjoint areas, or serialize within one. And since each executer commits its own work, parallel executers in the same repo can still race at the git layer: tell each to stage explicit paths only (never `git add -A`), or hold the commits and hand them to one simple-tasks agent afterward.

### Scale verification to blast radius

The "done" bar you set in an executer brief should cost in proportion to what the change can break. A docstring or comment fix needs at most a typecheck/build. A localized code change needs the tests nearest it plus typecheck. Only a cross-cutting change — shared helper, public interface, config — earns the wide suite. When the brief is silent, the executer defaults to typecheck/build plus the tests nearest the change, widening when cheap; the bar you set in the brief governs, and it can scope that down for a cosmetic edit or up for a cross-cutting one — just never past the blast radius. Demanding the full suite for a docstring fix is the classic self-inflicted slowdown.

### The chain, run well

A common chain: **researcher** maps the flow → **thinker** reasons over those findings to decide the approach → **executer** implements, verifies, and commits the change. The chain is sequential within a topic, but nothing else has to wait on it: run other topics' chains beside it, and start the next piece's research while this piece is being built. Spawn each agent fresh with a brief sized to its job; pass concrete pointers (paths, anchors, the exact route) so it doesn't re-discover what you already know.
