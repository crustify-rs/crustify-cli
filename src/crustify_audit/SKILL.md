# crustify-audit

- Skill name: crustify-audit
- Bin path: crustify-audit
- Doc path: docs/audit.md
- Description: Review the safety of Rust repositories, especially crates that
  wrap native libraries. The deterministic `unsafe` command reports compiled
  unsafe and raw-pointer surfaces and supports source-site queries seeded by
  type or symbol names. The agentic `ub` command investigates undefined
  behaviour reachable through safe APIs and produces reproducible advisories
  that trigger sanitizer in Miri, ASan/UBSan, and BorrowSanitizer.
  Read the referenced documentation before choosing a command.

`Doc path` is relative to this file. `Bin path` is the logical executable name
for a harness to resolve in its own environment.
