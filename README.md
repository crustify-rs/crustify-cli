# Crustify

Crustify is an agent harness for incrementally migrating production C and C++
code to safe, idiomatic Rust. It can:

- port an implementation to native Rust;
- generate safe Rust wrappers over an unsafe API; or
- migrate selected subsystems, types, or functions while preserving FFI
  interoperability with the code left behind.

Crustify combines agentic implementation and review with deterministic
semantic planning, isolated worktrees, build and test gates, and compiled Rust
safety analysis.

## Agent harness

Crustify uses a **two-agent architecture**. A campaign may run many isolated
translator instances, but every agent acts in one of these two roles:

- The **orchestrator** configures campaigns, scaffolds repositories, schedules
  sub-campaigns, manages worktrees, lands changes, and runs regression gates.
- A **translator** ports or wraps scheduled types and symbols, writes tests,
  validates its work, and commits the result. Translator instances can also be
  dispatched in review mode.

The harness supports two complementary reliability mechanisms:

- **Self-repair:** the orchestrator diagnoses failed agents, incomplete work,
  merge conflicts, and regression failures, then retries or reschedules the
  affected work.
- **Adversarial testing:** independent translator instances review completed
  code, challenge unsafe assumptions and behavioral equivalence, and repair
  confirmed defects.

Deterministic tools support both roles; they are not additional agents.

## Components

| Component | Responsibility |
|---|---|
| `crustify-cli` | Agent execution, isolated worktrees, crate placement, logging, and campaign artifacts |
| [`crustify-oracle`](https://github.com/crustify-rs/crustify-oracle) | CodeQL-backed dependency analysis, scope selection, and deterministic scheduling |
| [`crustify-audit`](https://github.com/crustify-rs/crustify-audit) | Compiled Rust safety analysis and optional UB review |
| [`ffibox`](https://github.com/crustify-rs/ffibox) | Safe FFI smart pointers and lifetime traits used by generated wrappers |

Claude Code and OpenAI Codex are supported as agent backends. Campaigns can
use API or subscription billing and can select a model independently for each
agentic stage.

## Quick start

Crustify requires Python 3.13. Clone the four components side by side and
install the three Python packages:

```bash
git clone https://github.com/crustify-rs/crustify-cli.git
git clone https://github.com/crustify-rs/crustify-oracle.git
git clone https://github.com/crustify-rs/crustify-audit.git
git clone https://github.com/crustify-rs/ffibox.git

python -m pip install -e crustify-cli
python -m pip install -e crustify-oracle
python -m pip install -e crustify-audit
```

Render the orchestrator prompt and give it to a supported coding agent:

```bash
crustify-orchestrator-prompt \
  ./crustify-oracle ./crustify-audit \
  -o orchestrator.md
```

The orchestrator asks which repository and revision to use, whether to port or
wrap it, where the campaign should start, which models to use, and how much
autonomy it has. It presents a consolidated campaign brief for approval before
mutating the target repository.

For a reproducible container environment or a pre-filled campaign manifest,
see [`bench/`](bench/README.md).

## Campaign lifecycle

1. **Scope:** choose a target and objective, then define sub-campaigns now or
   brainstorm them with the orchestrator.
2. **Setup:** establish the build and test baseline, extract CodeQL data,
   configure the oracle target, and scaffold compiling Rust crates.
3. **Plan:** the oracle converts the selected dependency closure into ordered,
   deterministic batches. Waves are internal scheduling artifacts.
4. **Translate:** isolated translator agents port, wrap, or review one batch at
   a time and verify both Rust and original project tests.
5. **Land and audit:** the orchestrator reconciles parallel work, runs
   regression and safety gates, performs approved reviews, and promotes the
   completed sub-campaign.

The authoritative procedures are the
[`orchestrator playbook`](docs/orchestrator-playbook.md) and
[`translator playbook`](docs/translator-playbook.md).

## CLI

This repository installs three commands:

```text
crustify-orchestrator-prompt  render the campaign orchestrator prompt
crustify-cli                 validate crate placement and execute wave documents
crustify-log-cost            summarize agent usage and cost logs
```

The oracle and audit interfaces are owned by their respective repositories.
Use `<command> --help` for current commands, defaults, and flags; the README
does not duplicate those interfaces.

## Results

Crustify has been exercised on >100K-line targets in
[libgit2](https://github.com/crustify-rs/crustify-libgit2) and
[OpenSSL](https://github.com/crustify-rs/crustify-openssl). In the recorded
god-object experiments, it transitively processed 68 new libgit2 units and 47
new OpenSSL units across closures up to 12 layers deep. Generated wrappers
priced consistently at roughly $0.008–$0.010 per line across the two unrelated
codebases.

Detailed cost, wall-time, unsafe-surface, and test measurements are preserved
in [`bench/libgit2-openssl-results.md`](bench/libgit2-openssl-results.md).
Newer per-campaign reports and reproducible inputs live under [`bench/`](bench/).

## Documentation

- [`docs/orchestrator-playbook.md`](docs/orchestrator-playbook.md): campaign
  setup, translation, landing, review, accounting, and self-repair
- [`docs/translator-playbook.md`](docs/translator-playbook.md): translator
  procedures for types, symbols, lifetimes, tests, and completion
- [`docs/conventions.md`](docs/conventions.md): generated Rust conventions
- [`docs/schemas/`](docs/schemas/): crate-placement and wave schemas
- [`bench/TASK.md`](bench/TASK.md): campaign intake questionnaire

## Acknowledgements

This material is based upon work supported by the Defense Advanced Research
Projects Agency (DARPA) Translating All C To Rust (TRACTOR) program under
Agreement No. HR00112590134.
