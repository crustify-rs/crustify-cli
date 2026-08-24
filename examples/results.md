# crustify — `<target repo> / <target>`

Copy this template into the campaign checkout, then fill it as work lands.

## Campaign

- **target repo** — `<repo>` @ `<commit>`
- **target** — `<target>`
- **campaign objective** — `<port | wrap>`
- **`impl_files`** — `<dirs / files>`
- **`api_headers`** — `<dirs / files>`
- **agent backend** — `<codex | claude>`
- **model** — `<provider>/<model>`
- **`--billing`** — `<api | subscription>`
- **`--max-types`** — `<n>`
- **`--max-syms`** — `<n>`
- **`--max-loc`** — `<n>`
- **`--min-fields`** — `<n>`
- **`--parallel-max`** — `<n>`
- **branch** — `<branch>`, tip `<sha>`
- **deps** — crustify-cli `<sha>` (`<branch>`), ffibox `<sha>` (`<branch>`)

## Review pass

`--objective review`, LLM-as-a-Judge over the landed waves.

- **agent backend** — `<codex | claude>`
- **model** — `<provider>/<model>`
- **`--billing`** — `<api | subscription>`
- **`--max-types`** — `<n>`
- **`--max-syms`** — `<n>`
- **`--max-loc`** — `<n>`
- **`--min-fields`** — `<n>`
- **`--parallel-max`** — `<n>`
- **branch** — `<branch>`, tip `<sha>`
- **agents** — `<n>`, over `<n>` session(s)

`rv`-prefixed columns below carry the review pass; the unprefixed ones remain
the campaign's.

## UB pass

`crustify-audit ub`, an agentic hunt for undefined behaviour reachable from the
crate's SAFE APIs.

- **agent backend** — `<codex | claude>`
- **model** — `<provider>/<model>`
- **`--billing`** — `<api | subscription>`
- **`--timeout`** — `<n>` min — a wall BUDGET, not a kill switch: agents are
  spawned one after another until it is reached and each finishes on its own,
  so the run overshoots by however long the last one takes. `0` runs exactly
  one agent
- **subject** — `<sub-campaign>` at `<sha>`
- **agents** — `<n>`, `<n>h<n>m<n>s` wall, `$<n>`
- **advisories** — `<n>` at `crustify/audit/advisories/`
- **patch** — `<branch>` at `<sha>`; `<merged | left unpromoted — reason>`

`ub`-prefixed columns carry this pass.

## Legend

- `DAG layer` — the unit's own wrap DAG layer
- `kind` — `struct` / `union` / `enum` for a type; `callback`; `function` for
  every symbol, whatever linkage the C declaration carries
- `fields` — all declared fields
- `target fields` / `target ptr` — fields a target-section function touches / of
  those, pointers
- `wrapped fields` — fields given an accessor, counted as DISTINCT `type.field`
  paths; `—` = wrapped with no field accessor (opaque)
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
- `$/symbol` / `$/type` — a batch holds one kind or the other, so one of the
  two reads `—`; on a Σ row each divides that kind's own cost by its own count.
  The review batch tables are split by kind instead, so each carries a single
  `rv $/unit`
- `↖ batched` — shares the row above's agent; one usage record covers both
- `rv $` / `rv wall` / `rv loc` — the REVIEW agent's own cost, elapsed time, and
  net `.rs` line delta (`+ins/-del`) of its landing commit
- `rv batch` — the review batch that judged this unit or layer. The review pass
  is scheduled separately and packs under its own budgets, so the mapping to
  emitting batches is many-to-many; list every review batch that touched the
  row, or `<n> batches` on a Σ row
- `ub $` / `ub wall` — the UB pass's cost and elapsed time over that
  sub-campaign; `—` where the optional pass did not run
- `verdict` — what the judge concluded: `held` = analysis and code confirmed as
  emitted, `fixed` = a defect in the emitted Rust corrected, `record` = an
  ownership finding resubmitted through the oracle. Several may apply
- In a batches row, `wall` is the layer's LONGEST agent — what the layer would
  cost with every batch spawned at once — and the parenthetical is the
  serial-sum multiple. A Σ row sums the columns it can and carries the same
  longest-agent reading for `wall`

## Overview

One row per sub-campaign, in the order they ran, plus a row for the
orchestrator's own supervision cost. A review pass is a sub-campaign in its own
right, not a column on the wave it judges: the oracle re-batches the reviewed
units, so its layer and batch distribution rarely matches the wrap or port wave
underneath it, and averaging the two would hide exactly that.

`ub` columns stay `—` unless the optional agentic UB pass ran over that
sub-campaign; it is off by default and needs explicit approval. It gets columns
rather than a row because it is not oracle-batched at all — one budgeted agent
chain over a whole tree, with no units of its own to divide by.

| sub-campaign | objective | nr types | nr symbols | session wall | total | $/type | $/sym | ub wall | ub $ |
|---|---|---:|---:|---|---:|---:|---:|---|---:|
| `<waves>-<name>` | raw lifetime | `0` | `<n>` | `<n>m<n>s` | `$<n>` (`<model>`) | — | `$<n>` | — | — |
| `<waves>-<name>` | wrap | `<n>` | `<n>` | `<n>h<n>m<n>s` | `$<n>` (`<model>`) | `$<n>` | `$<n>` | `<n>m<n>s` | `$<n>` (`<model>`) |
| `<waves>-<name>` | review | `<n>` | `<n>` | `<n>h<n>m<n>s` | `$<n>` (`<model>`) | `$<n>` | `$<n>` | — | — |
| `<waves>-<name>` | port | `<n>` | `<n>` | `<n>h<n>m<n>s` | `$<n>` (`<model>`) | `$<n>` | `$<n>` | — | — |
| `<waves>-<name>` | review | `<n>` | `<n>` | `<n>h<n>m<n>s` | `$<n>` (`<model>`) | `$<n>` | `$<n>` | — | — |
| orchestrator | orchestration | `<n>` | `<n>` | — | `$<n>`+ (`<model>`) | — | — | — | — |
| **Σ recorded agents** | | **`<n>`** | **`<n>`** | | **`$<n>`** | **`$<n>`** | **`$<n>`** | | **`$<n>`** |

A review row's `nr types` / `nr symbols` count what that pass actually judged,
which may be fewer than the wave emitted — the oracle drops lifecycle
primitives it reviews with their owning type. Say so in a footnote when the
counts differ.

The orchestrator row carries `+` because its own session is still running when
the table is written; record the figure at hand-off and say so.

## Raw lifetime discovery

Goal: turn the untyped lifecycle primitives into Rust lifetime contracts before
any wrapper needs one. Oracle `schedule --lifetime-for void` then
`schedule --lifetime-for string`, one
agent each, objective `raw` (set by the tier, not `--objective`). `strategies`
counts the deleter/cloner ZSTs emitted; the four trait columns count the
`unsafe impl`s that bind them.

| tier | symbols submitted | strategies | CDropped | CCloned | CLenDropped | CLenCloned | $ | wall |
|---|---|---|---|---|---|---|---|---|
| void | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` |
| string | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` |
| **Σ** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>m<n>s`** |

## Target set

What the campaign wrapped and in what order: types and callbacks first,
bottom-up by DAG layer, then the symbols over them.

### Types and callbacks

| DAG layer | unit | kind | fields | target fields | target ptr | wrapped fields | newtypes | $ | wall | loc | rv $ | rv wall | rv loc | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `<N>` | `<tag>` | struct | `<n>` | `<n>` | `<n>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` | `<n>` | `$<n>` | `<n>m<n>s` | `+<n>/-<n>` | held |
| `<N>` | `<tag>` | enum | `<n>` | — | — | — | `<n>` | ↖ batched | ↖ batched | ↖ batched | ↖ batched | ↖ batched | ↖ batched | held · fixed |
| `<N>` | `<tag>` | callback | — | — | — | — | `<n>` | `$<n>` | `<n>m<n>s` | `<n>` | `$<n>` | `<n>m<n>s` | `+<n>/-<n>` | record · fixed |
| **Σ `<count>`** | | | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`<n>`** | **`$<n>`** | | **`<n>`** | **`$<n>`** | | **`+<n>/-<n>`** | **`<n>`/`<n>` reviewed** |

### Batches — types

`rv batch` names the review batch or batches that later judged this layer's
units. It is frequently many-to-one in both directions: the review pass
re-batches under its own budgets, so one wrap batch can be split across several
review batches and one review batch can span several wrap layers.

| DAG layer | units | loc | $ | wall (longest) | wall (actual) | serial Σ | $/unit | $/loc | rv batch |
|---|---|---|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` | **`<n>m<n>s`** | `<n>h<n>m` (`<n>`x) | `$<n>` | `$<n>` | `<batch>` |
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` | **`<n>m<n>s`** | `<n>h<n>m` (`<n>`x) | `$<n>` | `$<n>` | `<batch>`, `<batch>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | — | **`<n>h<n>m<n>s`** | **`<n>h<n>m`** (`<n>`x) | **`$<n>`** | **`$<n>`** | **`<n>` batches** |

### Symbols

| DAG layer | symbol | kind | target fns | deps | wrappers | batch | rv batch | verdict |
|---|---|---|---|---|---|---|---|---|
| `<N>` | `<name>` | function | `<n>` | — | `<n>` | `<batch>` | `<batch>` | held |
| `<N>` | `<name>` | function | `<n>` | `<tag>`, `<tag>` | `<n>` | `<batch>` | `<batch>` | fixed — `<what>` |
| `<N>` | `<name>` | global | `<n>` | — | `<n>` | `<batch>` | `<batch>` | held |
| **Σ `<count>`** | | | **`<n>`** | | **`<n>`** | **`<n>` batches** | **`<n>` batches** | **`<n>` held · `<n>` fixed** |

### Batches — symbols

| DAG layer | units | loc | $ | wall | $/unit | $/loc | rv batch |
|---|---|---|---|---|---|---|---|
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` (`<n>`x) | `$<n>` | `$<n>` | `<batch>` |
| `<N>` | `<n>` | `<n>` | `$<n>` | `<n>m<n>s` (`<n>`x) | `$<n>` | `$<n>` | `<batch>`, `<batch>` |
| **Σ** | **`<n>`** | **`<n>`** | **`$<n>`** | **`<n>h<n>m<n>s`** (`<n>`x, session wall) | **`$<n>`** | **`$<n>`** | **`<n>` batches** |

### Batches — review types

One agent per judged batch. The review pass is scheduled and batched on its
own, so these layers and batches do not line up with `Batches — types` above;
read the two side by side rather than row for row. `rv loc` is the net `.rs`
delta of the landing commit, and a review that confirms without changing code
reads `+0/-0`.

| session | batch | units | rv loc | rv $ | rv wall | rv $/unit | judged |
|---|---|---|---|---|---|---|---|
| `<session>` | `<batch>` | `<n>` types | `+<n>/-<n>` | `$<n>` | `<n>m<n>s` | `$<n>` | `<n>` held · `<n>` fixed |
| **Σ** | **`<n>` agents** | **`<n>` types** | **`+<n>/-<n>`** | **`$<n>`** | **`<n>m<n>s`** (longest; `<n>h<n>m<n>s` serial, `<n>`x) | **`$<n>`** | **`<n>` held · `<n>` fixed** |

### Batches — review symbols

| session | batch | units | rv loc | rv $ | rv wall | rv $/unit | judged |
|---|---|---|---|---|---|---|---|
| `<session>` | `<batch>` | `<n>` symbols | `+<n>/-<n>` | `$<n>` | `<n>m<n>s` | `$<n>` | `<n>` held · `<n>` fixed |
| **Σ** | **`<n>` agents** | **`<n>` symbols** | **`+<n>/-<n>`** | **`$<n>`** | **`<n>m<n>s`** (longest; `<n>h<n>m<n>s` serial, `<n>`x) | **`$<n>`** | **`<n>` held · `<n>` fixed** |

A batch holding both kinds is rare but legal; list it in the table matching its
`kind` and say so in a footnote.

## Safety audit

`crustify-audit <crate> unsafe`, unseeded — tree-wide, not
per seed. Two snapshots: the tree the review pass judged, and the tree it
produced.

| | before review (`<sha>`) | after review (`<sha>`) |
|---|---|---|
| unsafe loc | `<n>` | `<n>` |
| % of loc | `<n>`% | `<n>`% |
| blocks | `<n>` | `<n>` |
| % in `impl T` | `<n>`% | `<n>`% |
| `unsafe fn` | `<n>` | `<n>` |
| ...of which not sanctioned | `<n>` | `<n>` |
| raw-ptr smell | `<n>` | `<n>` |
| void-ptr smell | `<n>` | `<n>` |
| FFI calls | `<n>` | `<n>` |
| `&`/`&mut` on a wrapper | `<n>` | `<n>` |
| field proj outside an accessor | `<n>` | `<n>` |

### All metrics

| metric | before | after | Δ | reading |
|---|---|---|---|---|
| `code_lines` | `<n>` | `<n>` | `<n>` | union of HIR definition spans (denominator); `cfg`-disabled items excluded |
| `total_stmts` | `<n>` | `<n>` | `<n>` | statements |
| `unsafe_blocks` | `<n>` | `<n>` | `<n>` | count of `unsafe { }` blocks, macro-expanded included |
| `unsafe_block_stmts` | `<n>` | `<n>` | `<n>` | statements inside them |
| `unsafe_block_lines` | `<n>` | `<n>` | `<n>` | their lines, every outermost block |
| `unsafe_block_code_lines` | `<n>` | `<n>` | `<n>` | **`<n>`% → `<n>`%** |
| `unsafe_blocks_wrapper_impl` | `<n>` | `<n>` | `<n>` | inside `impl <wrapper T>` |
| `unsafe_blocks_ffi_export` | `<n>` | `<n>` | `<n>` | inside the C-ABI gateway |
| `unsafe_fns` | `<n>` | `<n>` | `<n>` | `unsafe fn` declarations, post-expansion |
| `unsafe_fns_seam` | `<n>` | `<n>` | `<n>` | ...the sanctioned subset |
| **`unsafe fn` smell** | **`<n>`** | **`<n>`** | **`<n>`** | the remainder — read each and accept or fix it |
| `unsafe_fns_pub` | `<n>` | `<n>` | `<n>` | ...of `unsafe_fns`, exported from the crate |
| `unsafe_impls` / `unsafe_traits` | `<n>` / `<n>` | `<n>` / `<n>` | `<n>` | lifecycle contracts asserted once per type |
| `ffi_calls` | `<n>` | `<n>` | `<n>` | calls to a foreign item — the unsafe-FFI-call surface |
| `wrapper_newtypes` | `<n>` | `<n>` | `<n>` | LAYOUT newtypes — `repr(transparent)` over a `repr(C)` type by value, detected structurally |
| `wrapper_newtypes_declared` | `<n>` | `<n>` | `<n>` | the `CCell`-declared count, for comparison |
| `wrapper_declared_nonconformant` | `<n>` | `<n>` | `<n>` | declared but failing the structural test — **target 0** |
| `wrapper_newtypes_undeclared` | `<n>` | `<n>` | `<n>` | structural but undeclared — a hand-written layout newtype |
| `raw_ptr_args` | `<n>` | `<n>` | `<n>` | raw-ptr positions in arguments |
| `raw_ptr_rets` | `<n>` | `<n>` | `<n>` | raw-ptr positions in returns |
| **total positions** | **`<n>`** | **`<n>`** | `<n>` | args + rets; disjoint, so this is the surface |
| `raw_ptr_seam` | `<n>` | `<n>` | `<n>` | sanctioned: seam fn / `mod ffi_export` / `extern "C"` / ptr-to-own-`Self` |
| **smell (total − seam)** | **`<n>`** | **`<n>`** | `<n>` | the non-seam remainder |
| `raw_ptr_wrapped` | `<n>` | `<n>` | `<n>` | **of the smell**: pointee is a C type that HAS a wrapper — the actionable defect |
| `raw_ptr_in_wrapper` | `<n>` | `<n>` | `<n>` | **of the smell**: inside a wrapper impl — the least excusable placement |
| `raw_ptr_derefs` | `<n>` | `<n>` | `<n>` | `*p` on a raw pointer (volume) |
| `ref_to_type_wrapper` | `<n>` | `<n>` | `<n>` | `&`/`&mut` on a layout newtype — **target 0** |
| `field_proj_wrapped` | `<n>` | `<n>` | `<n>` | projection VOLUME — shares one HIR shape with `addr_of!`, not a violation |
| `field_proj_outside_impl` | `<n>` | `<n>` | `<n>` | projections outside any accessor — **target 0** |
| `field_ref_wrapped` | `<n>` | `<n>` | `<n>` | `&(*p).field` — forbidden by the translator playbook — **target 0** |
| `void_ptr_sanctioned` | `<n>` | `<n>` | `<n>` | `*c_void` in a seam / `ffi_export` / `extern "C"` signature |
| `void_ptr_smell` | `<n>` | `<n>` | `<n>` | `*c_void` elsewhere; `void_ptr_sites` names each one |

### What the review moved

What changed between the two snapshots, and what did not. State the targets
that held at `0` explicitly — an unchanged `0` is a result, not an absence.

## Notes

The only prose outside the setup and legend above: pitfalls, findings, and the
context each table cannot carry. One `###` subsection per finding, titled by
what it is about. Describe the EXPERIMENT and its results — a fix made to
crustify-cli or ffibox along the way belongs in that repo's history, not here.

> Gate misses and anything the oracle and `translate` disagreed on; a wave that
> was superseded and why; what each wave's diff actually contained beyond its
> row counts; where a metric moved and what moved it; what the judge found and
> whether it held. Everything else stays in the tables.
