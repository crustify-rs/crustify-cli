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
| `unsafe_block_lines` | source lines spanned by unsafe blocks, outermost only (raw `{`..`}` span; incl. blank/comment lines). **Hand-written only** — a macro-generated block's span lives in the macro's defining crate, so charging its lines here would bill ffibox's source to this crate; the blocks themselves still count in `unsafe_blocks` / `wrapper_impl_macro`, and the invocation's own lines are in `code_lines` |
| `unsafe_block_code_lines` | same, but non-blank / non-`//`-comment lines only (apples-to-apples with `code_lines`) |

### Region attribution of unsafe blocks
| field | meaning |
|---|---|
| `unsafe_blocks_wrapper_impl` | unsafe blocks inside `impl T` / `impl Tr for T` where `T` is a `define_*ctype!` wrapper |
| `wrapper_impl_macro` | ...of which macro-generated (the `define_*ctype!` seam accessors) |
| `wrapper_impl_handwritten` | ...of which hand-written wrapper methods |
| `unsafe_blocks_ffi_export` | unsafe blocks at the C-ABI boundary — a fn carrying a C symbol name (`#[unsafe(no_mangle)]` / `#[export_name]`) **or** having a non-Rust ABI (`extern "C"`, the callback-shim form that needs no symbol name). One predicate, `in_ffi_export`, decides this and the pointer sanctioning below |

### Raw pointers in fn signatures (args / returns), region-classified
| field | meaning |
|---|---|
| `raw_ptr_args` / `raw_ptr_rets` | **every** raw-ptr position in a signature (args / returns) — the denominator |
| `raw_ptr_seam` | ...the subset sanctioned by construction: seam fn, C-exported fn (`#[no_mangle]` / `#[export_name]`), `extern "C"`, or a ptr to the method's own `Self` (`free`/`dup` — you cannot pass `&Self` while destroying it). The smell is `raw_ptr_args + raw_ptr_rets - raw_ptr_seam` |
| `raw_ptr_wrapped` | ...of the NON-seam remainder, pointee is a C type that HAS a wrapper (`*mut ffi::git_oid` where `GitOid` exists) — the actionable defect. A raw ptr to the *wrapper* is not counted: it already uses the wrapper |
| `raw_ptr_in_wrapper` | ...of the NON-seam remainder, inside `impl <wrapper T>` — a raw ptr in the very type meant to hide it |
| `field_ref_wrapped` | `&(*p).field` / `&mut (*p).field` where `p: *C` and `C` has a wrapper — a reference over a FIELD of memory C may write, the same rule that keeps `&W` out applied one level down. `addr_of!` / `&raw` lower to `BorrowKind::Raw` and are NOT counted, so this is exactly the sanctioned/forbidden split; **should be 0** |
| `ref_to_type_wrapper` | `&W` or `&mut W` in signatures (incl. the `&self` / `&mut self` receiver) where `W` is a TYPE wrapper — implements `CCell` AND stores the C object inline (`CType<ffi::T>`), rather than holding a pointer to it. A reference of either kind over the object's own bytes asserts something about memory C may write through a pointer it retains, so access goes through the borrowed handles instead; **should be 0**. A reference to a POINTER wrapper (a handle, `#[repr(transparent)]` over `CPtr`) covers Rust-owned storage and is NOT counted |
| `field_proj_wrapped` | `(*p).field` (incl. `addr_of!((*p).field)`) where `p: *C` and `C` has a `define_*ctype!` wrapper |
| `field_proj_outside_impl` | ...of those, the subset outside any impl/trait body — the smell (a port body bypassing the accessor instead of calling it); inside-impl projections are accessor definitions (sanctioned) |
| `raw_ptr_smell_macro` / `raw_ptr_smell_handwritten` | ...the NON-seam remainder split by ORIGIN: emitted by a macro expansion (`def_span().from_expansion()`, i.e. `define_ctype!` in ffibox) vs written in this crate. Same split as `wrapper_impl_macro` / `wrapper_impl_handwritten`, and for the same reason — the position is real, public, callable API of this crate, so it is not dropped the way a macro block's LINES are; but it is boilerplate replicated once per wrapped type, which no agent wrote and none can fix. **`raw_ptr_smell_handwritten` is the campaign-attributable number** |
| `void_ptr_smell_macro` / `void_ptr_smell_handwritten` | the same origin split over `void_ptr_smell` |
| `void_ptr_sanctioned` / `void_ptr_smell` | `*const/*mut c_void` in signatures, split: sanctioned (seam method / C-ABI boundary) vs smell (ordinary signatures, where a typed pointer/wrapper is preferred). Signature-scoped; `as *mut c_void` casts not counted |

### Raw deref + denominators
| field | meaning |
|---|---|
| `raw_ptr_derefs` | `*p` where `p: *const/*mut T`, decided by the **operand's type** (excludes `*Box`/`*&`/`Deref`) |
| `raw_ptr_derefs_outside_impl` | ...of those, the subset **not** inside any `impl`/trait body — i.e. port-body raw access, vs. the sanctioned centralisation in wrapper accessor/seam method bodies (same split as `field_proj_outside_impl`) |
| `total_stmts` | `hir::Stmt` nodes crate-wide (denominator) |
| `code_lines` | crate-wide code LoC (denominator): non-blank, non-`//`-comment lines, counted over the **union of HIR definition spans** rather than the raw file text. A `cfg`-stripped item has no `DefId`, so an inline `#[cfg(test)] mod tests` — never compiled under `cargo build`, so unable to reach any numerator — no longer inflates it (same for any `#[cfg(..)]`-disabled item). Macro-expanded defs map through `source_callsite()` to their invocation line; containers (`mod`/`impl`/`trait`/`extern` blocks) contribute only their opening and closing lines, since their full span still covers text `cfg` removed from inside them |

## Classification model (all resolution-based, not textual)
- **wrapper type `T`** — a struct implementing the seam trait `CCell`, i.e.
  carrying the `type C` associated item. `C` then maps to "has a wrapper".
  Keying on the trait rather than the macro covers the generic and
  lifetime-carrying wrappers the macro cannot express. The borrowed handles
  (`TRef<'a>` / `TMut<'a>`) are NOT wrappers: they hold the pointer by value, so
  a reference to one covers Rust-owned storage and is not counted.
- **wrapper impl** — an `impl` whose self-type (HIR path-resolved) is a wrapper `T`.
- **seam method** — fn named `as_ptr`/`as_mut_ptr`/`as_c_ptr`/`as_raw`/`from_ptr`/
  `from_raw`/`to_ptr`/`to_raw`/`into_raw` (raw ptrs there are the expected boundary).
- **C-ABI boundary** (`in_ffi_export`) — the fn, or an enclosing one, carries
  a C symbol name (`#[unsafe(no_mangle)]` / `#[export_name]`, read off
  `codegen_fn_attrs`) or has a non-Rust ABI (`extern "C"`). Replaces an
  earlier `mod ffi_export` region check — a module convention neither ported
  tree uses, so that branch was unreachable and every void pointer fell
  through to the smell bucket.
- **macro-generated vs hand-written** — `Span::from_expansion()` on the unsafe block.

## Why a driver, not regex/syn
Three things need HIR/typeck, not text:
- `raw_ptr_derefs` and `raw_ptr_*` need the **operand/pointee type** (`*Box` vs `*raw`; which C type a `*mut` points at).
- wrapper detection needs the struct's **macro-expansion context** + the impl's resolved self-type.
- the `_wrapped` subset needs mapping each wrapper's `CType<C>` field back to `C` (a `DefId`, alias-proof).
HIR is also post-expansion, so it counts macro-generated unsafe and excludes
`///` doc-comment examples (on `ffibox`: 27 real blocks vs 59 `grep 'unsafe {'`).

## Usage mode (`UM_MODE=usage`)
A separate mode (distinct from the unsafe/audit metrics above) that profiles
ffibox primitive usage. Emits a different JSON shape:
```json
{"crate":"libgit2",
 "types":{"CType":118,"CBox":58,"SelfPtr":55,"CVec":38,...},   // struct refs in type positions
 "trait_impls":{"CCell":113,"CValued":27,"CDropped":20,...},      // impl <crustify trait> for T
 "macros":{"define_ctype":112,"impl_cvalued":23,"impl_dropped":18,...},  // distinct invocations
 "ffi_calls":{"libgit2_sys::git__free":48,"libc::close":6,...},   // per-crate::symbol counts
 "ffi_call_sites":{"libgit2_sys::git__free":{"free_fn":[{"file":"...","count":2,"lines":[804,805]}],
                                             "trait_impl:CLenDropped":[...]}, ...}}
```
- `types` — references to the smart-pointer/cell **structs** in type positions
  (fn signatures, struct/enum/union fields), counted by resolved `DefId` (crate == `crustify`).
- `trait_impls` — `impl <crustify trait> for T` counts (gated on `DefKind::Impl{of_trait}`).
- `macros` — distinct `ExpnId`s per crustify `*!` macro (items from one invocation share an id).
- `ffi_calls` — crate-wide tally of every **call to a foreign fn**
  (`tcx.is_foreign_item`, i.e. declared in an `extern` block), keyed `crate::symbol`. This is
  the FFI boundary itself, **crate-agnostic**: bindgen `*-sys` bindings, `libc`, and local
  `extern "C"` blocks all resolve to it (a name heuristic like `*-sys` would miss `libc` and
  local externs, and over-count Rust helpers living in a sys crate). Calling a foreign fn is
  unsafe, so this is the **unsafe-FFI-call surface** (allocators/frees included).
  Resolution-based (callee `DefId`), so alias-/re-export-proof and multi-line-safe. Free-fn
  path calls (`ExprKind::Call` with a `Path` callee).
- `ffi_call_sites` — the same calls grouped `{crate::symbol: {region: [{file,count,lines}]}}`,
  where `region` is the **enclosing body's** kind: `free_fn`, `inherent_impl`, or
  `trait_impl:<Trait>`. This separates a sanctioned wrapper chokepoint (a `git__free` inside
  `trait_impl:CDropped` / `trait_impl:CLenDropped`) from port-body smell (`free_fn` /
  `inherent_impl`), making the actionable subset a filter rather than a judgement. Pair with
  the symbol to triage manual `git__malloc` / `git__free` / `git__*array` uses vs. their safe
  `CVec` / `CBox` / `CVoidBox` wrappers.
- Cross-checks: cell refs ~= `define_*ctype!` ~= seam-trait impls; trait-impl counts > macro
  counts where lifecycle impls are hand-written rather than macro-generated.
- Not counted: `COut` (a type alias -> typeck-transparent).

```
UM_MODE=usage  RUSTC=.../unsafe_metrics ... cargo +nightly build
```

## Seed mode (`UM_MODE=seed`)
Per-seed audit metrics (like `audit.py`'s seed model), scoped to each seed's
region, plus a `naked` footprint. A **seed** is a **type** (a `define_*ctype!`
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
  `ref_to_type_wrapper`, `void_ptr_smell`.
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
3072 unsafe blocks: 54% in `define_*ctype!` wrapper impls (526 macro + 1145 hand),
18% in `mod ffi_export`, 27% other. Signature raw ptrs: 164 in wrapper non-seam
methods (81% have a wrapper available), 91 outside excl. ffi_export (24% do).
1485 type-confirmed raw-ptr derefs.

## Scope / next steps
Covers the unsafe-block / raw-pointer subset of `audit.py`. The same
`after_analysis` HIR/typeck walk extends to the rest (naked `ffi::T` use by
`DefId`, references to wrappers, raw-field projections, the full THIR
`UnsafeOpKind` set). Productionize as a `dylint` lib to run via `cargo dylint`
and emit diagnostics at source spans.
