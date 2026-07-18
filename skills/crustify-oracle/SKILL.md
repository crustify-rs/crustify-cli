---
name: crustify-oracle
bin: crustify
roles: [translator, orchestrator, analyzer]
description: >-
  The crustify analysis oracle for C symbols and types. Query a record /
  signature, its emitted Rust module, its dependency closure, the schema, the
  synthetic string/array clusters, and the unsafe surface of ported code -- and,
  for the analyzers, submit findings back through it. Both read and write go
  THROUGH the oracle (`query` syms/types/dag/files, `--schema`, `--update` /
  `--create`; `scaffold --name`; `audit`), never by touching a manifest file
  directly (risk of concurrent race).
---

# The crustify analysis oracle (`query` / `scaffold` / `audit`)

`query` owns the manifest schema and the file layout: you read your worklist AND
submit your findings *through it* -- never write a `syms.json` /
`types.json` directly. Everything is read-only except the two submit verbs
(`--update`, `--create`), which validate the findings, map them onto the schema,
and merge them under a lock (untouched slots left as-is; re-submitting is
idempotent). For **exact flags run the command's `--help`** (or `--update-help`
for the findings shape) -- argparse is the source of truth and never drifts.
This skill is the *router*: which command for which intent, grouped by command,
plus the idioms `--help` can't tell you.

> Invocation shape: `crustify <repo_root> <target> <command> ...`. Global flags
> (`--model`, `--parallel`) go **before** `<repo_root>`; a stage's own flags
> (e.g. `--parallel-max`) go **after** the subcommand. Use the target that owns
> a `scope.json` (e.g. `src/libgit2`) for scope-aware commands; add `--unscoped`
for a repo-wide, scope-blind pass.

## `query types` -- type records, type-analyzer discover + submit, buffer clusters

| you need | invocation |
|----------|------------|
| a type's record | `query types --name <tag>` |
| the field/role **schema** definitions | `query types --schema` |
| the declared **fields** to analyze (+ each pointer's `ptr` block); `--range A:B` windows a batch of them | `query types --name <tag> --file <file> --fields [--range A:B]` |
| the lifecycle **candidate pool** (complete footprint) | `query types --name <tag> --file <file> --methods` |
| which fn **touches** each field (complete footprint) | `query types --name <tag> --file <file> --field-touchers` |
| the synthetic **array** / **string** clusters | `query types --arrays` / `--strings` |
| enumerate by scope (then `xargs` a stage) | `query types --wrap-only` / `--port-only` |
| **submit** type findings (WRITE) | `query types --name <tag> --file <file> --update <file>` (or `--update -`) |
| **create** a synthetic string/array cluster (WRITE) | `query types --create <cluster.json>` |

## `query symbols` -- symbol records, symbol-analyzer discover + submit

(`syms` is a back-compat alias for `symbols`.)

| you need | invocation |
|----------|------------|
| a symbol's record (signature, pointer analysis, type/sym deps) | `query symbols --name <name>` |
| the **type-generator** primitives (`DEFINE_*` / `DECLARE_*` macro families, `macro.typegen`) | `query symbols --typegens` |
| a type's lifecycle **roles** -- READ what the analyzer already flagged, grouped into `dropped_by`/`fields_disposed_by`/`cloned_by` | `query symbols --lifetime-for <SPEC> [--array]` |
| lifecycle **candidates** -- DISCOVER the pool to triage (the inverse); `--calling` keeps only those reaching a known primitive | `query symbols --taking <SPEC> [--calling FN,...] [--hops N] [--array]` |
| **submit** symbol findings (WRITE) | `query symbols --name <name> --file <file> --update <file>` |

`SPEC` = struct tag / typedef, or `void` (raw byte-level) / `string`.

## `query dag` -- dependency closure

| you need | invocation |
|----------|------------|
| a symbol/type's transitive **deps** (already emitted; call its safe API, never raw `ffi::`) | `query dag --name <X> [--depth 1] [--with-details]` |
| flattened-cycle twins you may reference **naked** (higher-layer; target not wrapped yet) | `query dag --name <X> --scc hi-deps` |
| already-wrapped twins that referenced **you** naked - switch them to your wrapper | `query dag --name <X> --scc lo-deps` |

## `query files` -- scope sets

| you need | invocation |
|----------|------------|
| the port set / wrap closure file lists | `query files --port-only` / `--wrap-only` |

## `scaffold` -- locate a Rust module

| you need | invocation |
|----------|------------|
| where an element's Rust module lives (placeholder anchor, or an already-wrapped type's module) | `scaffold --name <name>` |

## `audit` -- unsafe surface (deterministic, read-only)

| you need | invocation |
|----------|------------|
| the unsafe / raw-ptr / naked-`ffi::` surface of ported code | `audit [--name <seed>]` |

## Idioms `--help` won't tell you

- **`query` is enumerate-or-introspect**: no `--name` -> list (filtered) entries;
  `--name X` -> introspect one; several names -> several records.
- **Submit through the oracle, never the file.** `--update` / `--create`
  validate (rejecting malformed findings), map onto the schema, and merge under
  a lock -- so re-submitting is idempotent and other entries/slots are left
  untouched. Run `--update-help` for the findings shape before your first write.
- **Never strip `CRUSTIFY_OUT_SUFFIX` from the environment.** It is set by the
  orchestrator when your run is one arm of a parallel matrix, and it redirects
  your writes to `syms/types_<SUFFIX>.json` so concurrent arms do not clobber each
  other or the canonical tree. If you shell out (e.g. `subprocess.run`), inherit
  the environment as-is: do NOT pass a hand-built `env=`, and do NOT
  `env.pop('CRUSTIFY_OUT_SUFFIX')`. Dropping it silently redirects your findings
  into canonical and corrupts every other arm's baseline. The suffix is not a
  demotion -- suffixed records are promoted to canonical after review.
- **Enumerate -> xargs a stage**: `query types --wrap-only | xargs ... wrap --name`.
- **`dag` closure vs scope**: `query dag --name X` is X's transitive *deps*
  (emitted before it); `--depth 1` = direct deps only. Scope gates EMISSION,
  never CONTENT: an emitted record's `depends_on` / `used_by` are codebase-wide
  regardless of its scope, so a WRAP-scope node's `depends_on.syms` is populated
  and safe to walk.
- **`audit` is deterministic (no LLM)**: per-seed unsafe surface + a tree-wide
  `global` section + `totals`; printed to stdout, nothing written.