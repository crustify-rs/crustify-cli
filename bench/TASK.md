---

# Campaign

- repository: `<repository URL>`
- revision: `<commit or tag>`
- objective: `<port | wrap>`

# Sub-campaigns

## `<subsystem name>`

- subsystem: `<directory, library, or named component>`
- implementation-paths: `<paths>`
- api-headers: `<paths | derive from implementation paths>`
- coverage: `<whole subsystem | named subset>`
- named-items: `<none | types and functions>`
- translator-backend: `<codex | claude>`
- translator-model: `<model>`

# Workload

- batching: `<defaults | customize>`
- max-types: `<default | number>`
- max-syms: `<default | number>`
- max-loc: `<default | number>`
- parallel-max: `<orchestrator-selected | number>`

# Agentic review

- checkpoints:
  - milestone: `<none | campaign milestone>`
    model: `<model>`

# Execution

- mode: `<autonomous | pause after each sub-campaign>`

# UB audit

- run: `<no | yes, with explicit user approval>`
- model: `<not applicable | model>`

# Benchmark metadata

- orchestrator-backend: `<codex | claude>`
- orchestrator-model: `<model>`
- billing: `<api | subscription>`
- setup-approval: `<required | not required>`
- results-path: `<repo-checkout>/crustify/wrappers-results.md`
- results-template: `standard`

# Legend

- `Campaign` answers which repository and revision the campaign uses and
  whether its top-level objective is `port` or `wrap`.
- Each `Sub-campaign` answers which subsystem is targeted, what constitutes its
  implementation and public API, whether coverage is whole or selected, and
  which translator backend and model execute it.
- `Workload` records whether live defaults or explicit scheduler limits apply.
- Every agentic review checkpoint names its own model. Deterministic
  `crustify-audit unsafe` checks do not need one.
- `Execution` controls whether the orchestrator pauses between sub-campaigns.
- `UB audit` remains off unless the user explicitly approves it.
- Waves and steps are internal scheduler artifacts and never user-facing
  manifest settings.
