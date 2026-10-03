# Privacy

delegate-kit is a local Claude Code plugin: slash commands, subagent definitions, a skill, a
hook and a few Python scripts. It has no server, no account, and no telemetry.

**Data collection.** delegate-kit does not collect, transmit or store any data about you
anywhere other than your own machine. Its scripts (`bin/`, `hooks/`) use only the Python
standard library and make no network requests; none of them opens a socket or imports an HTTP
client.

**What it writes, all locally**

- `~/.claude/agents/*.md` (and a `.delegate-kit-backup/` of anything it overwrote, plus a
  `.delegate-kit-stamp.json`) when you run `/delegate-kit:setup`.
- `~/.claude/delegate-kit/bin/` (`partition.py`, `bench.py`, `squash.py`, a `dk` launcher) when
  you run `/delegate-kit:setup`. Both locations move under `$CLAUDE_CONFIG_DIR` if you set it.
- `.claude/settings.local.json` in your project, only when you run `/delegate-kit:director on`
  or `off` (it adds or removes one `agent` key).
- `.delegate-kit/` in your project: run artifacts (plans, partition maps, caches) written by
  the director and the partition tooling.

**What it reads.** `bench.py` reads your Claude Code session transcripts under
`~/.claude/projects/` to compute per-agent token and cost figures. It prints them to your
terminal and does not send them anywhere.

**Your usage and Claude itself.** The subagents run through your own Claude Code session and
are subject to Anthropic's terms and privacy policy. Their prompts and tool results are
handled by Claude Code exactly like any other session, and they spend your own Claude usage.
The `researcher` agent has `WebSearch` and `WebFetch`, so a task you give it can fetch
web pages; the plugin's own code does not.

**Third parties.** None. The optional integrations (Serena, graphify) are tools you install
and configure yourself; delegate-kit only adds instructions for them.

Questions: open an issue at <https://github.com/Thyra-AI/delegate-kit/issues>.
