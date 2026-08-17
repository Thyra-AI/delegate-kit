---
name: serena
title: Serena — symbol-precise code navigation
url: https://github.com/oraios/serena
detect_mcp: serena
agents: executer, researcher, simple-tasks
tools: mcp__serena__activate_project, mcp__serena__initial_instructions, mcp__serena__find_symbol, mcp__serena__get_symbols_overview, mcp__serena__find_referencing_symbols, mcp__serena__find_implementations, mcp__serena__find_declaration, mcp__serena__search_for_pattern, mcp__serena__find_file, mcp__serena__list_dir, mcp__serena__get_diagnostics_for_file
tools@simple-tasks: mcp__serena__activate_project, mcp__serena__initial_instructions, mcp__serena__find_symbol, mcp__serena__get_symbols_overview, mcp__serena__search_for_pattern, mcp__serena__find_file, mcp__serena__list_dir
---

## Serena — activate before you use it

This project has [Serena](https://github.com/oraios/serena), so prefer it over grepping: it reads one symbol instead of a whole file.

**Setup, once per session, before any other Serena tool:** call `mcp__serena__activate_project` with the project path from your brief to point Serena at this project, then `mcp__serena__initial_instructions` to load the usage manual. If activation fails — e.g. there's no `.serena/project.yml` for this project — say so in your report and fall back to Grep **explicitly**. Never let a failed activation turn into silent grepping, and never claim a Serena result you didn't get.

Then navigate with `get_symbols_overview` (a file's shape without reading it), `find_symbol` (one symbol's body, not the file), and `find_referencing_symbols` / `find_implementations` / `find_declaration` to trace across files.

## agent:researcher

## Serena — activate before you use it

This project has [Serena](https://github.com/oraios/serena). It is your best navigation tool: symbol-precise reads instead of pattern-matching, which is exactly the bloat you exist to avoid.

**Setup, once per session, before any other Serena tool:** call `activate_project` with the project path from your brief, then `initial_instructions` to load the usage manual. If activation fails (e.g. no `.serena/project.yml` for this project), note it in your report and fall back to Grep **explicitly** rather than silently.

Then, for targeted reads:

- `get_symbols_overview` to see a file's shape without reading it.
- `find_symbol` (with the symbol path) to read one symbol's body, not the file.
- `find_referencing_symbols` / `find_implementations` / `find_declaration` to trace the flow across files.
- `search_for_pattern`, `find_file`, `list_dir` to locate things.

Prefer these over Grep for anything you can name. Grep stays the fallback for text you can only describe by pattern.

## agent:simple-tasks

## Serena — activate before you use it

This project has [Serena](https://github.com/oraios/serena). Use it for reading instead of bulk-opening files.

**Setup, once per session, before any other Serena tool:** call `mcp__serena__activate_project` with the project path from your brief, then `mcp__serena__initial_instructions` to load the usage manual — before `get_symbols_overview`, `find_symbol`, or anything else. If activation fails (e.g. no `.serena/project.yml` for this project), note that in your report and fall back to Grep **explicitly** rather than silently. Don't assume every project you run in has Serena configured, and don't retry a failed activation more than once — it's a fallback condition, not a blocker.
