# crustify-orchestrator

- Skill name: crustify-orchestrator
- Doc path: ../../../../docs/orchestrator-playbook.md
- Description: How to drive crustify end to end, in two phases. Setup: toolchain
  install through the first commit of the initial Rust tree — authoring
  `build.json`, `cli-config.json`, `crates.json` and a target's `oracle-config.json`, building
  the CodeQL database and extracting the T1/T2 tables, crate placement and crate
  shells. Translation: spawning raw lifetime discovery, landing waves, scanning
  them with `crustify-audit`, then promoting and guarding the result. Read
  Setup before any wave; every later stage reads what it produces. Read the
  referenced procedure in full before acting.

Orchestrator-facing. It is not in the translator capability catalog: a
translate agent never authors config or plans a wave, so setup instructions in
its context are pure waste. Only the orchestrator prompt indexes this.
