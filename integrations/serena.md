---
name: serena
title: Serena — symbol-precise code navigation
url: https://github.com/oraios/serena
detect_mcp: serena
agents: researcher, simple-tasks, partitioner
tools: mcp__serena__activate_project, mcp__serena__find_symbol, mcp__serena__get_symbols_overview, mcp__serena__find_referencing_symbols, mcp__serena__find_implementations, mcp__serena__find_declaration, mcp__serena__search_for_pattern, mcp__serena__find_file, mcp__serena__list_dir, mcp__serena__get_diagnostics_for_file
tools@simple-tasks: mcp__serena__activate_project, mcp__serena__find_symbol, mcp__serena__get_symbols_overview, mcp__serena__search_for_pattern, mcp__serena__find_file, mcp__serena__list_dir
---

## Serena 1.5.3 — symbol-precise navigation

**Before your first Serena call**, call `activate_project(project="<absolute project root from your brief>")` once. Desktop sessions share one Serena server whose active project may belong to another session, so never assume it is yours. (If `activate_project` isn't offered, the project is preset; go on.) Never call `initial_instructions` — this section is all you need. If activation or a call fails, say so in your report and fall back to Grep **explicitly**; never claim a Serena result you didn't get.

**Routing.** Symbol questions (where is X defined, who calls X, what does a type contain) go to Serena first. Text and pattern questions (strings, config keys, log messages) go to Grep. Read targeted — `get_symbols_overview` or `find_symbol` with `include_body` for the one symbol — never the whole file.

**Exact parameters** (a wrong name fails the call):
- `find_symbol(name_path_pattern=..., relative_path=..., include_body=..., depth=...)` — the first is `name_path_pattern`, NOT `name_path`. Only it is required; `relative_path` narrows the search.
- `find_referencing_symbols(name_path=..., relative_path=...)` — here it IS `name_path`; both required, `relative_path` is the file defining the symbol.
- `get_symbols_overview(relative_path=...)` — a file's shape without reading it.
- `search_for_pattern(substring_pattern=..., relative_path=..., paths_include_glob=...)` — a regex over files.
- `find_implementations(name_path=..., relative_path=...)`; `find_declaration(relative_path=..., regex=...)`.
- `list_dir(relative_path=..., recursive=...)` — `recursive` is REQUIRED. `list_dir`, `find_file` and `read_file` exist only in the `desktop-app` Serena context (Claude Desktop sessions); CLI sessions run `--context ide` without them — use Glob and Read there.

## agent:simple-tasks

## Serena 1.5.3 — read with it, don't bulk-open files

**Before your first Serena call**, call `activate_project(project="<absolute project root from your brief>")` once (the server is shared and may point at another session's project; if the tool isn't offered, the project is preset). Never call `initial_instructions`. If activation fails, say so in your report and fall back to Grep **explicitly**; don't retry it more than once.

Symbol questions go to Serena, text and pattern questions to Grep; read one symbol, not the file:
- `find_symbol(name_path_pattern=..., relative_path=..., include_body=..., depth=...)` — `name_path_pattern`, NOT `name_path`.
- `get_symbols_overview(relative_path=...)`; `search_for_pattern(substring_pattern=..., relative_path=...)`.
- `list_dir(relative_path=..., recursive=...)` (`recursive` REQUIRED) and `find_file` exist only in Desktop sessions; in CLI sessions use Glob and Read.
