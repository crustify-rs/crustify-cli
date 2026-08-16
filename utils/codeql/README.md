# `utils/codeql/` - CodeQL substrate + deterministic composers

The fact layer under the pipeline. **Committed, vetted CodeQL queries** extract
the C codebase into CSVs; **deterministic Python composers** join those into the
JSON artifacts each CLI stage consumes. Agents read the results and reason - they
never author queries (ad-hoc agent queries produced run-to-run-variable
manifests; see `docs/PITFALLS.md` sec 2026-05-31). This is the *composer* half of
the composer<->agent split.

## Flow

```
entities/ + edges/ (CodeQL)  -->  T1/T2 CSVs  -->  compose/ (Python)  -->  *.json artifacts
        ^ port_scope.qll (generated from scope.json)
```

## Layout

| path | role |
|---|---|
| `qlpack.yml`, `codeql-pack.lock.yml` | pack manifest (cpp-all dep) |
| `entities/` | **Tier 1** - *what exists* -> T1 CSVs |
| `edges/` | **Tier 2** - *how things connect* -> T2 CSVs |
| `generate_port_scope.py` -> `port_scope.qll` | **Tier 3** - scope predicates regenerated from the target's `scope.json` |
| `compose/` | deterministic composer layer (Python) |
| `fa_*.ql`, top-level loose `.ql` | ad-hoc / scratch - not part of the pipeline |

## Tier 1 - `entities/` (what exists)

`functions`, `macros`, `globals`, `types`, `fields` (per-field type +
scalar/aggregate), `includes`.

## Tier 2 - `edges/` (connectivity)

`function_calls`, `function_addresses`, `global_accesses`,
`macro_expansions`, `field_accesses`, `signature_type_uses`,
`local_type_uses` (body locals / casts / sizeof), `field_type_uses`,
`global_type_uses`, `casts`, `function_pointer_{args,returns}`,
`callback_{call_sites,signature_type_uses}`.

## Composers - `compose/`

| module | produces | consumed by |
|---|---|---|
| `extract_csvs.py` | runs the queries -> T1/T2 CSVs | everything below |
| `scope.py` | scope predicates (def-anchored classify, typedef walk, `type_method_syms`) | all composers |
| `reach.py` | per-entity T2 reach rollups (callers, field accesses, type uses) | manifests, dag |
| `filter_spec.py` / `path_partition.py` | `scope.json` in-scope filter; stem-group -> manifest-dir map | all stages |
| `scope_manifest.py` | in-memory port set; `import_closure.py` adds the import-closure. `analyze scope --dump` snapshots the pair | `analyze scope`, and every stage via `crustify.scope.build` |
| `import_closure.py` | the wrap-scope closure reached from port code | scope / wrap scheduling |
| `types_manifest.py` / `syms_manifest.py` | `types.json` / `syms.json` skeletons + full dependency edges | `analyze types` / `symbols` |
| `deps_dag.py` | in-memory unified layered types+symbols DAG (cast-centrality + fallback/back-fill edges); `analyze dag --dump` snapshots it | `analyze dag`, `query dag`, wrap scheduler |
| `scaffold_manifest.py` | **legacy** — only `sync_workspace` (the shared `rust/` Cargo workspace member list) is still live, called from `bindgen_manifest`. `crates.json` has no producer: it is authored outside the pipeline (see `docs/schemas/crates.md`), and `.rs` stubs come from `crustify.scaffold` | `bindgen` |
| `bindgen_manifest.py` | `<lib>-sys` crate scaffolds - per-kind allowlists + include closure (no `fn main`, no shims) | `bindgen` |
| `audit_manifest.py` | JSON to stdout (per-seed own + naked-ffi surface, tree-wide `global` scan, `totals`); nothing written to disk | `audit` |
| `manifest_merge.py` | union-by-key merge of agent findings into a manifest | `query --update` |
| `check_types_consistency.py` | consistency gate (every op homed once; acyclic) | standalone (`python -m`, manual) |

All artifacts are deterministic from the CSVs + `scope-config.json`; the analyze
`types`/`symbols` skeletons carry judgement fields that the WRAP-stage
agents fill in later, via `query --update`
(ownership, lifecycle, ptr facets) - every other composer output is final.
