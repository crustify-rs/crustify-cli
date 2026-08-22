---

# Campaign

- campaign-repo: `https://github.com/openssl/openssl`
- campaign-revision: `2924476b5591e691e904c4baf57894c526c4b8de`
- campaign-objective: `wrap`
- campaign-surface: public API of libcrypto
- excluded-surface: libssl
- surface-coverage: subset selected with the user
- wave-planning: collaborative
- setup-approval: required
- campaign-autonomy: decide with the user
- orchestrator-backend: `codex`
- orchestrator-model: `gpt-5.6-sol`
- translator-backend: `codex`
- translator-model: `gpt-5.6-sol`
- billing: `api`
- max-types: `2`
- max-syms: default
- max-loc: default
- parallel-max: orchestrator-selected
- review-pass: decide with the user
- audit-ub: explicit user approval required
- results-path: `<repo-checkout>/crustify/wrappers-results.md`
- results-template: standard

# Legend

- `campaign-*` and `*-surface` define the repository, objective, and requested
  coverage boundary.
- `wave-planning`, `setup-approval`, and `campaign-autonomy` control when the
  orchestrator pauses for user input.
- `orchestrator-*` selects the LLM that plans and supervises the campaign.
- `translator-*` selects the LLM used by each implementation agent.
- `billing` applies to both LLM roles unless separate values are provided.
- `max-*` controls deterministic oracle batching; `parallel-max` limits how
  many translator agents may run concurrently.
- `review-pass` may launch additional LLM translators with a review objective on prior waves.
- `audit-ub` may launch the agentic `crustify-audit ub` pass and therefore uses
  an LLM. The normal `crustify-audit unsafe` pass is deterministic.
- `results-*` selects the tracked report location and template.
