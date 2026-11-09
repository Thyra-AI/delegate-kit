# subagents — Kimchi native adaptation (linked addendum)

This file is the Kimchi-side companion to this skill (the playbook body is the original Claude Code
delegate-kit text). When you run inside Kimchi, follow this addendum for mapping and spawn semantics;
the playbook body's routing/briefing/reporting rules remain valid as-is. Verified empirically on
Kimchi harness v1.27.0 (2026-11-05); evidence in the port's investigation docs.

## Roster (Kimchi, open-weight)

| Role | Type | Model | thinking | Notes |
|---|---|---|---|---|
| thinker | custom `thinker` (project `.kimchi/agents/`) | `kimchi-dev/deepseek-v4.1-flash-1017` | high | zero file tools (full `disallowed_tools` blocklist); pure reasoning over packed prompt |
| super-thinker | custom `super-thinker` | `kimchi-dev/kimi-k3` | max | zero file tools; top-tier reasoning; K3 collapses below max |
| partitioner | custom `partitioner` | `kimchi-dev/glm-5.3-flash` | low | tools: read, grep, find, bash, write (no edit); writes wus.json/schedule.json; never edits source |
| researcher | built-in `Researcher` | `kimchi-dev/glm-5.3` | high | pass model+thinking on every spawn |
| executer | built-in `Builder` | `kimchi-dev/glm-5.3` | high | pass model+thinking on every spawn |
| simple-tasks | built-in `General-Purpose` | `kimchi-dev/deepseek-v4-flash-0731` | off | pass model+thinking on every spawn |
| bug-hunter | built-in `Reviewer` | `kimchi-dev/deepseek-v4.1-flash-1017` | high | pass model+thinking on every spawn |
| director | **no spawnable type** | — | — | subagents cannot spawn subagents (verified); the director role is the main session agent, which routes via this playbook |

Model/thinking alignment rationale (language-task evals, AA v5, LMArena, Terminal-Bench 3.0, DS-1017
effort study, as-of 2026-11-05): deepseek-v4.1-flash-1017 and glm-5.3 are the top released open-weight
models on AA Index v5; kimi-k3 leads human-preference evals; minimax-m3 excluded (non-commercial
license); all pins are open-weight (no Anthropic/OpenAI).

## Spawn contract (critical)

- **Custom types**: `thinking`, tool restriction and max_turns come from the frontmatter and ARE applied at runtime (verified in sidechain transcripts). The frontmatter `model:` is NOT honored by this harness version (verified: sidechain `model_change` showed the session model) — pass `model` per spawn for custom types too.
- **Built-in types**: there is no file to carry pins — the orchestrator MUST pass `model` and `thinking`
  on every spawn, or the child runs the current session model (which defeats the alignment).
- `[Wn]` tags: the first words of the Agent tool's `description` parameter (the kit's convention ports 1:1).
- Custom agents are discovered at **session start only**; after adding/editing `.kimchi/agents/*.md`,
  start a new session — otherwise the harness falls back to General-Purpose with an
  "Unknown agent type" note.

## Mechanics (verified)

- `tools:` absent/empty → full toolset; non-empty `tools:` → strict allowlist; full `disallowed_tools:`
  blocklist → zero tools. The Agent/spawn tool is NEVER granted to subagents.
- Agents support frontmatter: description, tools, disallowed_tools, model, thinking, max_turns,
  max_duration, token_budget, isolated, isolation (worktree|none), inherit_context, prompt_mode
  (replace|append), run_in_background, and more (parse-surfaced).

## Claude-ism → Kimchi mapping (read the playbook body with this in mind)

| Claude Code reference | Kimchi fact |
|---|---|
| Read/Grep/Glob/Write/Edit/Bash tools | `read`/`grep`/`find`/`write`/`edit`/`bash` |
| WebSearch/WebFetch | `web_search`/`web_fetch` |
| SendMessage (resume) | `resume_subagent` (spawn-fresh guidance still applies) |
| Task/Agent tool inside agents | `Agent` is main-session-only; no nested spawning |
| `claude --agent director`, settings agent pin | no equivalent; director = the session agent |
| `/delegate-kit:setup` / `:run` / `:director` | no slash commands; read this file + `.kimchi/agents/README.md` |
| `~/.claude/agents/` composition | `.kimchi/agents/*.md` (project) — or **`~/.config/kimchi/harness/agents/`** (user level, verified 2026-11-05; resolves from any cwd, immune to trust/worktree gates) | project dir discovered at session start only |
| `~/.claude/projects/.../*.jsonl` transcripts | `~/.config/kimchi/harness/sessions/<enc-cwd>/<ts>_<id>.jsonl`; one-shot CLI sessions don't persist |
| git-guard / context-meter hooks | no hook system; Kimchi session conventions govern pushes; `dk git-guard` (in `.kimchi/bin/`) can be invoked explicitly |
| Serena / graphify | not bundled; MCP route |
| fable/opus/sonnet/haiku pins | the open-weight table above |

## Ledger & tooling

- Ledger relay: `.delegate-kit/runs/<slug>/` (git-ignored) — payloads go through paths, digests through reports.
- `sh .kimchi/bin/dk` (ported dk CLI): `partition`, `squash`, `events`, `compose`, `git-guard`;
  `bench` deferred (needs a Kimchi transcript adapter). Set `KIMCHI_DK_DIR` to isolate state.

Known limitations: custom-agent `model:` frontmatter is NOT applied at runtime on harness v1.27.0 (sidechain `model_change` showed the session model) — always pass `model` per spawn; `thinking` pins verified applied; `prompt_mode: replace` assumed default; model facts drift (re-check before changing pins).