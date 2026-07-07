---
name: crustify-pipeline
roles: [orchestrator]
description: >-
  Drive the crustify C→Rust translation pipeline end to end: the stage order
  (build → alloc → analyze → scaffold → bindgen → wrap → port), what each stage
  consumes/produces, how to select work, and the invocation gotchas. Use when
  orchestrating crustify across stages (not when translating a single symbol —
  that's crustify-oracle + the crate primitive skill).
---

# Orchestrating the crustify pipeline

For a single symbol's discovery use **crustify-oracle**; for choosing Rust
ownership wrappers use **crustify-wrap-crate**. This skill is the
*orchestration* layer: the stage graph and how to run it. **Exact flags live in
each command's `--help`** — run it; this is the router.

> Invocation: `crustify <repo_root> <target> <command> …`. Global flags
> (`--model`, `--parallel`) **before** `<repo_root>`; stage flags
> (`--parallel-max`, `--name`, `--dry-run`, `-y`) **after** the subcommand.
> `<target>` is the dir owning a `scope.json` (e.g. `src/libgit2`); `_root` is
> the whole-repo target. `--dry-run` previews a plan without spawning agents.

## Stage order (each consumes the prior artifacts)

| Stage | Produces | Notes |
|-------|----------|-------|
| `build` | `build.json` + CodeQL DB | two phases: `propose` drafts, `execute` runs configure+build+tests+CodeQL |
| `alloc` | `alloc.json` | the byte-allocator surface catalogue |
| `analyze <subject>` | `analysis/**/{types,syms}.json`, `deps-dag.json`, `scope.json` | subjects: `scope` → `symbols`/`types` → `dag` (in that order) |
| `scaffold` | resolved `.rs` modules (placement oracle / stubs) | `--all` fills a target; `--validate` runs the consistency gate |
| `bindgen` | `<lib>-sys` FFI crates | deterministic, no LLM |
| `wrap` | Rust wrappers for **wrap-scope** units (types + free syms) | dependency-layer order; needs scaffold+bindgen done |
| `port` | ported Rust for **port-scope** via `--name` | the agent operates at repo root |

## Orchestration idioms

- **Gate explicitly**: stages don't auto-chain — confirm a stage's artifacts
  exist before the next. `build` is split `propose`/`execute` precisely so the
  gating is deliberate.
- **Select work via the oracle**: `query … types --wrap-only | xargs … wrap
  --name` (see crustify-oracle). `port --name A B …` takes the dep order *you*
  supply; `--dag-layer N` is the e2e driver mode.
- **Parallelism**: `--parallel` (global) + `--parallel-max N` (on the stage),
  for agents across disjoint files. Isolated waves run in git worktrees.
- **Preview first**: `--dry-run` on `wrap`/`port` prints units, batches, and
  first-layer deps before committing agents.

## Read-only vs mutating

`query`/`scaffold`(locate)/`audit` are safe to run anytime (→ crustify-oracle).
`build execute`/`scaffold --all`/`wrap`/`port` mutate the tree and/or spawn
agents — run them deliberately, and prefer `--dry-run` to scope them first.

> Discovery note: this skill is orchestrator-facing, so it is loaded via Claude
> Code's native skill discovery (symlink/install into `.claude/skills/`), **not**
> via crustify's `{skills}` injection — translators never see it.
