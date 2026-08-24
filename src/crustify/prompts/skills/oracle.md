<!-- SKILL -->

Additional role guidance for a Crustify translator:

- Treat the wave worklist as fixed. Use oracle queries to understand an
  item and submit ownership findings, never to expand the scheduled batch.
- Read `query {types|symbols|dag} --help` before the first query and submit
  findings only through `--update`.
- The submitted wave document is orchestrator state. Do not edit or regenerate it from an
  agent worktree.
