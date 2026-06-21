# unsafe_metrics (PoC)

A rustc driver (HIR + typeck) that emits per-crate unsafe/raw-pointer metrics
as one JSON line per compiled crate. A precise, resolution-aware alternative to
the regex pass in `audit.py` for the subset of properties below.

## Metrics (JSON fields)

### Unsafe blocks
| field | meaning |
|---|---|
| `unsafe_blocks` | number of `unsafe { ... }` blocks (real code; macro-expanded included, doc-comment examples excluded) |
| `unsafe_block_stmts` | `hir::Stmt` nodes inside unsafe blocks (desugared; tail exprs are not stmts) |
| `unsafe_block_lines` | source lines spanned by unsafe blocks, outermost only (raw `{`..`}` span; incl. blank/comment lines) |
| `unsafe_block_code_lines` | same, but non-blank / non-`//`-comment lines only (apples-to-apples with code LOC) |

### Region attribution of unsafe blocks
| field | meaning |
|---|---|
| `unsafe_blocks_wrapper_impl` | unsafe blocks inside `impl T` / `impl Tr for T` where `T` is a `define_type!` wrapper |
| `wrapper_impl_macro` | ...of which macro-generated (the `define_type!` `get`/`get_mut`/CCell accessors) |
| `wrapper_impl_handwritten` | ...of which hand-written wrapper methods |
| `unsafe_blocks_ffi_export` | unsafe blocks inside a `mod ffi_export` (the sanctioned C-ABI re-export seam) |

### Raw pointers in fn signatures (args / returns), region-classified
| field | meaning |
|---|---|
| `rp_wrap_nonseam_args` / `_rets` | raw-ptr args / returns in wrapper-impl methods that are NOT seam methods |
| `rp_wrap_nonseam_wrapped` | ...of those, pointee is a type that has a `define_type!` wrapper (a safe alternative exists) |
| `rp_outside_args` / `_rets` | raw-ptr args / returns in fns outside wrapper impls AND outside `mod ffi_export` |
| `rp_outside_wrapped` | ...of those, pointee has a `define_type!` wrapper available |
| `mut_borrow_wrapper` | `&mut W` in signatures (incl. `&mut self`) where `W` is a `define_type!` wrapper — a discipline smell (wrappers interior-mutate via `&self`); should be 0 |
| `field_proj_wrapped` | `(*p).field` (incl. `addr_of!((*p).field)`) where `p: *C` and `C` has a `define_type!` wrapper |
| `field_proj_outside_impl` | ...of those, the subset outside any impl/trait body — the smell (a port body bypassing the accessor instead of calling it); inside-impl projections are accessor definitions (sanctioned) |
| `void_ptr_sanctioned` / `void_ptr_smell` | `*const/*mut c_void` in signatures, split: sanctioned (seam method / `mod ffi_export`) vs smell (ordinary signatures, where a typed pointer/wrapper is preferred). Signature-scoped; `as *mut c_void` casts not counted |

### Raw deref + denominators
| field | meaning |
|---|---|
| `raw_ptr_derefs` | `*p` where `p: *const/*mut T`, decided by the **operand's type** (excludes `*Box`/`*&`/`Deref`) |
| `total_stmts` | `hir::Stmt` nodes crate-wide (denominator) |

## Classification model (all resolution-based, not textual)
- **wrapper type `T`** — a struct whose def-span carries a `*::define_type!`
  macro-expansion context (bare `define_type!` or path-qualified
  `crustify::define_type!`). Its `CType<C>` field maps `C` -> "has a wrapper".
- **wrapper impl** — an `impl` whose self-type (HIR path-resolved) is a wrapper `T`.
- **seam method** — fn named `as_ptr`/`as_mut_ptr`/`as_c_ptr`/`as_raw`/`from_ptr`/
  `from_raw`/`to_ptr`/`to_raw`/`into_raw`/`from_foreign`/`into_foreign` (raw ptrs there are the expected boundary).
- **`mod ffi_export`** — any ancestor module named `ffi_export`.
- **macro-generated vs hand-written** — `Span::from_expansion()` on the unsafe block.

## Why a driver, not regex/syn
Three things need HIR/typeck, not text:
- `raw_ptr_derefs` and `rp_*` need the **operand/pointee type** (`*Box` vs `*raw`; which C type a `*mut` points at).
- wrapper detection needs the struct's **macro-expansion context** + the impl's resolved self-type.
- the `_wrapped` subset needs mapping each wrapper's `CType<C>` field back to `C` (a `DefId`, alias-proof).
HIR is also post-expansion, so it counts macro-generated unsafe and excludes
`///` doc-comment examples (on `crustify-crate`: 27 real blocks vs 59 `grep 'unsafe {'`).

## Usage mode (`UM_MODE=usage`)
A separate mode (distinct from the unsafe/audit metrics above) that profiles
crustify-crate primitive usage. Emits a different JSON shape:
```json
{"crate":"libgit2",
 "types":{"CType":118,"CBox":58,"SelfPtr":55,"CVec":38,...},   // struct refs in type positions
 "trait_impls":{"CCell":113,"CValued":27,"CFreed":20,...},      // impl <crustify trait> for T
 "macros":{"define_type":112,"impl_cvalued":23,"impl_freed":18,...}}  // distinct invocations
```
- `types` — references to the smart-pointer/cell **structs** in type positions
  (fn signatures, struct/enum/union fields), counted by resolved `DefId` (crate == `crustify`).
- `trait_impls` — `impl <crustify trait> for T` counts (gated on `DefKind::Impl{of_trait}`).
- `macros` — distinct `ExpnId`s per crustify `*!` macro (items from one invocation share an id).
- Cross-checks: `CType` refs ~= `define_type!` ~= `CCell` impls; trait-impl counts > macro
  counts where lifecycle impls are hand-written rather than macro-generated.
- Not counted: `COut` (a type alias -> typeck-transparent), and expr-level call sites.

```
UM_MODE=usage  RUSTC=.../unsafe_metrics ... cargo +nightly build
```

## Seed mode (`UM_MODE=seed`)
Per-seed audit metrics (like `audit.py`'s seed model), scoped to each seed's
region, plus a `naked` footprint. A **seed** is a **type** (a `define_type!`
wrapper) or a **function**; selectors union:

| env filter | resolves to |
|---|---|
| `UM_SEED_NAME="a b"` | entities whose **Rust name** (`GitOid`) **or C tag** (`git_oid`) is in the list |
| `UM_SEED_FILE="oid.rs"` | entities defined in that file (def-span filename) |
| `UM_SEED_DIR="src/odb"` | entities under that dir |
| `UM_SEED_ALL=1` | every wrapper ∪ every fn |

Emits `{"crate":...,"seeds":[ {per-seed} ]}`. Per seed:
- **region** = a type's `impl T` blocks (self-type == T), or a function's body.
- **own-region** metrics (scoped to the region): `unsafe_blocks`,
  `unsafe_block_code_lines`, `wrapper_macro`/`wrapper_handwritten`,
  `raw_ptr_derefs`, `field_proj` / `field_proj_outside_impl`,
  `mut_borrow_wrapper`, `void_ptr_smell`.
- **`naked`** = uses of the seed's C entity outside the sanctioned homes
  (seam routines, `mod ffi_export`, macro-generated code via `from_expansion`),
  counted **everywhere including the seed's own region**: for a **type**, raw
  `ffi::C` refs in signatures (`DefId`-matched); for a **function**, calls to the
  `*-sys` binding with the C tag. This is the precise version of audit's `naked`/
  `wrapped_bypass` (e.g. `git_oid` → `naked=15`).

```
UM_MODE=seed UM_SEED_NAME="GitOid git_commit_parent_id"  RUSTC=.../unsafe_metrics ... cargo +nightly build -p <crate>
```

## Build
Needs nightly + `rustc-dev` + `llvm-tools` (pinned by `rust-toolchain.toml`):
```
cargo +nightly build
```

## Run on one file
```
./run.sh path/to/file.rs       # prints the JSON line
```
`demo.rs` shows the deref precision (`*Box` excluded, `*raw` counted).

## Run on a whole crate / workspace (RUSTC wrapper)
The driver is rustc-arg-compatible. Under cargo it emits only for workspace
**primary packages** (deps and build scripts are skipped):
```
NS=$(rustc +nightly --print sysroot)
RUSTC=.../unsafe_metrics/target/debug/unsafe_metrics SYSROOT=$NS LD_LIBRARY_PATH=$NS/lib \
  cargo +nightly build        # one JSON line per workspace crate
```
`UM_DEBUG=1` additionally dumps each struct's macro-expansion name (debugging
wrapper detection).

## Example (libgit2 crustify workspace, non-sys crates)
3072 unsafe blocks: 54% in `define_type!` wrapper impls (526 macro + 1145 hand),
18% in `mod ffi_export`, 27% other. Signature raw ptrs: 164 in wrapper non-seam
methods (81% have a wrapper available), 91 outside excl. ffi_export (24% do).
1485 type-confirmed raw-ptr derefs.

## Scope / next steps
Covers the unsafe-block / raw-pointer subset of `audit.py`. The same
`after_analysis` HIR/typeck walk extends to the rest (naked `ffi::T` use by
`DefId`, `&mut self` on wrappers, raw-field projections, the full THIR
`UnsafeOpKind` set). Productionize as a `dylint` lib to run via `cargo dylint`
and emit diagnostics at source spans.
