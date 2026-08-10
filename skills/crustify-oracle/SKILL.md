---
name: crustify-oracle
bin: crustify-oracle
description: >-
  Read the analysis of a C codebase and submit ownership findings back. Type and
  symbol records, their pointer analysis and lifecycle roles, the dependency
  closure, the scope sets, and the submission verb. Reads and writes both go
  through the oracle, never by editing a file. Prefer using this instead of grep
  or regex for accurate semantic reasoning over the code. 
---

# `crustify-oracle`

`crustify-oracle <repo_root> <target> <command> …`

Everything is read-only except `--update`, which validates a findings doc
against the composed record, merges it under a lock, and leaves untouched slots
as they are. Re-submitting is idempotent.

Run a command's `--help` for its exact flags, and `--update-help` for the
findings shape — argparse is the source of truth.

## `query types`

| you need | invocation |
|---|---|
| a type's record | `query types --name <tag> --file <file>` |
| the schema's field meanings | `query types --schema` |
| declared fields + each pointer's `ptr` block | `query types --name <tag> --file <file> --fields` |
| the lifecycle candidate pool (whole-codebase footprint) | `query types --name <tag> --file <file> --methods` |
| which function touches each field | `query types --name <tag> --file <file> --field-touchers` |
| enumerate by scope | `query types --port-only` / `--wrap-only` |
| submit findings | `query types --name <tag> --file <file> --update <doc>` (`-` for stdin) |

## `query symbols`

`syms` is an alias.

| you need | invocation |
|---|---|
| a symbol's record — signature, pointer analysis, deps | `query symbols --name <name> --file <file>` |
| a type's lifecycle roles, from the `lifetime` blocks already submitted | `query symbols --lifetime-for <SPEC> [--array]` |
| the candidate pool to triage; `--calling` keeps those reaching a known primitive | `query symbols --taking <SPEC> [--calling FN,…] [--hops N] [--array]` |
| submit findings | `query symbols --name <name> --file <file> --update <doc>` |

`SPEC` = a struct tag or typedef, or `void` (raw bytes) / `string`.

A type carries no lifecycle of its own: `dropped_by` / `fields_disposed_by` /
`cloned_by` are reverse-derived from the acting symbols' `lifetime` blocks.

## `query dag`

| you need | invocation |
|---|---|
| transitive deps, already emitted — call their safe API, never raw `ffi::` | `query dag --name <X> [--depth 1]` |
| higher-layer cycle twins you may reference naked | `query dag --name <X> --scc hi-deps` |
| lower-layer twins that referenced you naked — switch them to your wrapper | `query dag --name <X> --scc lo-deps` |
| every node at one layer | `query dag --layer <N> [--port-only|--wrap-only]` |

JSON grouped by kind: `types` / `callbacks` / `functions` / `globals` /
`macros`, each `{id, layer, defined_in}` plus `depth` in closure mode; empty
groups are omitted. The groups route the work — `types` to the type wrapper,
`callbacks` and `functions` to the symbol wrapper, `macros` to nobody (bindgen
owns their `-sys` shims). `layer` and `depth` exist only here.

## `query files`

| you need | invocation |
|---|---|
| the port set / wrap closure file lists | `query files --port-only` / `--wrap-only` |

## `extract-ql`

Re-runs the CodeQL batches into `crustify/codeql/{t1,t2}/`. Takes minutes, avoid running unless
really necessary.

## Idioms

- **Enumerate or introspect.** No `--name` lists the filtered set; `--name X`
  returns one whole record; several names return several.

- **`_analysis` on every record.** `submitted` is whether the ownership store
  holds anything for this entity — `lifetime: null` alone cannot say whether
  nobody looked or an agent found no lifecycle role. `pending` lists the pointer
  slots with no ownership block; under `--port-only` / `--wrap-only` it counts
  only that scope's fields.

- **`--file` disambiguates, and is required when a name is ambiguous.** A tag
  can name two unrelated structs BOTH in this target's scope
  (`ossl_record_layer_st` is one in `ssl/record/methods/recmethod_local.h` and
  another in `ssl/quic/quic_tls.c`); the oracle refuses and prints the `--file`
  for each candidate. A name shared with a type OUTSIDE the scope is not
  ambiguous and resolves without `--file`.

- **Submit through `--update`, never by editing a file.**

- **Scope gates enumeration, not lookup.** A listing (`query types`,
  `--port-only`, `--wrap-only`) is this target's inventory. A lookup by
  `--name` reaches every entity the extraction saw, so a type's destructor in
  another scope — `ossl_free_compression_methods_int` in
  `crypto/comp_methods.c` drops the `ssl` type `stack_st_SSL_COMP` — is
  readable, submittable through `--update`, and comes back from
  `query symbols --lifetime-for`. Ownership does not stop at the scope line;
  record it where you find it.

- **Scope gates emission, not content.** An emitted record's `depends_on` /
  `used_by` are codebase-wide whatever its scope, so a wrap-scope node's
  `depends_on.syms` is populated and safe to walk.

- **Enumerate into a stage:** `query types --wrap-only | xargs … translate --name`.
