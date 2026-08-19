# `utils/codeql/compose/` — manifest composers

Deterministic Python that bridges raw CodeQL fact CSVs (Tier 1
entities + Tier 2 edges) to the factual skeletons of the wrap stage's
manifests. The composer layer exists so scope partitioning,
typedef-chain walking, reachability rollup, and edge-join logic
run the same way every agent invocation — not as freelance
prompt code that varies run-to-run.

## Architecture

```
Tier 1+2 queries (utils/codeql/entities/, utils/codeql/edges/)
         │
         │  raw fact CSVs
         ▼
Tier 3 generated lib (utils/codeql/port_scope.qll)
         │
         │  port path set (also surfaces in port/files.json)
         ▼
Composers (this directory)            ← deterministic, no LLM
         │
         │  factual manifest skeletons
         ▼
Agent (CrustifyTypeAnalyzer, CrustifySymbolAnalyzer)
         │
         │  + semantic judgment (kind, ops, ownership, library tag)
         ▼
Final manifests (analysis/scope/{port,wrap}/{types,syms}.json)
```

The agent's job collapses to the genuinely semantic work — macro
kind classification, lifecycle role detection (ctor/dtor/up_ref/
clone), opaque-vs-non-opaque inference, library tag attribution,
ops list curation — and away from the bug-prone CSV joining that
produced run-to-run variability (see `docs/PITFALLS.md`
§2026-05-31).

## Modules

| Module              | Role                                                         |
| ------------------- | ------------------------------------------------------------ |
| `scope.py`          | Definition-anchored scope rule + typedef alias-chain walker. |
| `reach.py`          | T2 edge join with the port-path set; per-entity reach API.   |
| `types_manifest.py` | Emit `port/types.json` + `wrap/types.json` skeletons.        |
| `syms_manifest.py`  | Emit `port/syms.json` + `wrap/syms.json` skeletons.          |

## Run order

Composers depend on the upstream layers in this order:

1. **`CrustifyFileAnalyzer`** produces the target's file set + per-file
   include graph.
2. **`generate_port_scope.py`** regenerates `port_scope.qll` from
   `scope.json`'s `targeted.files` so T2 queries that import it see the
   correct path set.
3. **Tier 1 + Tier 2 queries** run via `codeql query run` against
   the CodeQL database. Outputs are BQRS files; decode each to CSV
   via `codeql bqrs decode --format=csv`. The CSVs live in two
   sibling directories (the composers don't care about the exact
   paths — pass them via `--t1` / `--t2`).
4. **Composers** read the CSVs + `port/files.json` and write the
   factual manifest skeletons.
5. **`CrustifyTypeAnalyzer` + `CrustifySymbolAnalyzer`** consume
   the composer outputs as input, apply semantic judgment, and
   write the final manifests under `analysis/scope/`.

The CrustifyFileAnalyzer step (1) stays agent-driven for now; the
composer pipeline kicks in from step 3 onward.

## Invocation

Each composer is a Python module under `utils.codeql.compose` and
also runs as a standalone script via `python3 -m`. From the
crustify repo root:

```bash
# Compose the types manifests.
python3 -m utils.codeql.compose.types_manifest \
    --t1 <t1-csv-dir> \
    --t2 <t2-csv-dir> \
    --target <project-root> \
    --out-dir <where-to-write-port_types.json-and-wrap_types.json>

# Compose the syms manifests.
python3 -m utils.codeql.compose.syms_manifest \
    --t1 <t1-csv-dir> \
    --t2 <t2-csv-dir> \
    --target <project-root> \
    --out-dir <where-to-write-port_syms.json-and-wrap_syms.json>
```

Each composer is independent — the syms composer doesn't depend on
the types composer's output and vice versa. Both share the same T1
type CSV for typedef alias resolution (`syms_manifest.py` uses it
to scope-split `depends_on.types` via the typedef chain).

`scope.py` can be self-tested as a one-shot scope-distribution
report:

```bash
python3 utils/codeql/compose/scope.py \
    --csv-dir <t1-csv-dir> \
    --target <project-root>
```

It prints per-kind port/wrap counts useful for sanity-checking
after a database refresh.

## What the composers fill vs. what the agent fills

The contract is documented per-module in each composer's docstring.
At a glance:

| Manifest field                  | Composer | Agent |
| ------------------------------- | -------- | ----- |
| `name`                          | ✓        |       |
| `kind` for functions / globals  | ✓        |       |
| `kind` for macros               |          | ✓     |
| `library`                       |          | ✓ (BUILD.md §1) |
| `declared_in` / `defined_in`    | ✓        |       |
| `type` (signature / global type / macro body) | ✓ |   |
| `called_by.call` / `called_by.ref` for fns / globals | ✓ | |
| `called_by` for macros (raw expansion sites) | ✓ pre-fill | ✓ reclassify per kind |
| `depends_on.syms` for functions | ✓ scope-split   |       |
| `depends_on.syms` for macros    |          | ✓ (body parse) |
| `depends_on.syms` for globals' initializers |       | ✓ (out of T2 scope) |
| `depends_on.types` for functions | ✓ scope-split via typedef chain | |
| `typedef[]` alias list          | ✓        |       |
| `non_opaque_in` (functions touching fields) | ✓ | |
| `fields[]` + `used_by`          | ✓        |       |
| `fields[].type` (non-scalar) — sourced from `entities/fields.ql` | ✓ | |
| `opaque_in` (functions using opaquely) | ✓ | |
| `fields[].locked_by`            |          | ✓     |

### Imported-section inclusion gate

Composers emit an imported entry **only when** some targeted-side site reaches
it, or -- on a wrap campaign -- when `api_headers` declares it. The reach signal is
kind-specific:

| Kind                   | Reach signal                                                |
| ---------------------- | ----------------------------------------------------------- |
| `function_*` (non-TU)  | `function_calls` caller in port OR `function_addresses` enclosing in port |
| `global_extern`        | `global_accesses` enclosing in port                         |
| `macro_*`              | `macro_expansions` enclosing-fn in port (fn scope) OR invocation-file in port (file scope) |
| struct types           | `field_accesses` access-file in port OR `signature_type_uses` fn in port OR `local_type_uses` fn in port (all with typedef chain resolution) |

### `fields[].type` composition

The schema rule is: emit `fields[].type` ONLY for non-scalar fields
(struct, union, enum, pointer-to-struct, typedef chain ending at an
aggregate). Scalar fields (`int`, `size_t`, `char *`, `void *`,
arrays of primitives) omit the key entirely.

The composer reads the per-field `(field_type, is_scalar)` row from
`entities/fields.ql`'s output and emits `type` only when
`is_scalar` is false. The classification predicate in
`fields.ql` walks both `DerivedType.getBaseType()` (pointers /
arrays / qualifiers) AND `TypedefType.getBaseType()` (typedef
aliases — `TypedefType` is NOT a subclass of `DerivedType` in
this cpp-all version, so the second branch is load-bearing).

Verified across the openssl-crustify-statem statem partition: 255
non-scalar fields carry `type` keys, 675 scalar fields omit them.
Spot-checks on representative entries:

  - `ssl_connection_st.cert` — `"cert_st *"` ✓ (non-scalar)
  - `ssl_connection_st.ca_names` — `"stack_st_X509_NAME *"` ✓
  - `buf_mem_st.data` — omitted ✓ (`unsigned char *`, scalar)
  - `buf_mem_st.length` — omitted ✓ (`size_t` → `unsigned long`,
    scalar after typedef chain unwrap)
  - `ossl_statem_st.hand_state` — `"OSSL_HANDSHAKE_STATE"` ✓
    (typedef to anonymous enum — enum classifies as aggregate per
    schema, so `type` is emitted)

### `opaque_in` / `non_opaque_in` composition

Both partitions are factually derivable from T2:

```
mention_set(T) = signature_type_uses(T)        # return type + params
              ∪ local_type_uses(T)             # locals, casts, sizeof
              ∪ (each typedef alias of T → same lookups)   # belt-and-suspenders

field_users(T) = field_accesses(T)             # struct.field reads/writes/&

non_opaque_in(T) = field_users(T)              # split port/wrap
opaque_in(T)     = mention_set(T) − field_users(T)
```

The two sets are disjoint by construction (a function that
performs a `FieldAccess` on T is in `non_opaque_in`, never in
`opaque_in` — regardless of how many other signature / local /
cast mentions of T it also has). Verified empirically on the
openssl-crustify-statem statem partition: zero disjointness
violations across 73 import-section struct entries.

The composer's emission of `opaque_in` and `non_opaque_in` is
**final**. The agent does NOT remove function names from either
list when classifying a function as an op of the type — `ops` and
the two `*_in` lists are orthogonal axes (semantic lifecycle role
vs mechanical field-access pattern). Overlap is expected and
intended: `SSL_new` is an op of `ssl_st` AND appears in
`non_opaque_in.wrap` because it initialises fields. The agent
never has to re-derive opaque/non-opaque from raw CSVs.

The TU-bounded kinds `function_static`, `function_inline_tu`, and
`global_static` are skipped entirely from the wrap manifest by C
language rules — a target-side call edge to one of those would
indicate a scope-rule mis-classification, not a real wrap entry.

## Versioning and the cpp-all API

The composers consume the T1+T2 CSV schemas documented in each
query's `# cols:` header. If a query's schema changes, the
corresponding composer adapts; the contract between composers and
query CSVs is the only API surface that needs to stay stable
across cpp-all version bumps. The agent input contract (the
manifest skeleton fields the composer fills) is independent of
both the cpp-all version and the CSV schema, so query-level
churn doesn't propagate to prompts.

## Why not write composers in CodeQL?

CodeQL is the right tool for the fact-extraction layer (T1+T2):
it has direct access to the AST, predicate evaluation is fast,
and the result format is uniformly typed. It's the wrong tool for
composition — joins across multiple result sets need post-query
glue; per-entity reachability rollups don't fit the relational
model cleanly; manifest emission needs JSON shape control CodeQL
doesn't natively provide. Python at the composition layer keeps
each tier focused on what it's good at.
