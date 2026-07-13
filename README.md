# crustify

Multi-agent C->Rust translation pipeline. Each stage pairs a **deterministic
composer/scheduler** (all bookkeeping - graphs, scope, placement, batching,
allowlists) with **LLM agents** confined to judgement work (ownership
inference, codegen, merge). Artifacts for a repo live under
`<repo_root>/crustify/`; work is scoped to a repo-relative `target`.

## Pipeline

```
build -> alloc -> analyze -> scaffold -> bindgen -> wrap -> port
                                 query / audit  (read-only, anytime)
```

| stage | deterministic composer / scheduler | LLM agents |
|---|---|---|
| **build** | `execute`: configure->build->tests->CodeQL->T1/T2 CSVs | `propose`: drafts `build.json` |
| **alloc** | catalogue the allocator surface -> `alloc.json` | - |
| **analyze** | `scope` + `dag` (pure); `types`/`symbols` skeleton + full dependency graph | type / symbol / buffer analyzers fill ownership, lifecycle, ptr facets |
| **scaffold** | `crates.json` -> `.rs` placement (query / create / validate) | `CrustifyScaffolder` authors `crates.json` on a lookup miss |
| **bindgen** | `<lib>-sys` crate skeletons, allowlists, macro worklists | `CrustifyBindgenShimmer`: macro shims + `cargo check` verify loop |
| **wrap** | DAG-layered scheduler: scope filter, per-file/per-dep batching, budget slicing, per-wave isolation | per-unit wrapper agents (type / string / array / symbol); per-wave merge agent |
| **port** | same scheduler (port scope) | per-unit port agents; per-wave merge agent |
| **query / audit** | graph walks, record reads, surface counts | - (fully deterministic) |

## Invocation

```
crustify [--no-console] [--no-file-log] [--model NAME] [--parallel] [--parallel-max N] \
         <repo_root> <target> <command> [subcommand] [flags]
```

| global flag | effect |
|---|---|
| `--no-console` | suppress live agent console output |
| `--no-file-log` | disable per-agent logs under `.crustify/logs/<session>/` |
| `--model NAME` | override every agent's model (`claude-opus-4-8`, `codex/gpt-5.5`, ...) |
| `--parallel` | enable per-command parallelism (analyze: one agent per manifest dir) |
| `--parallel-max N` | max concurrent agents (default 8) |

## Commands

**build** - two explicit phases.
| subcommand | flags | does |
|---|---|---|
| `propose` | `--reset` | draft `build.json` (LLM) |
| `execute` | `--reset` | run configure+build+tests+CodeQL, extract T1/T2 CSVs |

**alloc** - `crustify <r> <t> alloc` -> `alloc.json` (feeds the analyze buffer pass).

**analyze** `[--reset]` `<subject>`
| subject | flags | does |
|---|---|---|
| `scope` | `--port-only` \| `--wrap-only` | port set (config) / wrap import-closure -> `scope.json` |
| `symbols` | `--all`/`--dir`/`--file`/`--name`, `--port-only`/`--wrap-only` | syms composer skeleton + symbol analyzer |
| `types` | same as `symbols`, plus `--buffers` | types composer skeleton + type analyzer (`--buffers` = string/array cluster pass only) |
| `dag` | - | unified types+symbols dependency DAG -> `deps-dag.json` (scope-agnostic) |

**scaffold** - `crates.json`-driven `.rs` oracle. One selector required.
`--all` \| `--dir DIR` \| `--file FILE` \| `--name N...` \| `--validate`, plus `--create`
(write stubs; without it, **query mode** prints the homed `.rs` path).

**bindgen** - deterministic `<lib>-sys` FFI-crate composer + shim agent.
`--libs LIB...` (restrict), `--scaffold-only` (composer only, skip the shim agent).

**query** - read-only oracle. `<subject>`
| subject | flags |
|---|---|
| `types` / `syms` | enumerate, or `--name` introspect; `--with-details`, `--manifest`, `--update FINDINGS`, `--port-only`/`--wrap-only`, `--strings`/`--arrays`; types: `--fields`/`--ops`/`--methods`/`--accessors`/`--create ENTRY`/`--range A:B`; syms: `--typegens` |
| `files` | `--port-only` \| `--wrap-only` (scope file sets) |
| `dag` | `--name N...` (closure) \| `--layer N` (slice) \| `--name X --scc hi-deps`\|`lo-deps`; `--file`, `--depth N`, `--with-details` |

**wrap** - emit Rust wrappers for wrap-scope units in dependency-layer order
(requires `scaffold` + `bindgen` to have run).
`--name N...` \| `--strings` \| `--arrays`; `--file`, `--wrap-only`/`--port-only`,
`--max-fields N`, `--max-ops N` (per-agent budgets), `--dag-layer N`, `--skip N...`,
`--parallel-max N`, `-y`/`--yes`, `--dry-run`.

**port** - emit ported Rust via the `--name` scheduler.
`--name N...`, `--file F...`, `--max-syms N`, `--max-fields N`, `--parallel-max N`,
`-y`/`--yes`, `--dry-run`.

**audit** - deterministic (no LLM) unsafe / raw-pointer / naked-FFI surface scan ->
`audit.json`. One seed selector: `--all` \| `--name N...` \| `--crate C` \| `--mod M`
\| `--dir D` \| `--file F` (search is always global; the selector picks the seeds).

## Selection model (wrap / port / query / scaffold)

`--name` takes a space-separated list (the user supplies dependency order);
`--file` disambiguates same-named entities; `--dag-layer N` (wrap) selects a
whole DAG layer. The scheduler never gates on whether a dep is already emitted -
the C/FFI bridge keeps every intermediate state compiling - and prints the
first-layer deps before running (`--dry-run` to stop there).
