---
name: crustify-oracle
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
> a `scope.json` (e.g. `src/libgit2`), not `_root`, for scope-aware commands.

## `query types` -- type records, type-analyzer discover + submit, buffer clusters

| you need | invocation |
|----------|------------|
| a type's record | `query types --name <tag> --with-details` |
| the field/role **schema** definitions | `query types --schema` |
| the declared **fields** to analyze (+ each pointer's `ptr` block); `--range A:B` windows a batch of them | `query types --name <tag> --file <file> --fields [--range A:B] --with-details` |
| the lifecycle **candidate pool** (complete footprint fns) | `query types --name <tag> --file <file> --methods` |
| who **touches** each in-scope field (accessor fns) | `query types --name <tag> --file <file> --accessors --scope-only` |
| the synthetic **array** / **string** clusters | `query types --arrays --with-details` / `--strings --with-details` |
| enumerate by scope (then `xargs` a stage) | `query types --wrap-only` / `--port-only` |
| **submit** type findings (WRITE) | `query types --name <tag> --file <file> --update <file>` (or `--update -`) |
| **create** a synthetic string/array cluster (WRITE) | `query types --create <cluster.json>` |

## `query syms` -- symbol records, symbol-analyzer discover + submit

| you need | invocation |
|----------|------------|
| a symbol's record (signature, pointer analysis, type/sym deps) | `query syms --name <name> --with-details` |
| a lifecycle candidate's signature / body location | `query syms --name <fn> --with-details` |
| the **type-generator** primitives (`DEFINE_*` / `DECLARE_*` macro families, kind `macro_typegen`) | `query syms --typegens` |
| **submit** symbol findings (WRITE) | `query syms --name <name> --file <file> --update <file>` |

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

## `query mem` -- allocator clusters (from `alloc.json`)

Every allocator family returned **verbatim**: the family name, its `free`, and
each allocator's full record (`name` + the `zeroing` / `sized` / `aligned` /
`string` / `bounded` flags + `defined_in` / `declared_in` / `type`). Use it to
emit the `CBox` exclusive-freed strategy ZST (one per cluster, keyed on the free)
plus the constructor wrappers. **No string/byte gate** -- the per-allocator
`string` flag rides along, so you decide what is a nul-terminated string vs a raw
byte buffer. Output is JSON.

| you need | invocation |
|----------|------------|
| every allocator cluster | `query mem` |
| the cluster(s) owning a specific allocator/free (pick the free for your `CBox` strategy) | `query mem --name <sym>` |

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
- **Enumerate -> xargs a stage**: `query types --wrap-only | xargs ... wrap --name`.
- **`dag` closure vs scope**: `query dag --name X` is X's transitive *deps*
  (emitted before it); `--depth 1` = direct deps only. A WRAP-scope node's
  `depends_on.syms` is empty by design -- read the closure, don't infer from it.
- **`audit` is deterministic (no LLM)**: per-seed unsafe surface + a tree-wide
  `global` section + `totals`; printed to stdout, nothing written.