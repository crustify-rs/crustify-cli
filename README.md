# Crustify

LLM-driven pipeline for automating the migration of production C/C++ codebases to Rust, at scale.
Given a codebase, crustify first builds an accurate dependency graph of symbols and types, which
guide the LLM agents for an incremental, cost-/time-efficient, and correct translation.  

## Workflow

### Setup
### Analyze
### Scaffold
### Bindgen
### Translate

## Dependency graph

Deterministic, builds an accurate dependency graph of symbols and types,
sorted in topological order, automatically identifying items that cross the FFI boundary
and items that migrate to native Rust. based on codeql

## LLM Agents

- three LLM agents
  - an orchestrator for driving the pipeline
  - a SymbolsTranslator for porting / wrapping symbols (functions, globals, callbacks)
  - a TypesTranslator for porting / wrapping types (structs, unions, enums)

- supported LLM backends
  - claude CLI, subscription or API billing
  - codex CLI, subscription or API billing

## CLI

TODO: add commands and flags


---

Automated C->Rust translation pipeline based on reasoning LLM agents. Each stage pairs a **deterministic
composer/scheduler** (all bookkeeping - graphs, scope, placement, batching,
allowlists) with **LLM agents** confined to judgement work (ownership
inference, codegen, merge). Artifacts for a repo live under
`<repo_root>/crustify/`; work is scoped to a repo-relative `target`.

## Pipeline

```
(manual: toolchains, build.json, project build, CodeQL db)
  -> analyze -> scaffold -> bindgen -> wrap -> port
          query / audit  (read-only, anytime)
```

There is no `build` stage. Toolchains, `build.json`, the project build and the
CodeQL database are the orchestrator's job (see `skills/crustify-pipeline/`);
crustify picks up at `analyze extract-ql`.

| stage | deterministic composer / scheduler | LLM agents |
|---|---|---|
| **analyze** | the whole stage: `extract-ql`, `scope`, `types`/`symbols` skeletons + full dependency graph, `dag` | - (the schemas' judgement fields are submitted by the wrap agents) |
| **scaffold** | `crates.json` -> `.rs` placement (query / create / validate) | - (`crates.json` is authored outside the stage; a lookup miss is a hard error) |
| **bindgen** | `<lib>-sys` crate skeletons, allowlists, include closure | - (the `fn main`, clang args and shims are completed by hand) |
| **wrap** | DAG-layered scheduler: scope filter, per-file/per-dep batching, budget slicing, per-wave isolation | per-unit wrapper agents (type / string / array / symbol); per-wave merge agent |
| **port** | not implemented | - |
| **query / audit** | graph walks, record reads, surface counts | - (fully deterministic) |

## Invocation

```
crustify-cli [--no-console] [--no-file-log] [--model NAME] [--parallel] [--parallel-max N] \
         <repo_root> <target> <command> [subcommand] [flags]
```

| global flag | effect |
|---|---|
| `--no-console` | suppress live agent console output |
| `--no-file-log` | disable per-agent logs under `crustify/targets/<target>/logs/<session>/` |
| `--model NAME` | override every agent's model, named `<provider>/<model>` (`anthropic/claude-opus-4-8`, `openai/gpt-5.6`, ...) |
| `--parallel` | enable per-command parallelism (wrap / port: concurrent agent chains; the analyze subjects are composer-only and ignore it) |
| `--parallel-max N` | max concurrent agents (default 8) |
| `--parallel-policy P` | `per-agent` (default) \| `serialize-per-file` (chain batches sharing a home `.rs`) \| `per-file` (pool free symbols per defining file) |
| `--billing subscription\|api` | how the provider CLI authenticates (default `subscription`) |
| `--override-base-prompt` / `--no-` | replace the provider CLI's own base prompt with crustify's (default: **keep** the provider's) |

## Commands

**analyze** `[--reset]` `<subject>`
| subject | flags | does |
|---|---|---|
| `extract-ql` | - | run the T1/T2 `.ql` batches against `crustify/codeql/db/` -> `crustify/codeql/{t1,t2}/*.csv` |
| `scope` | `--port-only` \| `--wrap-only` | port set (config) / wrap import-closure -> `scope.json` |
| `symbols` | `--all`/`--dir`/`--file`/`--name`; `--scope-only` (default)/`--port-only`/`--wrap-only`/`--unscoped`; `--out-suffix` | syms composer skeletons |
| `types` | same as `symbols` | types composer skeletons |
| `dag` | - | unified types+symbols dependency DAG -> `deps-dag.json` (scope-agnostic) |

Every subject is composer-only — `analyze` spawns no agent. The schemas'
judgement fields (pointer facets, ownership, lifetime, locking) are submitted by
the **wrap** agents via `query …/--update` when the entity is wrapped; the merge
primitive unions at field level, so re-composing never clobbers them.
`--unscoped` emits the repo-wide candidate set instead of the port-reachable
one — optional, and required by no later stage.

**scaffold** - `crates.json`-driven `.rs` oracle. One selector required.
`--all` \| `--dir DIR` \| `--file FILE` \| `--name N...` \| `--validate`, plus `--create`
(write stubs; without it, **query mode** prints the homed `.rs` path — every home
of a name, since one `(kind, name)` may home once per `tu`).
`crates.json` is authored outside the stage; an unplaced selection is a hard error.

**bindgen** - deterministic `<lib>-sys` FFI-crate composer, no LLM.
`--libs LIB...` (restrict), `--reset` (recompute the composer-owned allowlists +
include seed instead of accumulating). Crates come out incomplete: `build.rs` has
the allowlists but no `fn main`, and `bindgen.h`'s shim block is empty.

**query** - read-only oracle. `<subject>`
| subject | flags |
|---|---|
| `types` / `symbols` (alias `syms`) | enumerate, or `--name` introspect; `--schema`, `--manifest`, `--update FINDINGS`/`--update-help`, `--file`, `--port-only`/`--wrap-only`; types: `--fields`/`--field-touchers`/`--ops`/`--methods`/`--range A:B`; syms: `--fields`, `--array`, `--taking SPEC`/`--calling FN`/`--lifetime-for SPEC` (`--hops N`) |
| `files` | `--port-only` \| `--wrap-only` (scope file sets) |
| `dag` | `--name N...` (closure) \| `--layer N` (slice) \| `--name X --scc`; `--file`, `--depth N`, `--loc`, `--port-only`/`--wrap-only` |

**wrap** - emit Rust wrappers for wrap-scope units in dependency-layer order
(requires `scaffold` + `bindgen` to have run).
`--name N...` \| `--dag-layer N` \| `--lifetime-for SPEC`; `--file`,
`--wrap-only`/`--port-only`, `--max-syms N` (free-symbol batch budget),
`--skip N...`, `--transitive` (expand each `--name` through its dep closure),
`--review` (also schedule already-wrapped units), `--out-suffix`,
`--parallel`/`--parallel-max N`, `--dry-run`.

**port** - not implemented. The command and its flags still parse; nothing is
emitted.

**audit** - deterministic (no LLM) unsafe / raw-pointer / naked-FFI surface scan ->
`audit.json`. One seed selector: `--all` \| `--name N...` \| `--crate C` \| `--mod M`
\| `--dir D` \| `--file F` (search is always global; the selector picks the seeds).

## Selection model (wrap / port / query / scaffold)

`--name` takes a space-separated list (the user supplies dependency order);
`--file` disambiguates same-named entities; `--dag-layer N` (wrap) selects a
whole DAG layer. `wrap` drops a selected unit once its `// crustify:todo`
placeholders are gone - the `// Wraps:` anchor plus, for a type, its port-scope
`// Field:` anchors; `--review` keeps them. The scheduler never gates on whether
a DEP is already emitted - the C/FFI bridge keeps every intermediate state
compiling - and prints the first-layer deps before running (`--dry-run` to stop
there).
