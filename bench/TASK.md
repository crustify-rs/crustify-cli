---

# Campaign

- campaign-repo: `<repository URL>`
- campaign-revision: `<commit or tag>`
- campaign-objective: `<port | wrap>`
- campaign-surface: `<files, subsystem, or public API>`
- excluded-surface: `<none | files or subsystems>`
- surface-coverage: `<whole surface | user-selected subset>`
- wave-planning: `<collaborative | orchestrator-selected>`
- setup-approval: `<required | not required>`
- campaign-autonomy: `<autonomous | user-in-loop | decide with the user>`
- orchestrator-backend: `<codex | claude>`
- orchestrator-model: `<model>`
- translator-backend: `<codex | claude>`
- translator-model: `<model>`
- billing: `<api | subscription>`
- max-types: `<default | number>`
- max-syms: `<default | number>`
- max-loc: `<default | number>`
- parallel-max: `<orchestrator-selected | number>`
- review-pass: `<none | after every wave | at campaign end | decide with the user>`
- audit-ub: `<none | explicit user approval required>`
- results-path: `<repo-checkout>/crustify/wrappers-results.md`
- results-template: `standard`

# Legend

- `campaign-*` and `*-surface` define the repository, objective, and requested
  coverage boundary. They do not launch an agent.
- `wave-planning`, `setup-approval`, and `campaign-autonomy` control when the
  orchestrator pauses for user input.
- `orchestrator-*` selects the LLM that plans and supervises the campaign.
- `translator-*` selects the LLM used by each implementation agent.
- `billing` applies to both LLM roles unless separate values are provided.
- `max-*` controls deterministic oracle batching; `parallel-max` limits how
  many translator agents may run concurrently.
- `review-pass` may launch additional LLM translators with a review objective.
- `audit-ub` may launch the agentic `crustify-audit ub` pass and therefore uses
  an LLM. The normal `crustify-audit unsafe` pass is deterministic.
- `results-*` selects the tracked report location and template.
