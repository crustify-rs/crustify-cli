# Wrap closure — `<target>`

**Legend**

- `layer` — the unit's own wrap DAG layer
- `id` — `WT<n>` types, `WC<n>` callbacks, numbered across all layers
- `kind` — `struct` / `union` / `enum`; or `callback`, and
  `callback · gate-missed` for one the wrap gate did not admit. A callback
  declares no fields, so its four field columns read `—`
- `fields` — all declared fields (T1 `fields.csv`)
- `port` / `port ptr` — fields a port-scope function touches / of those,
  pointers; read off the scaffolded `// Field: <tag>.<field>` anchors
- `wrapped` — fields given an accessor (promoted `/// Field:` anchors), counted
  as DISTINCT `type.field` paths; `—` = wrapped with no field accessor (opaque)
- `newtypes` — distinct Rust newtypes the unit forked into; `1` is a plain
  1:1 wrap, `>1` where one C type needs several representations (an owned
  handle beside a borrowed view, a by-value beside a by-pointer form)
- `port fns` — every port function needing the symbol, tree-wide
- `deps` — wrap types/callbacks the symbol needs
- `wrappers` — distinct safe fns emitted over the one C routine; `>1` where the
  signature forked (a slice-taking beside a `CStr`-taking form, a fallible
  beside an infallible one)
- `batch` — the agent that emitted it. Symbols pool, so their cost is per
  batch, not per symbol — see the batches table
- `$` / `wall` / `loc` — that agent's own cost, its elapsed time, and the `.rs`
  insertions of its landing commit. `wall` is `ended_at − started_at` from the
  agent's `wrap_<unit>.usage.json`, so it INCLUDES the per-worktree C rebuild.
  `$` is priced from token counts by `utils/log_cost.py`, never from
  provider-reported dollars, so it is comparable across billing modes and hosts
- `$/unit` / `$/loc` / `$/field` — that row's `$` over its units, its `loc`, or
  its declared fields
- A callback is a `node_kind == "symbol"` node, so `form_units` emits it as a
  sym-unit and it pools under `--max-syms` like a function — hence its own
  batches table. A type pools under `--max-types`, which the single-row types
  cost table assumes is `1`
- `↖ batched` — shares the row above's agent; one usage record covers both
- `canon` — the branch it was promoted to; `running` = agent in flight; blank =
  not yet scheduled
- In a batches row, `wall` is the layer's LONGEST agent, i.e. what the layer
  would cost with every batch spawned at once; the parenthetical is the
  serial-sum multiple. A Σ row sums the columns it can and carries the same
  longest-agent reading for `wall`

## Types and callbacks (`<count>`)

| layer | id | unit | kind | fields | port | port ptr | wrapped | newtypes | $ | wall | loc | canon |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `<N>` | WT1 | `<tag>` | struct | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` | `<n>` | `<branch>` |
| `<N>` | WT2 | `<tag>` | struct | `<n>` | `<n>` | `<n>` | — | `<n>` | `$<n>` | `<n>m<n>s` | `<n>` | `<branch>` |
| `<N>` | WC1 | `<tag>` | callback | — | — | — | — | `<n>` | `$<n>` | `<n>m<n>s` | `<n>` | `<branch>` |
| `<N>` | WC2 | `<tag>` | callback | — | — | — | — | `<n>` | ↖ batched | ↖ | ↖ | `<branch>` |
| `<N>` | WC3 | `<tag>` | callback · **gate-missed** | — | — | — | — | | | | | |
| **Σ `<count>`** | | | | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>`** (`<n>`x) | **`<n>`** | **`<n>`/`<n>`** |

### Cost — types

One row: a type is its own batch, so there is no per-layer batch structure to
report.

| units | fields | $ | wall | $/unit | $/field |
|---|---|---|---|---|---|
| **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>`** | **`$<n>`** | **`$<n>`** |

### Batches — callbacks

| layer | units | loc | $ | wall | $/unit | $/loc |
|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>` (`<n>`x) | `$<n>` | `$<n>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>`** | **`$<n>`** | **`$<n>`** |

## Symbols (`<count>`)

| layer | symbol | kind | port fns | deps | wrappers | batch | canon |
|---|---|---|---|---|---|---|---|
| `<N>` | `<name>` | function | `<n>` | `<tag>`, `<tag>` | `<n>` | `<batch>` | `<branch>` |
| `<N>` | `<name>` | function | `<n>` | — | `<n>` | `<batch>` | `<branch>` |
| `<N>` | `<name>` | global | `<n>` | — | `<n>` | `<batch>` | `<branch>` |
| `<N>` | `<name>` | function | `<n>` | — | | | |
| **Σ `<count>`** | | | **`<n>`** | | **`<n>`** | **`<n>` batches** | **`<n>`/`<n>`** |

### Batches

| layer | units | loc | $ | wall | $/unit | $/loc |
|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>` (`<n>`x) | `$<n>` | `$<n>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>`** | **`$<n>`** | **`$<n>`** |


## Safety audit

`crustify-cli <repo> <target> audit`, `global` section — tree-wide, not per seed.

| unsafe loc | % of loc | blocks | % in `impl T` | naked raw ptrs | `&mut` | field proj |
|---|---|---|---|---|---|---|
| `<n>` | `<n>`% | `<n>` | `<n>`% | `<n>` | `<n>` | `<n>` |

- `unsafe loc` / `% of loc` — `unsafe_block_code_lines` (non-blank,
  non-comment) over `code_lines`
- `blocks` / `% in impl T` — `unsafe_blocks`, and the share that is
  `unsafe_blocks_wrapper_impl`; the remainder sits outside a wrapper's own impl
- `naked raw ptrs` — `rp_outside_args` + `rp_outside_rets`: raw pointers in
  signatures outside both wrapper impls and `mod ffi_export`
- `&mut` — `mut_borrow_wrapper`: `&mut W` (including `&mut self`) in a
  signature, `W` a wrapper. The interior-mutability discipline holds at `0`
- `field proj` — `field_proj_wrapped`: `(*p).field` on a wrapped C type,
  bypassing the accessor

Also emitted, if a run wants them: `unsafe_blocks_ffi_export`;
`wrapper_impl_macro` vs `wrapper_impl_handwritten`; the seam split
`rp_wrap_nonseam_args` / `_rets` and the `_wrapped` subsets of both regions;
`void_ptr_sanctioned` vs `void_ptr_smell`; `raw_ptr_derefs` and
`raw_ptr_derefs_outside_impl`; `field_proj_outside_impl`; `unsafe_block_stmts`
over `total_stmts`; and the `*_sites` arrays giving `(file, line)` for the
naked, raw-ptr, void-ptr, field-projection and raw-deref categories.

## Notes

> The only section where the orchestrator leaves any worthy notes regarding the campaign:
> pitfalls, findings, etc. Everything else if free of prose.