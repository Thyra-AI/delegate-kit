# dk — delegate-kit CLI ported to Kimchi

Port of the delegate-kit (Claude Code plugin) CLI into `.kimchi/bin`. Pure git+JSON
tooling, no Claude coupling. Sources: delegate-kit `bin/{partition,squash,events,compose,check}.py`
and `hooks/git_guard.py`.

## Subcommands — `sh .kimchi/bin/dk <cmd>`

| command   | ported file              | what it does |
|-----------|--------------------------|--------------|
| `partition` | `dk-core/partition.py` | `map/plan/check/show/worktrees/join/auto/calibrate` work partitioner (git+json only, ported as-is). Kimchi addition: optional WU field `delivers` groups clusters into reviewable **deliverables** (one branch/MR each, stacked ship-oldest-first) emitted as a `deliverables` section in the plan; auto-buckets by directory prefix otherwise (AUTO_DELIVERABLES), with soft DELIVERABLE_CONFLICT / DELIVERABLE_OVERSIZE (>8 primary files) warnings |
| `squash`    | `dk-core/squash.py`    | fold `[Wn]`-tagged commits into one commit per tag via `git rebase -i` |
| `events`    | `dk-core/events.py`    | summarise `state/events.jsonl` (needs a Kimchi size-meter hook to write it) |
| `compose`   | `dk-core/compose.py`   | splice integration fragments into delegate-kit agent templates → a Kimchi agents dir |
| `git-guard` | `dk-core/git_guard.py` | block destructive git commands (stash/rebase/checkout/…, `add -A`, any push) in subagent command strings |
| `bench`     | —                      | deferred: needs a Kimchi transcript adapter (Claude-format JSONL parser not ported) |

## Config

`KIMCHI_DK_DIR` (default `~/.local/share/kimchi/delegate-kit`) is the single base dir for
`calibration.json`, `state/`, templates and the ledger. The launcher sets it when unset;
every ported script honours it.

## Port notes

- `partition`, `squash`: ported as-is, paths re-based (`~/.claude/delegate-kit` → `$KIMCHI_DK_DIR`).
- `events`: re-based to `$KIMCHI_DK_DIR/state/events.jsonl`; same JSONL format.
- `compose`: writes into `--agents-dir` (default `<cwd>/.kimchi/agents`); keeps Claude template
  tool names as-is (no renaming); refuses to overwrite your existing custom agents unless `--force`;
  MCP detection re-scoped to `~/.config/kimchi/harness/mcp.json`.
- `git-guard`: standalone scanner over argv words, `$(...)`, backticks, `sh -c`/`bash -c` args;
  Claude hook payload and the PowerShell-tool mention dropped (any shell tool is scanned).
- `check`: skipped — its self-test embeds Claude-format transcript fixtures and `~/.claude` layout
  assertions and hard-depends on them (bench, out of scope, is its subject).
- `bench`: not ported — Claude transcript format adapter required.

`--selftest` is implemented by `partition` (full self-test incl. worktree/join against a scratch repo), `squash` (in-memory check of the folding logic, no repo), `events` (log parsing/summarise checks) and `git-guard` (196 rule cases); `compose` has none — its splice path is covered by the functional test suite.