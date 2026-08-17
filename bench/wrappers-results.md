# crustify — `<target repo> / <target>`

Campaign: `<repo>` @ `<commit>`, target section `<dirs>` (`<n>` files), import
section `<n>`. Agent backend `<backend>`, model `<model>`, `--billing <mode>`,
`--max-types <n>`, `--parallel-max <n>`, policy `<policy>`.
Deps: crustify-cli `<sha>` (**`<branch>`**), ffibox `<sha>` (**`<branch>`**).
Campaign branch `<branch>`; `<branch>` is the untouched scaffold baseline. Cost
is priced from token counts by `log_cost.py`'s own `parse_usage`, never from
provider-reported dollars.

**Legend**

- `layer` — the unit's own wrap DAG layer
- `id` — `WT<n>` types, `WC<n>` callbacks, numbered across all layers
- `kind` — `struct` / `union` / `enum`; or `callback`, and
  `callback · gate-missed` for one the wrap gate did not admit. A callback
  declares no fields, so its four field columns read `—`
- `fields` — all declared fields
- `port` / `target ptr` — fields a target-section function touches / of those,
  pointers; read off the scaffolded `// Field: <tag>.<field>` anchors
- `wrapped` — fields given an accessor (promoted `/// Field:` anchors), counted
  as DISTINCT `type.field` paths; `—` = wrapped with no field accessor (opaque)
- `newtypes` — distinct Rust types carrying a `/// Wraps: <tag>` anchor; `1` is
  a plain 1:1 wrap, `>1` where one C type needs several representations (an
  owned handle beside a borrowed view, a by-value beside a by-pointer form)
- `target fns` — every target-section function needing the symbol, tree-wide
- `deps` — import types/callbacks the symbol needs
- `wrappers` — distinct safe fns emitted over the one C routine; `>1` where the
  signature forked (a slice-taking beside a `CStr`-taking form, a fallible
  beside an infallible one)
- `batch` — the agent that emitted it. Symbols pool, so their cost is per
  batch, not per symbol — see the batches table
- `$` / `wall` / `loc` — that agent's own cost, its elapsed time, and the `.rs`
  insertions of its landing commit. `wall` is `ended_at − started_at` from the
  agent's own `usage.json`, so it INCLUDES the per-worktree C rebuild
- `$/unit` / `$/loc` / `$/field` — that row's `$` over its units, its `loc`, or
  its declared fields
- `↖ batched` — shares the row above's agent; one usage record covers both
- `canon` — the branch it was promoted to; `running` = agent in flight; blank =
  not yet scheduled
- In a batches row, `wall` is the layer's LONGEST agent — what the layer would
  cost with every batch spawned at once — and the parenthetical is the
  serial-sum multiple. A Σ row sums the columns it can and carries the same
  longest-agent reading for `wall`

## Raw lifetime discovery

Goal: turn the untyped lifecycle primitives into Rust lifetime contracts before
any wrapper needs one. `--lifetime-for void` then `--lifetime-for string`, one
agent each, objective `raw` (set by the tier, not `--objective`). `strategies`
counts the deleter/cloner ZSTs emitted; the four trait columns count the
`unsafe impl`s that bind them.

| tier | symbols submitted | strategies | CDropped | CCloned | CLenDropped | CLenCloned | $ | wall |
|---|---|---|---|---|---|---|---|---|
| void | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` |
| string | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` |
| **Σ** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>m<n>s`** |


## Import closure

Goal: wrap everything the target section reaches but does not own — first every
import-section type and callback, then the import-section symbols the target
depends on. Two waves, both `--objective wrap`.

### Types and callbacks

The import type closure: every import-section type and callback, bottom-up by
its own DAG layer, `--max-types <n>` so each type is its own agent. `<n>` of
`<n>` units; `<tag>` is gate-missed (see Notes).

| layer | id | unit | kind | fields | port | target ptr | wrapped | newtypes | $ | wall | loc |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `<N>` | WT1 | `<tag>` | struct | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | $`<n>` | `<n>m<n>s` | `<n>` |
| `<N>` | WT2 | `<tag>` | enum | `<n>` | — | — | — | `<n>` | $`<n>` | `<n>m<n>s` | `<n>` |
| `<N>` | WC1 | `<tag>` | callback | — | — | — | — | `<n>` | $`<n>` | `<n>m<n>s` | `<n>` |
| **Σ `<count>`** | | | | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`$<n>`** | | **`<n>`** |

### Batches — types

| layer | units | loc | $ | wall (longest) | wall (actual) | serial Σ | $/unit | $/loc |
|---|---|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` | **`<n>m<n>s`** (queued) | `<n>h<n>m` (`<n>`x) | `$<n>` | `$<n>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | — | **`<n>h<n>m<n>s`** | **`<n>h<n>m`** (`<n>`x) | **`$<n>`** | **`$<n>`** |

### Symbols

The import symbol closure: the functions and globals target-section code at
layers `<L0>`–`<Ln>` calls but does not own — the direct symbol deps of all
`<n>` target-section symbols at those layers, intersected with the import
closure. `<n>` units, `<n>` distinct C items.

| layer | symbol | kind | target fns | deps | wrappers | batch | canon |
|---|---|---|---|---|---|---|---|
| `<N>` | `<name>` | function_exported | `<n>` | — | `<n>` | `<batch>` | `<branch>` |
| `<N>` | `<name>` | function_exported | `<n>` | `<tag>`, `<tag>` | `<n>` | `<batch>` | `<branch>` |
| `<N>` | `<name>` | global | `<n>` | — | `<n>` | `<batch>` | `<branch>` |
| **Σ `<count>`** | | | **`<n>`** | | **`<n>`** | **`<n>` batches** | **`<n>`/`<n>`** |

### Batches — symbols

| layer | units | loc | $ | wall | $/unit | $/loc |
|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` (`<n>`x) | `$<n>` | `$<n>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>h<n>m<n>s`** (`<n>`x, sum = session wall) | **`$<n>`** | **`$<n>`** |

## God objects

Goal: the `<n>` target-section types with more than `<n>` declared fields —
`<tag>`, `<tag>`, `<tag>` — and their transitive closure, which `--transitive`
expands across symbols so a type reachable only through a function comes along.
`<n>` units over `<n>` dependency layers.

### Types and callbacks

| layer | id | unit | kind | fields | port | target ptr | wrapped | newtypes | $ | wall | loc | canon |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `<N>` | WC1 | `<tag>` | callback | `—` | `—` | `—` | `—` | `<n>` | $`<n>` | `<n>m<n>s` | `<n>` | `<branch>` |
| `<N>` | WT1 | `<tag>` | struct | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | $`<n>` | `<n>m<n>s` | `<n>` | `<branch>` |
| **Σ `<count>`** | | | | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **$`<n>`** | | **`<n>`** | **`<n>`/`<n>`** |

### Cost — types

| units | fields | $ | wall | $/unit | $/field |
|---|---|---|---|---|---|
| **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>h<n>m<n>s`** | **`$<n>`** | **`$<n>`** |

### Batches — by layer

| layer | units | loc | $ | wall | $/unit | $/loc |
|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` (`<n>`x) | `$<n>` | `$<n>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>h<n>m<n>s`** (sum, = session wall) | **`$<n>`** | **`$<n>`** |

## Safety audit

`crustify-cli <repo> <target> audit --all`, `global` section — tree-wide, not
per seed. Two snapshots: at the import-closure promotion and at the final tree.

### Headline — at the import closure (`<sha>`)

| unsafe loc | % of loc | blocks | % in `impl T` | naked raw ptrs | `&mut` | field proj |
|---|---|---|---|---|---|---|
| `<n>` | `<n>`% | `<n>` | `<n>`% | `<n>` | `<n>` | `<n>` |

### All metrics — at the import closure

| metric | value | reading |
|---|---|---|
| `code_lines` | `<n>` | non-blank, non-comment source lines (denominator) |
| `total_stmts` | `<n>` | statements |
| `unsafe_blocks` | `<n>` | count of `unsafe { }` blocks |
| `unsafe_block_stmts` | `<n>` | statements inside them |
| `unsafe_block_code_lines` | `<n>` | **`<n>`%** of `code_lines` |
| `unsafe_blocks_wrapper_impl` | `<n>` | `<n>`% — inside `impl <wrapper T>` |
| `unsafe_blocks_ffi_export` | `<n>` | inside the C-ABI gateway |
| `wrapper_impl_macro` | `<n>` | macro-generated accessors |
| `wrapper_impl_handwritten` | `<n>` | hand-written ones |
| `rp_args` | `<n>` | raw-ptr positions in arguments |
| `rp_rets` | `<n>` | raw-ptr positions in returns |
| **total positions** | `<n>` | args + rets; disjoint, so this is the surface |
| `rp_seam` | `<n>` | `<n>`% sanctioned: seam fn / `mod ffi_export` / `extern "C"` / ptr-to-own-`Self` |
| **smell (total − seam)** | `<n>` | `<n>`% — the reportable remainder |
| `rp_wrapped` | `<n>` | **of the smell**: pointee is a C type that HAS a wrapper — the actionable defect |
| `rp_in_wrapper` | `<n>` | **of the smell**: inside a wrapper impl — the least excusable placement |
| `ref_to_type_wrapper` | `<n>` | `&W`/`&mut W` on an inline-`CType` wrapper — **target 0** |
| `field_proj_wrapped` | `<n>` | projection VOLUME — shares one HIR shape with `addr_of!`, not a violation |
| `field_proj_outside_impl` | `<n>` | projections outside any accessor — **target 0** |
| `field_ref_wrapped` | `<n>` | `&(*p).field` — the rule principles.md states — **target 0** |
| `raw_ptr_derefs` | `<n>` | `*p` on a raw pointer (volume) |
| `void_ptr_sanctioned` | `<n>` | `*c_void` in a seam / `ffi_export` / `extern "C"` signature |
| `void_ptr_smell` | `<n>` | `*c_void` elsewhere |

### Headline — final, after the god objects (`<sha>`)

| unsafe loc | % of loc | blocks | % in `impl T` | naked raw ptrs | `&mut` | field proj |
|---|---|---|---|---|---|---|
| `<n>` | `<n>`% | `<n>` | `<n>`% | `<n>` | `<n>` | `<n>` |

### All metrics — final

| metric | value | reading |
|---|---|---|
| `code_lines` | `<n>` | non-blank, non-comment source lines (denominator) |
| `total_stmts` | `<n>` | statements |
| `unsafe_blocks` | `<n>` | count of `unsafe { }` blocks |
| `unsafe_block_stmts` | `<n>` | statements inside them |
| `unsafe_block_code_lines` | `<n>` | **`<n>`%** of `code_lines` |
| `unsafe_blocks_wrapper_impl` | `<n>` | `<n>`% — inside `impl <wrapper T>` |
| `unsafe_blocks_ffi_export` | `<n>` | inside the C-ABI gateway |
| `wrapper_impl_macro` | `<n>` | macro-generated accessors |
| `wrapper_impl_handwritten` | `<n>` | hand-written ones |
| `rp_args` | `<n>` | raw-ptr positions in arguments |
| `rp_rets` | `<n>` | raw-ptr positions in returns |
| **total positions** | `<n>` | args + rets; disjoint, so this is the surface |
| `rp_seam` | `<n>` | `<n>`% sanctioned: seam fn / `mod ffi_export` / `extern "C"` / ptr-to-own-`Self` |
| **smell (total − seam)** | `<n>` | `<n>`% — the reportable remainder |
| `rp_wrapped` | `<n>` | **of the smell**: pointee is a C type that HAS a wrapper — the actionable defect |
| `rp_in_wrapper` | `<n>` | **of the smell**: inside a wrapper impl — the least excusable placement |
| `ref_to_type_wrapper` | `<n>` | `&W`/`&mut W` on an inline-`CType` wrapper — **target 0** |
| `field_proj_wrapped` | `<n>` | projection VOLUME — shares one HIR shape with `addr_of!`, not a violation |
| `field_proj_outside_impl` | `<n>` | projections outside any accessor — **target 0** |
| `field_ref_wrapped` | `<n>` | `&(*p).field` — the rule principles.md states — **target 0** |
| `raw_ptr_derefs` | `<n>` | `*p` on a raw pointer (volume) |
| `void_ptr_sanctioned` | `<n>` | `*c_void` in a seam / `ffi_export` / `extern "C"` signature |
| `void_ptr_smell` | `<n>` | `*c_void` elsewhere |

### What the god objects moved

| metric | post-W2 | post-W3 | |
|---|---|---|---|
| `code_lines` | `<n>` | `<n>` | |
| unsafe % | `<n>`% | `<n>`% | |
| total rp positions | `<n>` | `<n>` | |
| `rp_seam` | `<n>` | `<n>` | |
| smell | `<n>` | `<n>` | |
| **`rp_wrapped`** | **`<n>`** | **`<n>`** | |
| `rp_in_wrapper` | `<n>` | `<n>` | |
| `field_ref_wrapped` | `<n>` | `<n>` | |
| `ref_to_type_wrapper` | `<n>` | `<n>` | |

## Notes

The only prose outside the setup and legend above: pitfalls, findings, and the
context each table cannot carry. One `###` subsection per finding, titled by
what it is about.

> Gate misses and anything the oracle and `translate` disagreed on; a wave that
> was superseded and why; what each wave's diff actually contained beyond its
> row counts; where a metric moved and what moved it. Everything else stays in
> the tables.
