---

# Campaign

- repository: `https://github.com/openssl/openssl`
- revision: `2924476b5591e691e904c4baf57894c526c4b8de`
- objective: `wrap`

# Sub-campaigns

## `libcrypto-public-api`

- subsystem: libcrypto public API
- implementation-paths: derive from the libcrypto build definition
- api-headers: derive from the libcrypto public headers
- coverage: user-selected subset
- named-items: decide with the user
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
  - milestone: decide with the user
    model: ask the user

# Execution

- mode: ask whether to run autonomously or pause after the sub-campaign

# UB audit

- run: only with explicit user approval
- model: ask the user if enabled

# Benchmark metadata

- orchestrator-backend: `codex`
- orchestrator-model: `gpt-5.6-sol`
- billing: `api`
- setup-approval: required
- results-path: `<repo-checkout>/crustify/wrappers-results.md`
- results-template: standard

# Notes

Exclude libssl from this campaign. The normal deterministic
`crustify-audit unsafe` checks remain enabled independently of agentic review.
