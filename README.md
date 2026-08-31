# Crustify

Crustify is an agent harness for incrementally migrating production C and C++
code to safe, idiomatic Rust. It can:

- port an implementation to native Rust, fully or partially;
- generate safe Rust wrappers over an unsafe API; or
- find undefined behavior and memory safety bugs in safe Rust code.

Reliable translation takes more than a capable model, so Crustify equips its
agents with the tools and skills the job actually requires: a CodeQL-backed dependency
oracle that resolves the semantic order of a closure, a crate of smart pointers
and lifetime traits for safe interop across the FFI boundary, and a compiled
Rust safety auditor with an optional undefined-behavior pass. Agents write and
review code inside isolated worktrees, behind build, test, and safety gates.

## Agent harness

Crustify uses a **two-agent architecture**:

- The **orchestrator** configures campaigns, scaffolds repositories, schedules
  sub-campaigns, manages worktrees, lands changes, and runs regression gates.
- The **translator** agent ports or wraps scheduled types and symbols, writes tests,
  validates its work, and commits the result. Translator instances can also be
  dispatched in review mode. Several translator instances can run concurrently.

The harness supports two complementary reliability mechanisms:

- **Self-repair:** the orchestrator diagnoses failed agents, incomplete work,
  merge conflicts, and regression failures, then retries or reschedules the
  affected work.
- **Adversarial testing:** independent translator instances review completed
  code, challenge unsafe assumptions and behavioral equivalence, and repair
  confirmed defects.

The oracle, ffibox, and the auditor serve both the orchestrator and translators
as skills.

## Components

| Component | Responsibility |
|---|---|
| `crustify` | Agent execution, isolated worktrees, crate placement, logging, and campaign artifacts |
| [`crustify-oracle`](https://github.com/crustify-rs/crustify-oracle) | CodeQL-backed dependency analysis, scope selection, and deterministic scheduling |
| [`crustify-audit`](https://github.com/crustify-rs/crustify-audit) | Compiled Rust safety analysis and optional UB review |
| [`ffibox`](https://github.com/crustify-rs/ffibox) | Safe FFI smart pointers and lifetime traits used by generated wrappers |

Claude Code and OpenAI Codex are supported as agent backends. Campaigns can
use API or subscription billing and can select a model independently for each
agentic stage. The recommended baseline is `claude-opus-5` for the
orchestrator, `gpt-5.6-sol` for translators, and `claude-opus-5` again for the
review and UB passes.

## Quick start

Crustify requires Python 3.13. Clone the four components side by side and
install the three Python packages; `ffibox` is a Rust crate that generated
wrappers depend on, so it is cloned but not installed:

```bash
git clone https://github.com/crustify-rs/crustify.git
git clone https://github.com/crustify-rs/crustify-oracle.git
git clone https://github.com/crustify-rs/crustify-audit.git
git clone https://github.com/crustify-rs/ffibox.git

python -m pip install -e crustify
python -m pip install -e crustify-oracle
python -m pip install -e crustify-audit
```

Optionally copy and pre-fill the campaign questionnaire before starting the
orchestrator:

```bash
cp crustify/examples/crustify/TASK.md campaign-TASK.md
# Edit campaign-TASK.md and answer as many questions as you want.
```

Render the orchestrator prompt—with or without that task—and give it to a
supported coding agent:

```bash
crustify-orchestrator-prompt \
  ./crustify-oracle ./crustify-audit ./campaign-TASK.md \
  -o orchestrator.md
```

Omit `./campaign-TASK.md` to answer every unresolved question live.
The orchestrator asks which repository and revision to use, whether to port or
wrap it, where the campaign should start, which models to use, and how much
autonomy it has. It presents a consolidated campaign brief for approval before
mutating the target repository.

Campaign reports follow the
[`examples/crustify/results.md`](examples/crustify/results.md) template.

For a reproducible container environment or a pre-filled campaign manifest,
see [`examples/`](examples/crustify/README.md).

## Campaign lifecycle

1. **Scope:** choose a target and objective, then define sub-campaigns now or
   brainstorm them with the orchestrator.
2. **Setup:** establish the build and test baseline, extract CodeQL data,
   configure the campaign-wide oracle target, decompose its targeted and
   imported translation units into `subsystems.json`, and scaffold compiling
   Rust crates.
3. **Plan:** for a port campaign, the orchestrator creates one sub-campaign per
   subsystem plus separate raw `void` and raw `string` lifetime sub-campaigns.
   Each gets a `scope-config.json` whose exact closure comes from the oracle.
   Sub-campaigns and their waves run bottom-up so every consumer sees already-safe
   producers. Waves remain internal scheduling artifacts.
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
crustify                 validate crate placement and execute wave documents
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

Correctness is gated rather than assumed. Every unit landed only after
`cargo check`, `cargo clippy`, and `cargo test --workspace` passed over the
whole tree. Translators emit unit tests beside the code they write — 1,917 and
1,368 `#[test]` functions across the two campaigns — and exercise both the C
and Rust sides under the configured sanitizers wherever a test crosses the FFI
boundary. `crustify-audit unsafe` then measured the landed surface at 5.6% of
48,309 libgit2 lines and 7.4% of 34,235 OpenSSL lines, with 77.5% and 93.5% of
unsafe blocks confined inside an `impl T`, reachable only through a wrapped
type's accessors. An independent agentic review pass and the optional
`crustify-audit ub` undefined-behavior pass run on request, outside the
per-wave gates.

Detailed cost, wall-time, unsafe-surface, and test measurements are preserved
in [`examples/crustify/libgit2-openssl-results.md`](examples/crustify/libgit2-openssl-results.md).
Newer per-campaign reports and reproducible inputs live under
[`examples/`](examples/).

## Documentation

- [`docs/orchestrator-playbook.md`](docs/orchestrator-playbook.md): campaign
  setup, translation, landing, review, accounting, and self-repair
- [`docs/translator-playbook.md`](docs/translator-playbook.md): translator
  procedures for types, symbols, lifetimes, tests, and completion
- [`docs/conventions.md`](docs/conventions.md): generated Rust conventions
- [`docs/schemas/`](docs/schemas/): crate-placement and wave schemas
- [`docs/schemas/subsystems.md`](docs/schemas/subsystems.md): link-unit and subsystem decomposition artifact
- [`docs/schemas/scope-config.md`](docs/schemas/scope-config.md): per-sub-campaign selection and oracle closure
- [`examples/crustify/TASK.md`](examples/crustify/TASK.md): optional pre-filled campaign questionnaire
- [`examples/crustify/results.md`](examples/crustify/results.md): campaign results
  template

## Acknowledgements

This material is based upon work supported by the Defense Advanced Research
Projects Agency (DARPA) Translating All C To Rust (TRACTOR) program under
Agreement No. HR00112590134.
