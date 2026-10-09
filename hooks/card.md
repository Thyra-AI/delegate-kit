Usage notes from the delegate-kit plugin (installed by the user; shown at session start):
1. Do chunks of 5 or fewer tool calls yourself; delegate bigger chunks.
2. Size each executer brief to about 30 tool calls and under 15 files. For larger work, or "build it, then apply it everywhere" work, run the partitioner first.
3. If an executer reports SIZE-HANDBACK, spawn a fresh executer with its remaining list. Never resume it.
4. Push only from this session, as a background Bash command (run_in_background), and don't poll it.
5. Messages that begin "delegate-kit context meter" come from this plugin's hooks and carry measured numbers; follow them.
If you have no file tools (director mode), ignore rules 1 and 4. Full playbook: the delegate-kit:subagents skill.
