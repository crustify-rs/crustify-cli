---
name: crustify-pipeline
roles: [orchestrator]
description: >-
  Drive the crustify C→Rust translation pipeline end to end: the manual
  prerequisites (toolchains, build.json, CodeQL database), the stage order
  (analyze → scaffold → bindgen → wrap → port), what each stage
  consumes/produces, how to select work, and the invocation gotchas. Use when
  orchestrating crustify across stages (not when translating a single symbol —
  that's crustify-oracle + the crate primitive skill).
---

# Prerequisites — done by hand, before any crustify command

crustify has no build stage. Toolchains, `build.json`, the project build, and
the CodeQL database are the orchestrator's job; crustify picks up at
`analyze extract-ql`.

## 1. Install dependencies

| Dependency | Needed by | Install |
|---|---|---|
| CodeQL CLI + `codeql/cpp-all` | `analyze extract-ql`, DB creation | `brew install --cask codeql`; then `codeql pack install` in `utils/codeql/` |
| Rust ≥ 1.85 (edition 2024) | `bindgen`, `wrap`, `port` | `rustup` |
| Rust nightly + `rustc-dev`, `llvm-tools` | `audit` (rustc HIR driver, `utils/unsafe_metrics/rust-toolchain.toml`) | `rustup toolchain install nightly -c rustc-dev -c llvm-tools` |
| libclang | the `bindgen` crate in each `<lib>-sys` `build.rs` | Xcode CLT / LLVM; set `LIBCLANG_PATH` if not found |
| C toolchain + the project's own build deps | building the target, the `cc` crate | project-specific |
| Python ≥ 3.13 | crustify itself | venv + `pip install -e <crustify checkout>` |

## 2. Author `build.json`

Hand-write `<repo_root>/crustify/build.json` — repo-tier, project-wide.
Schema + a worked OpenSSL example: `templates/build.json`. Consumed downstream
by the composers, scaffolder, bindgen, wrap, port, and merge, so it must be
accurate before anything else runs.

## 3. Build under CodeQL trace

Run `build_commands.configure` from `<repo_root>`, then the build wrapped in
the extractor:

```
codeql database create <repo_root>/crustify/codeql/db \
  --language=cpp \
  --command="<build_commands.build, exactly as written>"
```

The database directory must not already exist — remove it first, never append.
Then run `build_commands.test` and record pass/total as the port-equivalence
baseline.

## 4. Extract the T1/T2 tables

```
crustify <repo_root> <target> analyze extract-ql
```

Deterministic, no LLM: runs every `.ql` under `utils/codeql/{entities,edges}/`
against the database and writes one CSV per query to
`crustify/codeql/{t1,t2}/`. `--reset` wipes those trees; the database is left
alone. Every analyze subject below reads these CSVs.

# Orchestrating the crustify pipeline

For a single symbol's discovery use **crustify-oracle**; for choosing Rust
ownership wrappers use **crustify-wrap-crate**. This skill is the
*orchestration* layer: the stage graph and how to run it. **Exact flags live in
each command's `--help`** — run it; this is the router.

> Invocation: `crustify <repo_root> <target> <command> …`. Global flags
> (`--model`, `--parallel`) **before** `<repo_root>`; stage flags
> (`--parallel-max`, `--name`, `--dry-run`, `-y`) **after** the subcommand.
> `<target>` is the dir owning a `scope.json` (e.g. `src/libgit2`); for a
> repo-wide run add `--unscoped` on a real target. `--dry-run` previews a plan
> without spawning agents.

## Stage order (each consumes the prior artifacts)

| Stage | Produces | Notes |
|-------|----------|-------|
| `analyze <subject>` | `codeql/{t1,t2}/*.csv`, `scope.json`, `analysis/**/{types,syms}.json`, `deps-dag.json` | subjects: `extract-ql` → `scope` → `symbols`/`types` → `dag` (in that order) |
| `scaffold` | resolved `.rs` modules (placement oracle / stubs) | `--all` fills a target; `--validate` runs the consistency gate |
| `bindgen` | `<lib>-sys` FFI crates | deterministic, no LLM |
| `wrap` | Rust wrappers for **wrap-scope** units (types + free syms) | dependency-layer order; needs scaffold+bindgen done |
| `port` | ported Rust for **port-scope** via `--name` | the agent operates at repo root |

## Orchestration idioms

- **Gate explicitly**: stages don't auto-chain — confirm a stage's artifacts
  exist before the next.
- **Select work via the oracle**: `query … types --wrap-only | xargs … wrap
  --name` (see crustify-oracle). `port --name A B …` takes the dep order *you*
  supply; `--dag-layer N` is the e2e driver mode.
- **Parallelism**: `--parallel` (global) + `--parallel-max N` (on the stage),
  for agents across disjoint files. Isolated waves run in git worktrees.
- **Preview first**: `--dry-run` on `wrap`/`port` prints units, batches, and
  first-layer deps before committing agents.

## Read-only vs mutating

`query`/`scaffold`(locate)/`audit`/`analyze extract-ql` are safe to run anytime
(→ crustify-oracle). `scaffold --all`/`wrap`/`port` mutate the tree and spawn
agents — run them deliberately, and prefer `--dry-run` to scope them first.

> Discovery note: this skill is orchestrator-facing, so it is loaded via Claude
> Code's native skill discovery (symlink/install into `.claude/skills/`), **not**
> via crustify's `{skills}` injection — translators never see it.
