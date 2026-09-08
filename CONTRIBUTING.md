# Contributing to delegate-kit

Thanks for helping improve delegate-kit! This project is intentionally **small and curated** — the value is a tight roster and a clear playbook, not a sprawling catalog. Contributions that keep it sharp are very welcome.

## Good contributions

- **Sharper briefing guidance** in `skills/subagents/SKILL.md` — clearer rules for when to pick each agent, better briefing templates.
- **A new specialist agent** that fills a real gap the current seven don't cover. Open an issue first to discuss the gap.
- **Portability fixes** — anything that assumes a specific OS, shell, model, or MCP server should degrade gracefully.

## Ground rules

- **Stay portable.** Agents must work in **Standard mode** (built-in `Read`/`Grep`/`Glob`/`Bash`) with **zero setup**. Optional tools like Serena or graphify may *enhance* an agent (Power mode) but must never be *required*.
- **No hard-coded personal or private tooling.** No references to private MCP servers, internal services, or a single person's environment.
- **Keep models overridable.** Pin a sensible default in frontmatter, but assume users may not have that exact model.
- **Cross-platform commands.** Don't assume bash-only or PowerShell-only.

## Adding an agent

1. Create `templates/agents/<name>.md` with `name`, `description`, `tools`, and `model` frontmatter. (The plugin ships no top-level `agents/` directory by design — definitions are composed from these templates into `~/.claude/agents/` by `/delegate-kit:setup`.)
2. Write the `description` for triggering: primary use case first, in the third person, with the words a user would naturally say.
3. Add a row to the roster table and a section in `skills/subagents/SKILL.md`.
4. Test that it spawns and behaves in a project **without** any optional MCP servers installed.

## Testing your change

Install your fork as a local marketplace and verify in a clean session:

```
/plugin marketplace add <your-fork>
/plugin install delegate-kit
/reload-plugins
```

Confirm `/delegate-kit:subagents` loads and each agent is spawnable.
