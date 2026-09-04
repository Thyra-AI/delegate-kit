---
name: run
description: Hand a whole multi-stage objective to the director — the orchestration agent that spawns the rest of the fleet and returns one verified result. Use for objectives with several delegation hops (investigate → decide → build → verify) that you don't need to steer step by step. Add --plan-only to get the plan and delegation map without changing anything.
argument-hint: "[--plan-only] <objective>"
allowed-tools: Task, Agent, Bash
---

# delegate-kit — run

Spawn the `director` with a well-formed brief for this objective:

> $ARGUMENTS

**This is the hand-off path, not the main one.** The director is normally the agent the user talks
to (`claude --agent director`, or `{ "agent": "director" }` in settings). This command exists for
when they're already in a normal session and want to give away one bounded objective without
switching. It stacks a director underneath the current main agent, so both are billed — measured at
~63% more than running as the director outright. If the user reaches for this repeatedly in one
session, say so and suggest they start as the director instead.

## 1. Read the flags

- **`--plan-only`** — the director investigates and plans, but changes nothing. It may spawn
  `researcher` and `simple-tasks` (read-only work) and must not spawn `executer`. It returns the
  approach it would take and the delegation map, and stops. Strip the flag before using the rest
  as the objective.
- No flag — full execution, including code changes and commits by the executers it spawns.

If the objective is empty after stripping flags, ask what it is rather than spawning anything.

## 2. Sanity-check the objective before spending anything

The director is the most expensive agent in the kit. Spawn it only when the objective earns it —
roughly **three or more delegation hops** and no need for the user to approve each step. If the
request is really a single hop ("what does X do", "fix this typo", "commit this"), say so in one
line and route it directly to `researcher` / `executer` / `simple-tasks` instead. Getting this
wrong costs the user real money for a middleman they didn't need.

Also check `.claude/agents/director.md` or `~/.claude/agents/director.md` exists. If it doesn't,
`/delegate-kit:setup` hasn't been run on this machine (or predates the director) — say that
rather than falling back to a generic agent.

## 3. Establish the run root

Get the absolute project root (`git rev-parse --show-toplevel`, falling back to the cwd). The
director has no tools and cannot work this out for itself — every brief it writes depends on
being handed it.

## 4. Spawn the director

One `Task`/`Agent` call, `subagent_type: director`, **no explicit `model`** (that would override
its pin). The brief must contain:

- **The objective**, verbatim from the user, plus any context from this conversation the director
  would otherwise have to rediscover — decisions already made, constraints, things already tried.
  It cannot read the transcript; what you don't tell it, it will pay a researcher to find out.
- **The absolute project root.**
- **The mode** — `--plan-only` (read-only, no executers, return the plan) or full execution.
- **The verification bar**, if the user implied one; otherwise say it should scale verification to
  blast radius.
- **The ledger location** — `<root>/.delegate-kit/runs/`, and that the run directory it picks under
  there should be reported back.
- **Anything the user must approve** — if the objective touches something irreversible or
  outward-facing (a push, a deploy, a migration, a delete), tell the director explicitly to stop and
  return before that step rather than doing it.

Don't pad the brief. The director's job is to find things out; yours is to state the objective
precisely and hand it the pointers you already have.

## 5. Relay the result

The director returns one consolidated report — outcome, decisions, changes, verification, open
items, ledger path. Pass it through faithfully:

- Don't re-verify its work by re-reading the files it touched — that spends your context on what
  you just paid to avoid. If a specific claim looks wrong, check that one claim.
- **Surface failures and open questions first.** If it bounced a question back with a
  recommendation, that's the headline, not a footnote.
- Give the user the **ledger path** — the full research reports and findings are on disk there,
  and reading one is much cheaper than asking the director again.
- If it stopped on budget with work remaining, say what remains and offer to run the next slice.

If the run produced a ledger directory, mention that `.delegate-kit/` belongs in `.gitignore` if
it isn't there already.
