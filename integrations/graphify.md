---
name: graphify
title: graphify — codebase knowledge graph
detect_bin: graphify
agents: researcher
tools:
---

## graphify — start here for flow and structure

This project has **graphify** on PATH, which turns the codebase into a queryable knowledge graph. It needs no extra tool grant — you run it through Bash — and it is the cheapest way to get the *shape* of a system before you drill in.

- `graphify query "<question>"` — scoped subgraph answering a question.
- `graphify path "<A>" "<B>"` — how two things relate / the path between nodes.
- `graphify explain "<concept>"` — focused subgraph for one concept.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation. Read `graphify-out/GRAPH_REPORT.md` only for broad architecture when query/path/explain don't surface enough.

Lead with a `graphify query` to find the nodes that matter, then read only those symbols. If graphify returns nothing useful for this repo (no graph built yet), say so once and navigate with your other tools instead of re-running it with reworded questions.
