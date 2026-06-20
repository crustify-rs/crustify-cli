# Wrap stage - free-symbol safe wrappers

You are the wrap-stage codegen agent for a batch of **free symbols** - functions
and globals that are wrap-scope and **not** bound to a single type (so they get
a thin safe Rust view over the FFI surface, not a full port). The deterministic
scheduler chose *which* symbols. Everything else you **discover yourself** with
`crustify query` - there is no pushed payload.

## Your batch: `{syms}`

`{syms}` is a JSON list of `{{name, defined_in}}` - your worklist. (`defined_in`
disambiguates same-named file-local statics.)

## Discover (run per symbol)

`{target}` is the crustify target.

| Command | Gives you |
|---|---|
| `crustify {repo_root} {target} query syms --name <name> --with-details` | the record - signature (`type`, `ptr_args`/`ptr_ret` pointer shapes + nullability), `kind`, `linked_in`, `used_by`. (Drop `--with-details` for the summary.) |
| `crustify {repo_root} {target} scaffold --name <name>` | the `.rs` module homing its `// Replaces: <name>` anchor - prints the path (the authoritative locator; also finds a dep's module). |
| `crustify {repo_root} {target} query dag --name <name> --depth 1` | first-layer deps - already wrapped; call **their** safe wrappers, never raw FFI. |

If `query syms` reports an ambiguity (a same-named static in >1 file), add
`--file <defined_in>` from your worklist entry to pick the right one.

## The anchor contract (load-bearing)

**Locate your files, then fill.** The wrap stage already created the whole
source-file stub tree up-front - you never create files or invent module paths.
Find each symbol's module with `crustify {repo_root} {target} scaffold --name <name>`: it
prints the `.rs` path homing its `// Replaces: <name>` anchor (and likewise
locates a dep's module). A `not created` / `not in scope` reply means out of
scope or a scheduling gap - stop and report; do not hand-create it.

Your `.rs` (from `scaffold --name <name>`) contains, for each symbol, a stable
anchor laid by the scaffolder - a plain `//` line comment (so the unfilled stub
still compiles) followed by its todo:

```
// Replaces: <name> (<file>.c)
// crustify:todo
```

For each symbol you wrap: **promote that line to a `/// Replaces:` doc comment
on the wrapper item you emit**, **delete its `// crustify:todo`**, and write the
safe wrapper in that anchor's region. Leave every other anchor (still a `//`
line comment) and any todo you do not complete **exactly as-is** - the scheduler
treats a surviving `// crustify:todo` as "still pending", so a partial batch is
resumable, and the leftover `//` placeholders keep the file compiling. Do not
touch the `//! crustify:managed` file header.

## Steps - execute in order

### 1. Discover each symbol

For each `{{name, defined_in}}` in `{syms}`: pull its record (`query syms --name <name> --with-details`
- the exact signature: params, return, pointer shapes, const-ness, nullability
from `ptr_args`/`ptr_ret`), its module (`scaffold --name <name>`), and its deps (`query
dag --depth 1`). Read the C source at `defined_in` if the signature needs
confirming.

### 2. Emit the safe wrapper

Under the symbol's anchor, write a `pub fn` (or `pub unsafe fn` only when the
contract genuinely cannot be made safe) that:

- takes/returns **typed wrappers** at every boundary - no raw `*mut ffi::T` in
  the public signature unless allowlisted (DISCIPLINE Sec 9: A1/A2/A3);
- calls the C symbol through `ffi::<name>` (functions) or `ffi::crustify_<NAME>`
  (macro shims) inside an `unsafe` block carrying a specific, falsifiable
  `// SAFETY:` note;
- for a wrap-dep type from `query dag`, takes/returns **its** safe wrapper (the
  FFI borrow ladder, Sec 10) - never re-wrap it, never raw-FFI it;
- for a **global**, emit a safe accessor (`pub fn <name>() -> ...`) over
  `ffi::crustify_get_<NAME>()` rather than exposing a raw `static`.

The C definition stays; this is an **additive** safe view (no `#[no_mangle]`
re-export, no `#ifndef` guard - that is the port stage's job, not wrap's).

### 3. Callbacks (`kind: callback`)

A symbol whose record is `kind: "callback"` is a C **function-pointer typedef**,
not a free function - wrap it as a callable handle (this section).

**Query every variant.** `crustify {repo_root} {target} query syms --name <name> --with-details`
returns the record: the per-argument ownership in `ptr_args` (`const` / `depth`
/ `array` / `string` / `moved` / `mutable`), `ptr_ret`, the `used_by` callsites,
and - when invokers disagree on ownership - a `forks` list, one entry per
distinct `ptr_args`/`ptr_ret` distribution. **Emit one wrapper per variant**: the
primary (the top-level `ptr_args`/`ptr_ret`) plus one for each `forks[]` entry.
All variants share the same C function-pointer type; they differ only in
argument/return ownership. When there is more than one variant, give each
wrapper a distinct name. Honour each variant's own `ptr_args`/`ptr_ret` in that
variant's `call`.

**Wrapper shape (each variant).** Under the symbol's `// Replaces: <name>`
anchor emit a `#[repr(transparent)]` newtype over the nullable FFI
function-pointer typedef:

- `pub struct <Wrapper>(ffi::<name>);` - `ffi::<name>` is the bindgen typedef, a
  nullable `Option<unsafe extern "C" fn(...) -> ...>`; store it directly (a callback
  may be null).
- `#[derive(Copy, Clone)]`; **no `Drop`** (a function pointer owns nothing).
- `pub fn from_raw(p: ffi::<name>) -> Self` and `pub fn to_raw(self) -> ffi::<name>`
  - the FFI-boundary conversions.
- `pub unsafe fn call(self, <safe args>) -> <safe ret>`: the `<safe args>` and
  `<safe ret>` are the Sec 2 **Pointer-shape -> Rust type** forms of this variant's
  `ptr_args` / `ptr_ret`. Convert each safe-wrapper argument to its raw form per
  that variant's ownership, invoke the stored function pointer inside an `unsafe`
  block with a specific, falsifiable `// SAFETY:`, then convert the raw return to
  its safe wrapper per `ptr_ret`.

**Deps are safe wrappers.** Every pointer type in the signature
(`ptr_args[].type`, `ptr_ret.type`) takes/returns **its** safe wrapper, never raw
`ffi::T` - the same rule as Sec 2.

### Pointer-shape -> Rust type

Render **every** `ptr_args[*]` and the `ptr_ret`
from its ownership facets in the record (`moved` / `borrowed` / `array` /
`string` / `mutable` / `const`, plus the signature's nullability). The wrapper
reconstructs each raw pointer at the FFI seam and exposes only these safe forms:

- **Argument (`ptr_args[*]`)** - by how the callee treats the pointee:
  - `moved=true` (callee takes ownership - frees, stores, or hands it off) -> take
    the **owning wrapper by value**: `CArc<T>` if the type has an `up_ref`, else
    `CBox<T>`; the caller relinquishes it. With `array`/`string`, the owning form
    is `CVec<T>` / `CStr` by value. If the reference is type-erased (`void *`) use
    a `COwnable` parametric to allow generic owners (`CArc`/`CBox`), and call `into_foreign` to
    transfer ownership to the C `ffi::`. 
  - `moved=false` (borrowed - used only for the duration of the call) -> take a
    **shared borrow** of the wrapper: `&T` (i.e. `&Wrapper`), or `SelfPtr` (Sec 7.2) /
    the DISCIPLINE Sec 10 ladder when a plain reference can't carry the C aliasing.
    **Pass `&T` even when `mutable=true`** - a wrapped type is `&self`-only; the C
    call mutates it through the raw pointer (interior mutability), so there is
    **no `&mut Wrapper`** (the Sec 8 `DerefMut` ban). `string=true` -> `&CStr` / `&str`.
    If reference is type-erased (`void *`) use parametric `<C: CCell>` over `&C`
    to allow generic types forcing layout-compatibility with `CType`.
  - **`&mut` is reserved for *non-wrapped* pointees** - a scalar out-param, an
    uninitialised slot, or a raw byte/element buffer that is *not* a wrapped type:
    `&mut T` / `&mut MaybeUninit<T>` / `&mut [T]` / `COut<T>` (Sec 9 A2), filled by the
    call. Never `&mut` a wrapped type.
  - `array=true` (over a wrapped element or read-only buffer) -> `&[T]`, or the
    matching `CVec` variant by value if `moved`.
  - non-pointer scalar -> by value; a **nullable** pointer -> wrap the chosen form
    in `Option`.

- **Return (`ptr_ret`)** - the same rule as the type wrapper's returned-reference
  mapping:
  - `moved=true` (the return transfers ownership) -> an **owning wrapper**
    (`CArc<T>` if `up_ref` exists, else `CBox<T>`); if the reference is type-erased
    (`void *`) use `COwnable` to allow generic owners, and call `from_foreign` to acquire
    ownership. 
  - `borrowed=true` -> `SelfPtr<'lifetime, T>` or an `&T` bound to `lifetime`, per
    the Sec 10 ladder / Sec 7.2 when it can't be expressed directly; if ref is
    type-erased (`void *`) use parametric `<C: CCell>` over `&C` to allow borrowed
    views over generic types.
  - `array=true` -> a slice view or the appropriate `CVec` variant (plain /
    zeroing / secure); `string=true` -> the appropriate `CStr` variant;
  - **nullable** -> wrap the above in `Option`; scalar -> return by value.

Never expose raw `*mut`/`*const ffi::T` in the public signature (except a
DISCIPLINE Sec 9 A1/A2/A3 allowlist case), and never **take or return** `&mut` to a
wrapped type - its mutation always goes through `&self` (interior mutability /
setters, Sec 8). `&mut` appears only on non-wrapped pointees (scalars, buffers,
out-param slots).

### 4. Cut cycle edges

**hi-deps / lo-deps** (use both):

- `crustify {repo_root} {target} query dag --name <name> --scc hi-deps` - deps whose
  wrapper does not exist yet: reference those **naked** as `ffi::<dep>` and note
  the gap. Do not invent a wrapper for them, they will be wrapped in a later step.
- `crustify {repo_root} {target} query dag --name <name> --scc lo-deps` - already-wrapped
  entities that reference this symbol/callback **naked** (`ffi::<name>`): open each and
  switch those references to these wrappers, keeping `cargo check` green.

### 5. Re-export imports

Add any `use` your wrappers need at the top of your module (inside the managed
region, after the header). Find the generated `bindings.rs` for the record's
`linked_in` library's `<lib>-sys` crate; derive module paths from it and the wrap
deps' real locations (their `scaffold --name <dep>`). Do not invent module paths.

### 6. Validate

Run `cargo check` and `cargo clippy` over the **whole workspace**
(`--workspace`). Fix errors before finishing.

Run `crustify audit --name {syms}` to get potential sites that are still using
your wrappers' naked `ffi::` calls, whether your own wrappers still use raw pointer
args or return, which may be signals that they need to use the wrappers you wrote,
and your wrapper should use the `define_type!` wrapped types and the
crustify-crate smart pointers / traits. Fix them, unless justified.