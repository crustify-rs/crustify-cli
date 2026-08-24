---

# Campaign

- repository: `https://github.com/intel/cryptography-primitives`
- revision: `9d397ba62e2369b63171bc995e9c1179aaa5c0dc`
- objective: `wrap`

# Sub-campaigns

## `public-api`

- subsystem: whole published API
- implementation-paths: derive during setup
- api-headers: derive from the implementation paths
- coverage: whole public API, excluding the imported closure
- named-items: none
- translator-backend: `codex`
- translator-model: `gpt-5.6-sol`

# Workload

- batching: customize
- max-types: `2`
- max-syms: default
- max-loc: default
- parallel-max: orchestrator-selected

# Agentic review

- checkpoints:
  - milestone: campaign end, covering all translated output
    model: `gpt-5.6-sol`

# Execution

- mode: autonomous

# UB audit

- run: no
- model: not applicable

# Benchmark metadata

- orchestrator-backend: `codex`
- orchestrator-model: `gpt-5.6-sol`
- billing: `api`
- setup-approval: not required; Phase 1 is pre-approved
- results-path: `<repo-checkout>/crustify/wrappers-results.md`
- results-template: standard

# Notes

Phase 1 and Phase 2 are approved to run autonomously end to end. Record and git
track the results using the exact standard template.
