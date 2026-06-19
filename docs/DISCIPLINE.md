# Porting rules — hard discipline for the C→Rust port

Rules that apply across every wrapper, accessor, and FFI export.
Unlike [HELP.md](./HELP.md), which collects pattern decisions ("what
to reach for in case X"), this document collects rules that must
hold *across* patterns. Violations are bugs.

> **Project-agnostic note.** These rules are project-agnostic; the
> concrete identifiers used as examples below (`SSL_*` / `BIO` /
> `libssl-*` from OpenSSL, `git_*` from libgit2, `Curl_*` from curl)
> are illustrative, not normative. Two conventions are parameterised on
> the project being ported:
> - **C-side feature-flag guard:** `CRUSTIFY_<FILE>`, where `<FILE>` is
>   the symbol's `defined_in` path, path-sanitised and upper-cased
>   (e.g. `lib/vssh/libssh2.c` → `CRUSTIFY_LIB_VSSH_LIBSSH2`). One macro
>   per ported C file; defining it switches that file's ported bodies to
>   their Rust replacements. §1 uses `CRUSTIFY_<FILE>` throughout.
> - **Re-export symbol** (for TU-local symbols a remaining C caller still
>   names): `crustify_<file>__<name>`, the lowercase counterpart of the
>   same path-sanitised token (e.g.
>   `crustify_lib_vssh_libssh2__parse_header`). See §1.4.
> - **Crate / file placement:** the canonical layout is the one the
>   `scaffold` stage emits — one port crate per **linked library**
>   (`rust/<linked_in>`) plus a `<lib>-sys` FFI crate per linked library —
>   with the wrapper file at the symbol's **full vanilla `defined_in` path**
>   under the crate's `src/`. The `#[no_mangle]` re-exports live in each
>   file's `mod ffi_export` submodule (no central `ffi-exports` crate). See §3.

[`HAZARDS.md`](./HAZARDS.md) catalogues the concrete UB / soundness-
gap scenarios that these rules exist to prevent — useful when
investigating a specific failure mode or onboarding to the
soundness model.

The sections below follow the natural decision sequence a porter
goes through: is this in scope → where does it live → what's it
called → what shape → how to expose fields → how to wrap methods
→ what's the underlying access discipline.

---

## 1. C-side guard discipline — `CRUSTIFY_<FILE>`

When a C function is ported, its C body is excluded from the build by
placing it inside `#ifndef CRUSTIFY_<FILE>`.  These rules
govern how those guards are structured.

### 1.1 Guard only what is ported

`#ifndef CRUSTIFY_<FILE>` covers **only** the C functions that
have a Rust replacement.  Unported functions must **not** be inside the
guard — they compile in both the C-only and Rust builds.

Violation: wrapping an entire source file in a single
`#ifndef CRUSTIFY_<FILE>` / `#endif` block.  This accidentally
excludes every unported function in the file from the Rust build,
causing linker failures.

### 1.2 Use tight blocks, not file-level wrapping

Break the guard into **tight blocks** around adjacent groups of ported
functions.  Unported functions sit between those blocks at their natural
file position, outside any guard:

```c
#ifndef CRUSTIFY_<FILE>
/* ... ported functions A, B, C ... */
#endif

/* unported function D — compiled in both builds, no guard */
static int D(SSL_CONNECTION *s, unsigned int context) { ... }

#ifndef CRUSTIFY_<FILE>
/* ... ported functions E, F ... */
#endif
```

Adjacent ported functions may share a single block; per-function
granularity is not required.

### 1.3 Unported statics get an unconditional public wrapper

An unported static has no header declaration and is not visible outside
its translation unit.  To make it callable from Rust, add a public
`ssl_extensions_*` wrapper that calls it.  Both the static and the
wrapper are **outside** any `CRUSTIFY_<FILE>` guard — compiled
unconditionally:

```c
/* unported static — no guard */
static int foo(SSL_CONNECTION *s, unsigned int context) { ... }

/* public wrapper — no guard; declared in wrapper_statics.h so
   bindgen includes it in the generated FFI bindings */
int ssl_extensions_foo(SSL_CONNECTION *s, unsigned int context)
    { return foo(s, context); }
```

Rust code calls `ssl_extensions_foo`; the static `foo` compiles in
both builds and is always available for the wrapper to call.

### 1.4 Re-exporting a ported function / global

A ported symbol's C body lives inside `#ifndef CRUSTIFY_<FILE>` and is not
compiled when Rust is active.  **Always re-export** the ported symbol so
remaining C can link against the Rust replacement — do **not** gate on
whether a C caller still exists.  Always-on is uniform, fully
deterministic (no caller-set analysis, no re-evaluation as porting
progresses), a dead export is harmless, and it gives a greppable inventory
of every re-export.

**Where it lives, and its shape — the per-file `mod ffi_export` gateway.** Every
`#[unsafe(no_mangle)]` re-export — bare-named or unique-named — lives in a
**`mod ffi_export { use super::*; … }`** submodule **in the ported symbol's own
`.rs` file**, right beside the idiomatic functions it exports (create it once
per file). There is **no central `ffi-exports` crate**: the re-exports compile
into the file's own library port crate (the `<linked_in>` staticlib). The
`mod ffi_export` is the **sole** place a raw C-ABI signature
(`*mut` / `*const ffi::T`, out-pointers, lengths) appears. It is a **thin
boundary**: it reconstructs the safe wrappers from the raw C parameters
(`T::from_ptr`, a `&mut *p` borrow, `COut<T>` for an out-param,
`slice::from_raw_parts` for a ptr+len) and **delegates to the idiomatic
`super::` sibling `fn`** — the ported function in the same file, which
takes/returns those wrappers and has **no `extern "C"`, no `#[no_mangle]`, no raw
signature**. The re-export keeps the **same name** as the C symbol; because
`ffi_export` is a distinct module, it does not clash with the idiomatic `fn` of
the same name in the parent. Never put a raw `extern "C"` signature, nor a
`*_from_raw` forwarding shim, outside `mod ffi_export` — the only raw items
permitted in the parent module are the §9 allowlist cases.

Two C-side sentinels, both `crustify`-prefixed (greppable):

1. the **feature-flag guard** `CRUSTIFY_<FILE>` (the `#ifndef`); and
2. for TU-local symbols, the **re-export redirect** `#define <name>
   crustify_<file>__<name>` (see below).

**The collision trap (TU-local symbols).**  A file-local `static`
(`function_static`, `global_static`) and a TU-local inline
(`function_inline_tu`) have *no external symbol*, and the name may be
reused by an unrelated `static` of the same name in another translation
unit — e.g. libgit2 has two different `parse_header` statics
(`indexer.c`: `int parse_header(git_pack_header *, git_pack_file *)` and
`odb_loose.c`: `int parse_header(obj_hdr *, size_t *, const unsigned char *, size_t)`),
and 56 such colliding static/inline names overall.  A bare
`#[unsafe(no_mangle)] fn parse_header` export would clash at link time.
**Never export a TU-local symbol under its bare name.**

**Mechanism for TU-local symbols — unique export + `#define` redirect.**
Export the Rust function/global under the unique symbol
`crustify_<file>__<name>` (the lowercase path-sanitised `<FILE>` token),
and in the C `#else` branch declare it `extern` and add a `#define`
redirecting the original name to it.  The redirect keeps the in-TU call
sites verbatim and is the greppable re-export sentinel:

```c
#ifndef CRUSTIFY_LIB_VSSH_LIBSSH2          /* function example */
static int parse_header(obj_hdr *h, size_t *n, const unsigned char *d, size_t l)
{ /* C body */ }
#else
extern int crustify_lib_vssh_libssh2__parse_header(obj_hdr *, size_t *, const unsigned char *, size_t);
#define parse_header crustify_lib_vssh_libssh2__parse_header   /* crustify: re-export */
#endif
```

```c
#ifndef CRUSTIFY_LIB_VSSH_LIBSSH2          /* file-local global example */
static int g_state = 0;
#else
extern int crustify_lib_vssh_libssh2__g_state;
#define g_state crustify_lib_vssh_libssh2__g_state            /* crustify: re-export */
#endif
```

Rust side — the **idiomatic** ported fn and its matching **thin `#[no_mangle]`
gateway** live in the **same file**; the gateway sits in that file's
`mod ffi_export`, reconstructs the wrappers, and delegates to its `super::`
sibling:

```rust
// src/odb_loose.rs  — the ported function: idiomatic, NO extern "C", NO raw params:
pub(crate) fn parse_header(h: &mut ObjHdr, out_n: COut<usize>, data: &[u8]) -> c_int { /* … */ }

mod ffi_export {
    use super::*;
    // the raw C-ABI gateway: reconstruct the wrappers, then delegate:
    #[unsafe(no_mangle)]
    pub unsafe extern "C" fn crustify_lib_vssh_libssh2__parse_header(
        h: *mut ffi::obj_hdr, n: *mut usize, d: *const c_uchar, l: usize,
    ) -> c_int {
        // SAFETY: C-ABI boundary; the raw params satisfy the C contract. Reconstruct
        // the safe wrappers (ObjHdr / COut / slice) and delegate — no logic here.
        super::parse_header(/* &mut *h */, /* COut(n) */, /* slice::from_raw_parts(d, l) */)
    }
    // global: the export stays `#[no_mangle] pub static [mut] …` here in mod ffi_export
    // (its representation still follows the shared-mutable-global note below).
}
```

The `#define` macro-shadows the original identifier for the rest of the
TU; that is exactly the intent for a function/global name (it redirects
the callers).  It is a problem only if the same token also names an
unrelated field/local in that TU — rare; fall back to a file-local
`static` forwarder there.

**A shared *mutable* global** that crosses the C/Rust seam is the most
dangerous re-export (no borrow checking, aliasing, no enforced sync): the
`#[no_mangle] static [mut]` is only the ABI-shared *view*; its
representation still follows the `[var]` rules and the
"lifecycle/sync primitives stay in C" discipline.  The re-export
*mechanism* is symmetric to a function, but the *soundness* is not — treat
with the same care as any shared `static mut`.

**Linkage table** (`subkind` from the symbol's manifest):

| subkind | guard | `#else extern` | `#define` redirect |
|---|---|---|---|
| `function_exported`, `global_extern` | yes | bare name | no |
| `function_inline_header` | yes (in header) | bare name, in header | no |
| `function_static`, `function_inline_tu`, `global_static` | yes | unique `crustify_<file>__<name>` | yes |

`function_exported` / `global_extern` get the `#else extern` too: a public
header declaration usually suffices, but the `extern` is required for a
**same-TU caller when no header declaration is visible** after the body is
guarded out, and is harmless when one is.  `macro_*` symbols are
**mirrored, not switched** — no guard and no re-export (kept in C *and*
Rust, §"Macros are mirrored" in the port prompt); `external` / `builtin`
symbols are out of scope.

---

## 2. Scope filter — what gets a wrapper

Two scopes, not one:

- **Port scope** — subsystems whose C implementation is rewritten in
  Rust. Defined by the target's `config.json` (`target` / `port_files`).
- **Wrapper scope** — the reference-closure of port scope: every
  type or function that port-scope code touches by parameter,
  return, or field type. Strictly broader than port scope.

Three bands fall out:

- **Port + wrap** — in port scope. Implementation rewritten; wrappers
  exist; the Category A → B → C migration applies.
- **Wrap-only** — in wrapper scope but not port scope. Gets the
  wrapper surface in-scope Rust needs; the implementation stays C,
  called via FFI. libcrypto exports, QUIC types libssl uses,
  `build.json`-disabled-feature types still referenced by enabled
  code, and types owned by not-yet-ported sibling subsystems all
  land here.
- **Out of scope** — outside wrapper scope. Nothing in port scope
  references it. Nothing to do.

**Being outside port scope does not put you outside wrapper scope.**
"We don't port it" and "we don't wrap it" are independent decisions.
A wrap-only type still gets a wrapper.

A **type's wrapper** exists if the type is in wrapper scope. A
**method** gets a wrapper if it's called by ≥1 port-scope subsystem
or is itself re-exported across the C-ABI boundary (its file's
`mod ffi_export`, §1.4). A **field** gets an accessor if port-scope
code reads or writes it.

A wrap-only type that *lacks* a wrapper is an **incomplete port**,
not a closed decision — work owed, identical in status to an
unported function. There is no "deferred" status for wrapper-scope
types: an agent that encounters one writes the wrapper.

**Speculative wrappers are forbidden.** If you can't name a caller,
don't write the wrapper. This is the discipline that prevents the
ballooning that derailed earlier port attempts.

### 2.1 Wrap-scope functions get safe wrappers in the library port crate

The scope-filter rule above applies to **types**.  The same discipline
extends to **functions**: every C function from a wrap-scope subsystem
that is called by port-scope code gets a safe Rust wrapper. The wrapper
lands at its C file's full vanilla path inside the **library port crate**
of the function's `linked_in` (§3) — the same crate-placement rule as a
port-scope function; wrap vs port is a *body* distinction (forward to
`ffi::` vs idiomatic Rust), not a *crate* distinction.

| Subsystem | C files | Wrapper location |
|---|---|---|
| ECH implementation | `ssl/ech/*.c` (in `libssl`) | `rust/libssl/src/ssl/ech/*.rs` |
| HPKE util | `crypto/hpke/hpke_util.c` (in `libcrypto`) | `rust/libcrypto/src/crypto/hpke/hpke_util.rs` |

**Why a safe wrapper, not inline `ffi::` calls.**  Port-scope code must
not call `ffi::ossl_ech_*` directly — it calls the safe wrapper at the
wrap-scope function's mirrored path. Routing through the wrapper:

- keeps the SAFETY surface in one place — audited once, not repeated at
  every call site;
- gives the future port pass a clear replacement target: when the ECH
  review pass lands, each wrapper body becomes a pure-Rust function with
  the same public signature and the call sites change nothing;
- keeps every raw `ffi::` call behind a single named seam, so the
  `-sys` dependency surface is greppable rather than scattered.

**Wrapper safety level.**  The safe-vs-unsafe split follows the
parameter types:

| Parameter kind | Wrapper signature |
|---|---|
| Bounded-lifetime only — `&[u8]`, `&mut [u8]`, scalars | `pub fn` (safe) |
| Raw pointer to a type that already has a typed wrapper | Use the typed wrapper; no raw pointer in the public API |
| Raw pointer to a type whose wrapper is owed work | `pub unsafe fn`; `# Safety` doc required; owed wrapper marked `// TODO(wrap-budget):` at the site |

A wrap-scope crate that exposes **only** `pub unsafe fn` items is
incomplete work, not a closed decision — the `unsafe` boundary shrinks
as owed type wrappers are written.  Mark each open item with a
`// TODO(wrap-budget):` comment at the site.

---

## 3. Crate organisation

The Rust workspace lives under `<repo_root>/crustify/rust/` (repo-root-shared
across targets, inside the single visible `crustify/` artifact dir),
scaffolded deterministically by the `scaffold` stage. There is **one port crate
per linked library** — the `linked_in` field — because the crate boundary *is*
the library: the ABI/linking unit the C build statically links against. libgit2
(one wrapped library) → one `libgit2` crate; openssl (two in-tree libraries) →
a `libssl` crate and a `libcrypto` crate. The crate a wrapper lives in is
*computed* from the C symbol/type's `linked_in` (filled by the type/symbol
analyzers, which run before scaffold) — it is not a judgement call.

**The module tree mirrors the full vanilla C path** under the crate's `src/`,
so a library's headers (`include/**`, the type wrappers) and its sources
(`src/**`, the op wrappers) **co-locate in the one library crate**:

| C source | Rust location (crate = `linked_in`) |
|---|---|
| `include/git2/oid.h` (in `libgit2`) | `rust/libgit2/src/include/git2/oid.rs` |
| `src/libgit2/oid.c` (in `libgit2`) | `rust/libgit2/src/src/libgit2/oid.rs` |
| `ssl/statem/extensions.c` (in `libssl`) | `rust/libssl/src/ssl/statem/extensions.rs` |
| `crypto/hpke/hpke_util.c` (in `libcrypto`) | `rust/libcrypto/src/crypto/hpke/hpke_util.rs` |

Crates and module files are **materialized on demand** — `scaffold --file` /
`--dir` / `--all` creates only the crates/modules for the files in scope — so a
crate exists only once a file linked into that library has been scaffolded; an
absent crate is not a gap, just unscaffolded. Only **in-tree** libraries (those
with `source_dirs` in `build.json`) get a port crate; system libraries
(libc, libpthread, …) are bound via their `-sys` crate, never ported.

| Role | Crate |
|---|---|
| Port + wrap of a linked library's symbols/types | `rust/<linked_in>` (e.g. `rust/libgit2`, `rust/libssl`) |
| Bindgen output + FFI imports, per linked library | `rust/libgit2-sys`, `rust/libssl-sys`, … (owned by the `bindgen` stage) |

The smart-pointer framework crate `crustify` is a shared dependency of every
port crate. There is **no central `ffi-exports` crate** — each ported file's
`#[unsafe(no_mangle)]` re-exports live in its own `mod ffi_export` submodule
(§1.4), compiled into that file's library port crate. The crate's `staticlib`
archive (`lib<linked_in>.a`) is what the C build links, gated per file by the
`CRUSTIFY_<FILE>` feature flag.

**Why crate-per-library (= per `linked_in`):**

- **The crate boundary is the linking unit.** A C library is one ABI/archive;
  the Rust port crate that replaces its symbols is the matching archive the C
  build links — one staticlib per library, no impedance mismatch.
- **Type/op co-location dissolves the cross-crate wrinkle.** A type
  (`include/git2/oid.h`) and its ops (`src/libgit2/oid.c`) link into the *same*
  library, so they land in the *same* crate. A type's idiomatic methods and its
  free-function ops can therefore be inherent `impl`s / `pub(crate)` siblings —
  no method must be hoisted to a public API just to be reachable from another
  crate. Cross-library type↔op dependencies are vanishingly rare (in openssl,
  1 of 786 ops referenced a type from the other library).
- **Cohesion boundary mirrors C exactly.** Each crate's `src/` tree is a 1:1
  image of the full vanilla source paths of the files linked into that library;
  the wrapper file is derived deterministically from the C file's path (see
  [§"Where the wrapper lives"](#74-where-the-wrapper-lives)).
- **Tangible scope filter.** "Is this in scope?" reduces to "does a port-scope
  file reach it?" — recorded in the analysis tree, not inferred from crate
  membership.
- **Module registration is generated.** Each `lib.rs` / `mod.rs` carries only a
  managed `// crustify:modules:start/end` block listing child modules; the
  scaffolder reconciles it additively and never touches hand-written bodies.

**Dependency direction:**

- Every `rust/<linked_in>` depends on the `-sys` crate(s) it binds and on
  `crustify`.
- Port crates may depend on each other only when a real type/function
  dependency crosses a **library** boundary (e.g. a `libssl` symbol that uses a
  `libcrypto` type → `rust/libssl` depends on `rust/libcrypto`). The crate graph
  must stay a DAG.

**Picking the crate + file for a type or function:** it is **deterministic** —
crate = the symbol/type's `linked_in`; the wrapper lands at the full vanilla C
path under that crate's `src/`, with the C extension swapped for `.rs`. A type
anchors in the `.rs` of the source file it lives in (file-grained), so multiple
types share a file — there is no separate per-type file.

---

## 4. Naming conventions

| Concept | Convention |
|---|---|
| C struct `ssl_session_st` | Wrapper type `SslSession` (no suffix) |
| C function `SSL_SESSION_get_id` | Rust method `id` on `SslSession` |
| C function `SSL_SESSION_set_id` | Rust method `set_id` on `SslSession` |
| Return value of a getter | `&[u8]`, `&str`, or owned wrapper — **never raw pointers** |
| Lifecycle `SSL_SESSION_up_ref` / `SSL_SESSION_free` | `CRefCounted` impl |
| Lifecycle `EVP_MD_CTX_new` / `EVP_MD_CTX_free` (no refcount) | `CFreed` impl |
| In-scope subsystem types | Anchored in the `.rs` of their source file (file-grained: its `defined_in`, or the declaring header for a wrap type) — multiple types share a file; no separate per-type file |
| Method/function wrappers (one C source file) | One Rust file at the C file's **full vanilla path** with `.c`/`.h` swapped for `.rs`, under the library port crate's `src/`: `rust/<linked_in>/src/<full-c-path>.rs`. No prefix-dropping — the path mirrors C exactly. Examples (in `libssl`/`libcrypto`): `ssl/statem/extensions_clnt.c` → `rust/libssl/src/ssl/statem/extensions_clnt.rs`; `crypto/hpke/hpke_util.c` → `rust/libcrypto/src/crypto/hpke/hpke_util.rs`. |
| Doc comment on each wrapper method | `Replaces: <C_FUNCTION_NAME> (<source_file>.c)` line |

**Types are not given their own file.** A type anchors in the `.rs`
that mirrors its source file — its `defined_in`, or the declaring
import header for a wrap type — alongside that file's other types and
the symbols that live there, so multiple types share a file. The `.rs`
path mirrors the C source path exactly (same rule as the
method/function-wrapper row above).

When in doubt about a Rust-method name, match `rust-openssl`'s naming
where it exists — staying aligned with the wider Rust-OpenSSL
ecosystem is a small but real win.

---

## 5. Per-type wrapper template

Every wrapped type follows the same shape. The canonical pattern:

```rust
//! Safe wrapper for `BIO` (`bio_st`) — refcounted (1.1.0+).
//!
//! Lifecycle: `BIO_new` / `BIO_up_ref` / `BIO_free`.
//! See `docs/type-analysis/bio_st.md` for the field/method analysis
//! that informs the in-scope accessors below.

use crustify::{CArc, CRefCounted, CUniqueArc};
use libssl_sys as ffi;

// -- type definition --
crustify::define_type!(Bio, ffi::bio_st);

// -- lifecycle --
// SAFETY: BIO_up_ref / BIO_free are the documented refcount primitives.
unsafe impl CRefCounted for Bio {
    fn up_ref(&self) {
        unsafe { ffi::BIO_up_ref(self.as_ptr()) };
    }
    unsafe fn down_ref(obj: core::ptr::NonNull<Self>) {
        unsafe { ffi::BIO_free(obj.as_ptr() as *mut _) };
    }
}

// -- thread-safety markers --
// SAFETY: BIO is Send. Sync only if all field access paths are atomic
// or guarded; assess per type. Default: Send-only.
unsafe impl Send for Bio {}

// -- constructors (if any are in-scope) --
impl Bio {
    /// Replaces: `BIO_new` (bio_lib.c)
    pub fn new(method: &BioMethod) -> Option<CUniqueArc<Self>> {
        let p = unsafe { ffi::BIO_new(method.as_ptr()) };
        unsafe { CUniqueArc::from_raw(p as *mut Self) }
    }
}

// -- field accessors (only those in-scope subsystems call) --
impl Bio {
    /// Returns the BIO type identifier.
    ///
    /// Replaces: direct field access `bio->method->type` (no public
    /// accessor in pre-1.1.0; thin wrapper in 1.1.0+).
    pub fn type_id(&self) -> c_int {
        // SAFETY: method pointer is stable for the BIO's lifetime;
        // type field is immutable.
        unsafe { (*(*self.as_ptr()).method).type_ }
    }
}

// -- method wrappers (only those in-scope subsystems call) --
impl Bio {
    /// Read up to `buf.len()` bytes. Returns bytes read on success.
    ///
    /// Replaces: `BIO_read` (bio_lib.c)
    pub fn read(&self, buf: &mut [u8]) -> c_int {
        unsafe {
            ffi::BIO_read(self.as_ptr(), buf.as_mut_ptr() as *mut _, buf.len() as c_int)
        }
    }
}
```

Mandatory elements:

1. **Module-header doc comment** stating: type name, refcounted or
   single-owned, C lifecycle functions, link to the type-analysis
   document.
2. **`define_type!` invocation** — one line.
3. **Lifecycle impl** — register the type's teardown via the matching
   macro (one block, with the C lifecycle calls; SAFETY comment cites the
   documented primitives):
   - refcounted (`up_ref`) → `impl_ref_counted!` → `CArc<T>`;
   - heap-allocated header freed by a destructor → `impl_freed!` → `CBox<T>`;
   - **by-value header that owns a resource disposed *without* freeing the
     header** (a `*_dispose`/`*_cleanup`) → `impl_cvalued!` → `CVal<T>`.

   `CFreed` and `CValued` are **not exclusive** — a C type exposing both a
   `*_free` (storage + fields) and a `*_dispose`/`*_cleanup` (fields only)
   registers both; the wrapper (`CBox<T>` vs `CVal<T>`) selects which runs.
   Never register the *same* function under both. **Never hand-write
   `impl Drop` on a wrapper** — the base `define_type!` type is intentionally
   `Drop`-less (so it stays embeddable by value, and `from_ptr` borrows never
   dispose); teardown comes *only* from the macro + the owning wrapper.
4. **Thread-safety markers** (`Send`, optionally `Sync`) — with
   SAFETY justification. Default conservatively to `Send`-only.
5. **Constructors** that in-scope subsystems call, returning
   `Option<CUniqueArc<Self>>` (refcounted) or `Option<CBox<Self>>`
   (single-owned).
6. **Field accessors** for in-scope-accessed fields only. Each has
   a `Replaces:` line citing the C function it stands in for (or
   "direct field access in `<file>`" when no C accessor exists) and
   a SAFETY comment naming the specific invariant.
7. **Method wrappers** for in-scope-called C functions. Same
   `Replaces:` line convention.

What is **not** mandatory: exhaustive coverage. If a field or
method isn't called by anything in-scope, it doesn't get a wrapper.
Adding speculative wrappers is forbidden per the
[scope filter](#2-scope-filter--what-gets-a-wrapper).

---

## 6. Field accessor policy in type wrappers

A type wrapper's field accessors **always raw-project the field** — they
expose the field directly through `addr_of!`, derived from the field
*layout*, never by calling a C getter/setter. (The *how* per field shape
— `addr_of!` read/write for scalars, `&T` borrow for embedded values,
`CBox`/`CArc` for owned pointers, slice/`CVec` for arrays — lives in the
type-wrapper prompt and the
[Access discipline](#8-access-discipline-at-the-ffi-boundary) section.)

### 6.1 Non-trivial work over a field is NOT a field accessor

A C function that does more than expose a field — transformation
(`OSSL_TIME` → seconds), version handling, locking, multi-field
computation, validation, lazy compute-and-cache (e.g. a getter that
populates `obj->summary` on first call) — is a **behaviour function**,
not a field accessor. It is wrapped as a *symbol* (`symbol_wrapper.md`)
or ported, **never emitted by the type wrapper and never reimplemented
here as a raw projection** (raw-projecting it would skip its work — read
a stale/`NULL` field, drop its side effects). The type wrapper emits only
raw field projections; anything that can't be expressed as one isn't a
field accessor.

This is why a field record carries no getter/setter function list:
"is there a pure C accessor for this field, and what does it do?" is not
the wrapper's question — pure field exposure *is* a raw projection, and
everything else is a separate symbol.

### 6.2 Field reads are raw `addr_of!` projections

Read the field directly through `addr_of!`, with a `// SAFETY:` comment
naming the invariant that justifies the read (immutability after init,
lock held, exclusive ownership — see the
[Access discipline](#8-access-discipline-at-the-ffi-boundary)
justification table). The `Replaces:` line cites the underlying access
(`direct field access in <file>`).

```rust
pub fn protocol_version(&self) -> c_int {
    // SAFETY: ssl_version is set at handshake start, immutable
    // thereafter while refcount >= 1.
    unsafe { core::ptr::addr_of!((*self.as_ptr()).ssl_version).read() }
}
```

Internal fields (`ext.*`, internal flags) with no public C accessor are
read identically — the source of the field makes no difference to the
mechanism, only to the SAFETY invariant you cite:

```rust
pub fn max_fragment_length(&self) -> u8 {
    // SAFETY: extension state; written only at handshake completion,
    // immutable thereafter.
    unsafe { core::ptr::addr_of!((*self.as_ptr()).ext.max_fragment_len_mode).read() }
}
```

**Atomic / racy fields** (refcounts, anything touched by
`CRYPTO_UP_REF` / `CRYPTO_DOWN_REF`) are a hard exception: never use
`(*p).field` or a `&` borrow; always go through `addr_of!` + an atomic
load. The `addr_of!` form is mandatory for every field read; racy fields
additionally require the atomic load on top.

---

## 7. Method wrapper policy

For every C function on a type that's called by in-scope subsystems,
emit a Rust method.

**Naming:** convert `SSL_SESSION_get_master_key` → `master_key`
(drop the type prefix and `get_` / `set_` qualifier when natural).
When in doubt, match `rust-openssl`'s naming where possible.

**Signature:**

- Borrowed parameters → `&Foo` (deref'd from the wrapper if needed).
- Owned-by-C parameters → `Foo` (consumes; impl forwards
  `.into_raw()`).
- Returns wrapped types in `Result<CArc<_>, ErrorStack>` or
  `Option<CArc<_>>` patterns; **never raw pointers**, unless
  subject to allowlist bellow.

**Body:** call the C function, `cvt`-style error conversion, wrap
the return in the appropriate crustify type.

```rust
pub fn set_certificate(&mut self, cert: &X509) -> Result<(), ErrorStack> {
    unsafe {
        cvt(ffi::SSL_CTX_use_certificate(self.as_ptr(), cert.as_ptr()))?;
    }
    Ok(())
}
```

The `Replaces: <C_FUNCTION_NAME> (<source_file>.c)` doc-comment line
is mandatory — it gives reviewers and future porters a direct map
from each Rust method back to its C origin.

### 7.1 Buffer-reservation and read-window methods

When a C function reserves a region inside an internally-managed
buffer and returns a raw pointer into it (WPACKET pattern), or when
a C type tracks a read cursor over an externally-owned buffer
(PACKET pattern), wrap the result as a Rust slice tied to the
wrapper's borrow — not as a raw pointer.

**WPACKET write reservation.** `WPACKET_allocate_bytes` writes a
`*mut u8` into an out-param; the pointed-at memory is valid for the
lifetime of the `&mut Wpacket` borrow. Return `&mut [u8]` from
`&mut self`. The exclusive borrow prevents any other WPACKET method
(hence any internal realloc) while the slice is live.

```rust
pub fn allocate_bytes(&mut self, n: usize) -> Result<&mut [u8], WpacketError> {
    let mut ptr: *mut u8 = core::ptr::null_mut();
    let ok = unsafe { ffi::WPACKET_allocate_bytes(self.as_ptr(), n, &mut ptr) };
    if ok != 1 || ptr.is_null() { return Err(WpacketError::AllocFailed); }
    // SAFETY: WPACKET reserved n contiguous bytes at ptr; &mut self
    // prevents any subsequent WPACKET call (hence realloc) for the
    // slice's lifetime.
    Ok(unsafe { core::slice::from_raw_parts_mut(ptr, n) })
}
```

**PACKET read window.** `PACKET_data` + `PACKET_remaining` expose the
current read position and remaining byte count. Return `&[u8]` from
`&self`. The shared borrow prevents any advancing method while the
slice is live.

```rust
pub fn remaining_bytes(&self) -> &[u8] {
    let p = self.as_ptr();
    let ptr = unsafe { ffi::PACKET_data(p) };
    let len = unsafe { ffi::PACKET_remaining(p) } as usize;
    // SAFETY: PACKET tracks its read window; &self prevents any
    // advancing method for the slice's lifetime.
    unsafe { core::slice::from_raw_parts(ptr, len) }
}
```

The caller uses the returned slice with standard Rust bounds-checked
operations — no call-site unsafe.

**Caller-retained buffer variant.** A small set of WPACKET
initialisers (`WPACKET_init_der`, etc.) store a *caller-provided*
buffer pointer inside the WPACKET for the duration of its lifetime.
The wrapper must carry a `'buf` lifetime that ties the caller's
buffer borrow to the wrapper's existence, enforcing that the buffer
strictly outlives the WPACKET.

```rust
pub struct WpacketWithBuf<'buf> {
    inner: Wpacket,
    _buf: PhantomData<&'buf mut [u8]>,
}

impl<'buf> WpacketWithBuf<'buf> {
    pub fn init_der(buf: &'buf mut [u8]) -> Result<Self, WpacketError> {
        let mut inner = Wpacket::uninit();
        let ok = unsafe {
            ffi::WPACKET_init_der(inner.as_ptr(), buf.as_mut_ptr(), buf.len())
        };
        if ok != 1 { return Err(WpacketError::InitFailed); }
        // SAFETY: WPACKET stores buf.as_mut_ptr() for its lifetime.
        // The 'buf bound on Self ensures the buffer outlives Self;
        // Drop runs WPACKET_cleanup before 'buf expires.
        Ok(Self { inner, _buf: PhantomData })
    }
}
```

No shared crustify primitive exists yet for this pattern. When the
first use site lands, design it in the owning crate and lift to a
project helper once the API stabilises.

### 7.2 Structural pointers — use `SelfPtr<'this, T>`

When a C struct field holds a pointer that refers back into the same
struct, into a parent struct, or into a parse-state buffer owned by
the same object, wrap it with `crustify::SelfPtr<'this, T>`. **Do not
leave these as raw pointers.** `SelfPtr` is implemented and available;
encountering a structural pointer field is a signal to apply it, not
to add an allowlist entry.

Structural pointer shapes and their `SelfPtr` mapping:

| Shape | C example | `SelfPtr` form |
|---|---|---|
| Interior pointer — field points into another field of the same struct | `cert_st.key → cert_st.pkeys[active]` | `SelfPtr::new(&self.pkeys[active_idx])` (safe — derived from real borrow) |
| Back-pointer — embedded child holds pointer to its containing parent | `record_layer_st.s → ssl_connection_st` | `SelfPtr::from_raw((*rl).s)` (unsafe — C-provided; assert outlives `'this`) |
| Parse-state cursor — pointer into a buffer owned by the same struct | `init_msg → init_buf.data + offset` | `SelfPtr::new(&self.init_buf[offset..])` or slice accessor if length is in hand |

**Construction.** Two paths:

```rust
// Safe — derive from a real Rust borrow; borrow checker proves 'this.
let key: SelfPtr<'_, CertPkey> = SelfPtr::new(&cert.pkeys[active_idx]);

// Unsafe — C-provided pointer; caller asserts pointee is valid for 'this.
let s: SelfPtr<'this, SslConnection> =
    unsafe { SelfPtr::from_raw((*rl_ptr).s) }.expect("non-null");
```

**Reading.** `SelfPtr::get` returns `&'this T`, bounded by the
`'this` lifetime the pointer was derived from — not by `&self`. The
wrapper can therefore be copied and the reference remains valid as long
as the parent is alive.

**Layout.** `SelfPtr<'this, T>` is `#[repr(transparent)]` over
`NonNull<T>`, so `Option<SelfPtr<'this, T>>` is layout-compatible with
`*const T` and can replace a raw pointer field in a `#[repr(C)]`
struct without changing the C-side layout or ABI.

### 7.3 Type-erased fn-pointer trampolines

Some C APIs store a callback as an opaque fn-pointer type (often
`unsafe extern "C" fn()` with no arguments) and cast it back to the
concrete signature at the call site. The Rust function must match the
*concrete* signature C expects at the call site exactly; it is then
cast to the opaque type when handed to C.

```rust
// Concrete signature C will call through at call time:
unsafe extern "C" fn msg_cb(
    _write_p: c_int,
    _version: c_int,
    _content_type: c_int,
    buf: *const c_void,
    len: usize,
    ssl: *mut ffi::ssl_st,
    arg: *mut c_void,
) {
    // SAFETY: C guarantees buf is len bytes; ssl is a live SSL*;
    // arg is our Rust state planted by SSL_CTX_set_msg_callback_arg.
    let _conn = unsafe { SslConnection::from_ptr(ssl as *mut _) };
    let _data = unsafe { core::slice::from_raw_parts(buf as *const u8, len) };
    // remainder in safe Rust
}

// Registration — cast to whatever opaque type C stores:
unsafe { ffi::SSL_CTX_set_msg_callback(ctx.as_ptr(), Some(msg_cb)) };
```

The unsafe is confined to the deserialisation of the raw arguments at
the top of the body. Nothing below that point needs unsafe.

**`qsort` / `bsearch` comparators** follow the same shell. Their
`*const c_void` arguments are double-pointers when the array holds
pointer elements — apply the double-pointer read pattern (cast to
`*const *const T`, deref twice) inside the unsafe block, then
delegate to a safe comparison function.

**Callback machinery is split across two patterns.** Together they
cover the full callback ABI:

- **Userdata half** — the `*mut c_void` slot carrying Rust state is
  handled by `COwnable` (`into_foreign` to plant, `from_foreign_ref`
  to recover). See `crustify::COwnable` docs.
- **Function-pointer half** — the fn-pointer, whether typed or
  type-erased, is always `unsafe extern "C" fn` with the exact
  concrete signature. This section covers that half.

### 7.4 Where the wrapper lives

Three placements, depending on the function's scope band (see
[§"Scope filter"](#2-scope-filter--what-gets-a-wrapper)):

| Scope band | Lives in | File |
|---|---|---|
| **Port scope — already ported** | The C file's library **port crate**, `rust/<linked_in>/` (e.g. `rust/libssl`) | The C file's **full vanilla path** mirrored under the crate's `src/`, extension swapped for `.rs` (`ssl/statem/extensions.c` in `libssl` → `rust/libssl/src/ssl/statem/extensions.rs`). The Rust function here is **idiomatic** — typed wrappers, **no `extern "C"`, no `#[no_mangle]`, no raw signature**; its raw C-ABI re-export lives in **this same file's `mod ffi_export`**, which reconstructs the wrappers and delegates to it (§1.4). |
| **Port scope — not yet ported** | Same crate + vanilla path, **as a thin safe wrapper around the C symbol** until the port lands | Same vanilla-path file. The wrapper signature is the eventual Rust signature; the body is `unsafe { ffi::SSL_FOO_bar(...) }`. When the port lands, only the body changes; call sites don't move. |
| **Wrap scope** (dependency or sibling subsystem the port doesn't own) | The dependency's library **port crate**, `rust/<linked_in>/` (e.g. `rust/libcrypto`) | Same full-vanilla-path rule: `crypto/hpke/hpke_util.c` in `libcrypto` → `rust/libcrypto/src/crypto/hpke/hpke_util.rs`. The wrapper body forwards to `ffi::`. |

When the subsystem-internal C function is callable only via its
parent C file (no header declaration), the C side gets a public
shim — see [§"Unported statics get an unconditional public wrapper"](#13-unported-statics-get-an-unconditional-public-wrapper) —
and the Rust wrapper calls the shim, not the original static.

A port-scope wrapper whose body is still a `ffi::` call is **owed
work** — mark it with a `// TODO(port-budget):` comment. Same status as
an unwritten accessor — not a closed decision. The wrapper's existence
today is a typed-API placeholder so call sites compile; the body gets
replaced when the underlying function is ported.

---

## 8. Access discipline at the FFI boundary

When the wrapped C struct is still being read or written by C (the
typical incremental-port state), the wrapper must stay layout-
compatible with the C struct. Rust does not translate the layout —
it translates the *access pattern* via projection methods.

**Layout — unchanged.** Wrap with `crustify::define_type!` /
`crustify::CType<T>`. Same bytes, same field offsets, same alignment.
The C side keeps writing the same struct.

**Access — always read and write through `addr_of!` / `addr_of_mut!`,
never through a bare `(*ptr).field` place expression.**

The mandatory forms:

```rust
// Read
let x = unsafe { core::ptr::addr_of!((*ptr).field).read() };
// Write
unsafe { core::ptr::addr_of_mut!((*ptr).field).write(v) };
```

These are the *only* forms permitted for field access through a raw
pointer. The constructs that synthesise a borrow are forbidden; the
constructs that go through a place expression on the raw pointer (the
bare `(*ptr).field` forms) are technically equivalent for Copy
fields but **also forbidden** as a matter of discipline — see the
rationale below.

| Construct | Synthesises a borrow? | Use? |
|---|---|---|
| `addr_of!((*ptr).field).read()` | **No** — pointer to place + byte-copy load | ✅ **mandatory read form** |
| `addr_of_mut!((*ptr).field).write(v)` | **No** — pointer to place + byte-copy store | ✅ **mandatory write form** |
| `addr_of!((*ptr).field)` / `addr_of_mut!((*ptr).field)` (passing the pointer to C) | **No** | ✅ for out-params and `&field`-C-side |
| `let x = (*ptr).field;` (Copy field, value context) | No — place-to-value load | ❌ **avoid** — use `addr_of!(...).read()` |
| `(*ptr).field = v;` (assignment) | No — direct store | ❌ **avoid** — use `addr_of_mut!(...).write(v)` |
| `&(*ptr).field` / `&mut (*ptr).field` | **Yes** — `&T` / `&mut T` materialised | ❌ **never** — synthesises an aliasing claim Rust can't honour over FFI memory |
| `(*ptr).field.method()` where method takes `&self` / `&mut self` | **Yes** — autoref | ❌ **never on FFI places**; copy the value out via `addr_of!(...).read()` first |

### 8.1 Why `addr_of!` / `addr_of_mut!` are mandatory even for Copy fields

For a Copy field, `let x = (*ptr).field;` and
`let x = unsafe { addr_of!((*ptr).field).read() };` produce identical
machine code under rustc today. The reason the project mandates the
`addr_of!` form anyway, even though it's strictly equivalent right now:

1. **Place-expression semantics through raw pointers are
   under-specified.** Tree Borrows / Stacked Borrows haven't fully
   settled whether evaluating `(*ptr)` momentarily materialises a
   `&T` / `&mut T` reborrow over the parent struct. The conservative
   posture — guaranteed by `addr_of!` — is that no parent reference
   ever exists, not even briefly.
2. **Symmetric with the write-side rule.** `addr_of_mut!` is
   genuinely required when the field is concurrently mutated (the
   racing-field hazard from atomic refcounts). Reading is the
   symmetric case; using the same syntax for both keeps the
   audit-mental-model uniform.
3. **Visual signal for reviewers.** `addr_of!` in source code is a
   marker that says "I'm reading through a raw pointer and being
   deliberate about it." Bare `(*ptr).field` reads look like normal
   Rust and might escape review. The discipline makes "raw-pointer
   field access" greppable.
4. **One shape always works.** No per-field judgment about whether
   the field is `Copy`, whether it could race, whether the parent
   could be packed, whether a future rustc tightening might apply.
   `addr_of!` is sound in every case; the bare form is sound only
   under a per-field analysis the reader has to redo each time.

The cost of the rule is 12 characters of source code per access. The
benefit is a single, uniform, future-proof access discipline that
needs no per-site adjudication.

### 8.2 Borrow-synthesising forms

The borrow-synthesising rows (`&(*ptr).field`, `&mut (*ptr).field`,
autoref via `(*ptr).field.method()`) are **forbidden** on FFI memory
because Rust's abstract machine asserts no other writer (for `&mut`)
or no other mutation (for `&`) for the borrow's lifetime —
assertions the C side cannot honour. Even cast to `*mut` one
expression later, the borrow's brief existence is enough to
invalidate the parent `&self` borrow stack (Stacked / Tree Borrows)
and to give LLVM `noalias` information it can't honour.

**Do not implement `Deref<Target = ffi::T>` or `DerefMut<Target = ffi::T>`
on a wrapper.** Both would return `&ffi::T` / `&mut ffi::T` with
`noalias readonly` / `noalias` claims that span the borrow's lifetime.
Any named projection (`let key = &sc.ext.session_id;`) extends that
borrow across subsequent code, including FFI calls. When the C side
mutates the same memory through any other pointer during the borrow
(callbacks, alert dispatch, BIO writes, parallel cache walkers), the
optimizer's `noalias` assumption is violated and reads can stale-cache
or reorder around the mutation. The `UnsafeCell<MaybeUninit<T>>`
underlying storage only defeats `noalias` on the wrapper's own
`&Self` — once `Deref` extracts a naked `&ffi::T`, the standard
`noalias` rules apply again. Use per-field accessors that produce
value reads or `addr_of!` projections; never expose the whole struct
as a `&ffi::T`.

The greppable lint:

```bash
# Inside every rust/<linked_in> port crate, expect zero matches.
# Any hit is either a bug or a case that should use addr_of! / addr_of_mut!.
# The `(^|[^&])` prefix excludes `&&` (logical-AND) false positives.
grep -rnE '(^|[^&])&\s*(mut\s+)?\(\*[a-zA-Z_.()]+(\.as_ptr\(\))?\)\.' rust/
```

**You may materialise `&T` / `&mut T` directly when an audited SAFETY
comment names a local invariant that rules out concurrent writers
for the borrow's lifetime.** The burden of proof is on the comment.
Categories where the proof is usually available:

| Justification | Typical SAFETY-comment shape |
|---|---|
| **Config-time-only field** — written by `SSL_CTX_set_*` or similar before any connection is active; immutable thereafter | "C-side discipline: `field` is written only during config-time before connections start; this method runs on the active path so no writer is live." |
| **Lock-protected field** — wrapper method holds the relevant `CRYPTO_THREAD_*lock` for the scope of the borrow | "Method holds `ctx->lock` for the duration of the returned borrow; the lock excludes all C-side writers per [`ssl_local.h` lock discipline]." |
| **Exclusively-owned wrapper** — we hold the sole `CBox<T>` (no clones, no `CArc` shared with C); no aliasing handles outside Rust | "Wrapper is `CBox`-owned and no FFI export hands the underlying pointer to C for storage; no aliasing handle exists." |
| **Post-handshake-immutable field** — written during handshake, then frozen for the connection's lifetime | "`field` is written only by `tls_process_*` during handshake; this method is reachable only post-handshake (see method's `&self` borrow on `SslConnection`)." |
| **Inline scalar / fixed array** — `c_int`, `u32`, `[u8; N]` etc. that hold a value, not a pointer to externally-mutated state | "`field` is an inline scalar; the value-level borrow does not propagate aliasing into another allocation." (Sufficient on its own only if the field isn't independently mutated by C; combine with one of the rows above if it is.) |

If none of those apply — or you can't write a specific, falsifiable
SAFETY comment — fall back to the `addr_of!` default. "I think it's
fine" is not an audited justification.

**Atomic fields are a hard exception.** Refcount counters, the
`references` field, any field touched by `CRYPTO_UP_REF` /
`CRYPTO_DOWN_REF` or other atomic primitives: **never** read or
write through `(*p).field` or a `&` / `&mut` borrow, even with a
SAFETY comment. The compiler is free to fuse, hoist, or reorder
non-atomic accesses in ways that race with concurrent atomic
mutations, regardless of any borrow-level proof. Use atomic loads
and stores on the address obtained from `addr_of!(...)` /
`addr_of_mut!(...)` with the appropriate ordering — or, better,
call the C accessor function (`SSL_SESSION_up_ref`, etc.) which
uses the right primitive internally.

**Conversion to `&T` happens at the projection-method boundary**,
not at field access. The wrapper's method takes `&self` or
`&mut self` on the *outer* wrapper, performs the raw-pointer read,
and converts the result to an `&T` whose lifetime is tied to the
method's `&self` parameter. That borrow is what propagates the
aliasing contract — anyone who got the `&T` cannot still hold it
when someone calls a `&mut self` method that would write.

```rust
pub fn active(&self) -> Option<&CertPkey> {
    // SAFETY: holding &self → no Rust-side &mut access can race.
    // C-side discipline says cert->key is written only under the
    // documented lock / config-time path.
    let cert_ptr = self.get();
    let key = unsafe { core::ptr::addr_of!((*cert_ptr).key).read() };
    if key.is_null() { return None; }
    // The returned reference's lifetime is bounded by `&self`.
    Some(unsafe { CertPkey::from_ptr(key) }.expect("non-null"))
}
```

---

## 9. Raw-pointer allowlist — tolerated exceptions

The discipline above bans raw `*mut ffi::T` / `*const ffi::T` from
public wrapper APIs by default. A public method that traffics in raw
pointers is one of three things:

1. A **bug** — fix it (convert to the typed form).
2. An **allowlist entry** (A1 or A2 below) — raw is *correct as-is*,
   because of a real structural constraint: the FFI signature
   demands it, or it's blocked on a larger migration that is itself
   tracked work.
3. A **wrapper that's owed** — the typed shape is writable today,
   nobody's written it yet. This is incomplete-port work, not a
   tolerated exception. Write it yourself unless you have a
   justified reason not to; if you defer it, mark it with a
   `// TODO(port-budget):` comment at the site. It does **not**
   belong on this allowlist.

When adding a new raw-pointer-trafficking method, decide which of
the three it is. If it's (2), add an entry here with the structural
reason. If it's (3), close it or mark it `// TODO(port-budget):` at the
site — don't grow this list with work that's simply unwritten.

### 9.1 A1 — Mixed owner/borrow at runtime (Category B migration required)

**Pattern.** A C struct field that *sometimes* aliases another field
of the same struct without bumping a refcount (a borrow), and
*sometimes* carries its own refcount (an owner). The C destructor
distinguishes the two states by walking the array of canonical
owners and NULL'ing the borrowed field before freeing.

**Reason for raw exception — and why no Rust primitive resolves it
under Category A.** No single layout-compatible Rust type can
statically capture "owner-or-borrow":
- `CArc<T>` (always own) → either silently up_ref's on alias
  (changes the C-observable refcount) or double-frees at Drop.
- `SelfPtr<'this, T>` (always borrow) → covers the alias case but
  can't represent the independent-owner state.
- Tagged enum (`Owned(CArc) | InArray(idx)`) → encodes the
  discriminant in the type, but changes the field layout to
  discriminant + payload. C-side reads break.
- Tagged low-bit pointer → preserves byte count but requires C-side
  cooperation to mask the tag bit on every read.

The runtime-discriminant nature is intrinsic to the data shape; the
discriminant lives in *which other field* a pointer happens to
equal, which can't be expressed as a compile-time type without
changing layout.

**Conversion path.** Wait for Category B migration of the containing
sub-struct (e.g. `s3.tmp`). Once Rust owns the layout, the
tagged-enum representation is the natural shape — the runtime alias
check disappears because the enum *variant* statically distinguishes
owned from borrowed. The crustify `SelfPtr<'this, T>` primitive
helps the simpler always-borrow cases (back-pointers, intrusive
list siblings) but is **not** the unblocker for this row — what
unblocks it is layout sovereignty.

In the meantime: the raw `*mut T` field stays, and cleanup logic
that touches these fields must replicate the C destructor's
alias-detect-then-NULL discipline, or accept the clone-and-own
behavioural drift.

**Current entries:**
- `SslConnection::set_s3_peer_tmp` / `s3_peer_tmp_ptr`
- `SslConnection::set_s3_tmp_pkey` / `s3_tmp_pkey_ptr`
- `SslConnection::set_s3_tmp_ks_pkey(idx, ...)` / `s3_tmp_ks_pkey(idx)`

### 9.2 A2 — Out-param field-address helpers

**Pattern.** Public method returns the address of a struct field
(`*mut *mut u8`, `*mut usize`, `*mut *mut c_char`) to be passed
directly to a C out-param function (`PACKET_memdup`,
`OPENSSL_strdup`, etc.).

**Reason for raw exception.** The C function signature *requires*
the raw pointer; converting to `COut<T>` at the wrapper boundary
would just be re-wrapped at the call site. The legitimate use is at
a single C function call, not in long-lived state.

**Conversion path.** Optionally repackage as scoped closures
(`with_X_field_ptrs(|ptr, len| { ffi::C_fn(ptr, len) })`) to scope
the raw pointer to a closure body and hide it from outer callers.
Marginal win; low priority.

**Current entries:**
- `SslConnection::ext_tls13_cookie_field_ptrs`
- `SslConnection::ext_peer_ecpointformats_field_ptrs`
- `SslConnection::ext_peer_supportedgroups_field_ptrs`
- `SslConnection::ext_hostname_field_ptr`
- `SslConnection::s3_alpn_proposed_field_ptrs`

### 9.3 A3 — Intrusive linked-list siblings (blocked on crustify infrastructure)

**Pattern.** A struct contains `prev` / `next` pointers that link it
into one or more intrusive linked lists managed by C code (session
cache LRU, BIO chain, cipher list). The correct typed wrapper is
`crustify::CList<T, ID>` / `CListArc<T, ID>` — a const-generic ID
distinguishes multi-list membership and prevents cross-list splicing.

**Reason for raw exception.** The `CList` / `CListArc` primitives do
not exist yet in crustify (see [`crustify/docs/rfl.md`
§10](../../../crustify/docs/rfl.md)). No layout-compatible Rust type
can safely represent a node that is simultaneously a member of
multiple lists with overlapping pointer fields without the
const-generic ID guard. Using `*mut T` directly is the only
sound option until the primitive lands.

**Conversion path.** Once `CList<T, ID>` / `CListArc<T, ID>` are
implemented in crustify, replace every raw `prev` / `next` field
access with the typed node accessor. The change is local to the
wrapper type and does not affect ABI.

**Current entries:**
- `ssl_session_st.prev` / `.next` (session cache LRU chain)
- BIO chain `next_bio` / `prev_bio`

### 9.4 Maintenance

A1, A2, and A3 are the only standing allowlist entries. A1 dies when
its containing sub-struct undergoes Category B migration; A2 is a
permanent FFI-seam fact and stays; A3 dies when `CList` / `CListArc`
land in crustify.

Raw-pointer methods that exist only because a wrapper hasn't been
written are not allowlisted — they are owed work, marked with a
`// TODO(port-budget):` comment at the site.

When reviewing a new wrapper method that traffics raw pointers:
classify it (bug / allowlist / owed wrapper). "I couldn't think of a
typed shape" is not a justification for an allowlist entry —
escalate to a design discussion if genuinely blocked.

---

## 10. Borrowing across the FFI boundary — pattern decision order

When a C function fills a pointer to data living inside a parent object
(sub-struct reachable via raw pointer, stack entry, table slot, …), the
returned pointer's true lifetime is governed by C-side allocation rules
that Rust's type system cannot directly express.  The choice of Rust
return shape determines whether the caller can also mutate the parent
afterwards, and whether the function can be safe or must be `unsafe`.

Three patterns are available.  Try them in order — use the first one
that fits.  Each subsequent option has a higher cost (extra indirection,
extra allocation, or extra runtime work); reaching for a later option
when an earlier one would have worked is a code-review concern.

### 10.1 Input-tied borrow (default — try this first)

Return a borrow whose lifetime is elided from an input parameter, and
let the caller phase-separate reads and writes.  No name needed — this
is just the borrow checker doing its job.

```rust
pub fn pick_entry<'a>(parent: &'a Parent) -> Option<(&'a Entry, Meta)> { … }
```

Caller pattern: extract the values needed across the upcoming mutation
*before* the first `&mut`, then drop the borrow.  Non-lexical lifetimes
end the borrow at last use; an explicit `{ … }` block makes the phase
boundary unmissable.

```rust
let (id, payload, meta) = {
    let (entry, m) = pick_entry(parent)?;     // &parent live
    (entry.id(), entry.payload().to_vec(), m)
};  // entry borrow ends here

parent.set_attempted(id);                      // &mut parent — fine
parent.set_payload(&payload);
```

**Applies when:**
- All caller reads of the returned data can finish before the first
  mutation of the parent (i.e. there's no read-decide-write-read cycle
  that requires the borrow to span mutations).
- The data structure on the C side is **not** subject to invalidation
  via shared (`&`) aliasing — e.g. it can't be freed or reallocated by
  a refcount drop or callback that runs while `&'a parent` is held.
  For libssl, the SSL_CONNECTION during a handshake satisfies this; the
  SSL_CTX (refcounted, shared) does not.

#### 1a. Materialise the reference immediately after the raw-pointer return

A variant of Option 1 for the case where a helper returns a
`*const Child` (a raw pointer with no lifetime anchored to the parent),
but the earlier code used `as_ref()` inside a `match` arm and the
compiler inferred a lifetime that spanned a later `&mut parent` borrow,
causing a conflict.

The fix: null-check the pointer, then call `unsafe { &*child_ptr }`
immediately — as a named binding, before any guard on the value.
Because `child_ptr` is a raw pointer, the resulting `&Child` lifetime
is inferred independently of `parent`, so passing `&mut parent` to a
later callee is conflict-free.

```rust
// Helper — returns a raw pointer; carries no borrow on sc.
fn get_child(sc: &SslConnection) -> *const Child { … }

// Caller
let child_ptr = get_child(sc);
if child_ptr.is_null() {
    return NOT_SENT;
}
// SAFETY: non-null (checked above); points into sc's backing allocation;
// no call below frees or reallocates it.
// Materialise here — lifetime inferred independently of sc — so the
// &mut sc in use_child does not conflict.
let child = unsafe { &*child_ptr };
if child.is_empty() {
    return NOT_SENT;
}
// &mut sc is fine: child's lifetime has no type-level connection to sc.
use_child(sc, child, pkt)
```

Do **not** derive the reference inside a `match` arm on `as_ref()` and
then bind the arm result to a named variable — the compiler may infer
the binding's lifetime to extend past the `&mut parent` call, producing
a spurious conflict even though the memory regions are disjoint.

**Applies when:**
- The helper returns `*const Child` (raw pointer, no parent lifetime).
- The child reference and `&mut parent` are needed in the same scope.
- The child lives in the parent's backing C allocation (not
  independently heap-allocated with its own lifetime).

**libssl example:** `construct_ca_names` in
`libssl-extensions/src/extensions.rs`.  `get_ca_names(sc)` returns a
`*const StackOf<X509Name>` pointing into `sc`'s `SSL_CTX` CA list.
`construct_ca_names` needs both the stack (to iterate names) and
`&mut sc` (for `ssl_fatal!` and `sc.options()`).  Materialising
`&StackOf<X509Name>` right after `get_ca_names` (explicit null check +
`unsafe { &*ca_sk_ptr }`) gives a reference whose lifetime does not
conflict with the `&mut sc` passed to `construct_ca_names`.

**ECH note:** this option was rejected for `ech_pick_matching_cfg` for
the first reason — the caller (`tls_construct_ctos_ech` in
`libssl-extensions/src/clnt.rs`) needs `&mut sc` immediately after
picking, both for write-backs and for further `&sc` reads (HPKE info
build, encapsulation buffer allocation), and the read/write interleave
cannot be linearised into a clean phase boundary without copying out
anyway.  Where the caller pattern *does* allow phase separation, this
option remains the default — no extra crate, no allocation, no copy.

### 10.2 Refcounted handle (`CArc`)

If the pointee has an independent C-side refcount (e.g.
`X509_up_ref` / `X509_free`, `SSL_SESSION_up_ref` / `SSL_SESSION_free`),
build a [`CArc<T>`](../../../../crustify/src/smart_pointers.rs) handle.
Owning a `CArc<T>` is one share of the C refcount; drop runs the C
`free` function.  The handle's lifetime is genuinely independent of any
parent — a `&CArc<T>` borrow does not hold a parent live.

This is the analogue of `Arc<T>` for shared-ownership Rust values, and
of `ARef<T>` from Rust-for-Linux for kernel-owned refcounted objects.

```rust
pub fn acquire_session(sc: &SslConnection) -> Option<CArc<SslSession>> { … }

// Caller — no parent borrow held while session is in use:
let session = acquire_session(sc)?;       // bumps refcount
sc.set_attempted();                        // ✓ — sc not borrowed
let id = session.id();                     // &session, not &sc
```

**Applies when:**
- The pointee has C-side `*_up_ref` / `*_free` symbols.
- You can afford the atomic refcount increment/decrement (negligible in
  most TLS handshake paths).

**ECH note:** this option is **not available** for ECH today.
`OSSL_ECHSTORE` has `OSSL_ECHSTORE_new` / `OSSL_ECHSTORE_free` but no
`up_ref`; entries (`OSSL_ECHSTORE_ENTRY`) are owned inline by the store
and not independently refcounted.  Adding refcounting upstream is a
real design lever — file a discussion before choosing option 3 if the
copy cost matters.  Established refcounted candidates in libssl
(`SSL_SESSION`, `X509`, `SSL_CTX`, `EVP_PKEY`, etc.) **should** use
this pattern.

### 10.3 Owned snapshot

Copy the needed scalars and byte slices out of the C struct into a
plain Rust struct before returning.  The input borrow ends at the
function boundary; the caller owns the snapshot.

```rust
pub struct EntryMatch {
    pub id:      u8,
    pub payload: Vec<u8>,
    pub meta:    Meta,
}

pub fn pick_entry(parent: &Parent) -> Option<EntryMatch> {
    let mut p: *mut FfiEntry = core::ptr::null_mut();
    let ok = unsafe { ffi::ffi_pick(parent.as_ptr(), &mut p) };
    if ok != 1 || p.is_null() { return None; }
    // Copy out under the brief raw-pointer-validity window.
    Some(unsafe {
        EntryMatch {
            id:      (*p).id,
            payload: copy_slice((*p).payload_ptr, (*p).payload_len),
            meta:    Meta::from_raw((*p).meta),
        }
    })
}
```

The function is `pub fn` — fully safe at the signature level — and the
returned struct has no lifetime annotation.  Caller destructures and
uses the owned data freely:

```rust
let EntryMatch { id, payload, meta } = pick_entry(parent)?;
parent.set_attempted(id);                          // ✓
let ctx = build_ctx(&payload, meta);
parent.set_ctx(ctx);                                // ✓
```

**Applies when:**
- Options 1 and 2 don't fit (caller pattern can't phase-separate; no
  C-side refcount).
- The data needed is finite and small enough that copying is cheap
  relative to surrounding work.

**Cost:** one or two small heap allocations per call.  Acceptable in
handshake-class code paths; potentially significant in hot per-record
paths — measure if uncertain.

**Benefit:** safe signature, no misleading lifetime, borrow checker
works at every call site, no unsafe escape hatch.

**Worked ECH example** —
[`rust/ssl/src/ech/internal.rs::ech_pick_matching_cfg`](../rust/ssl/src/ech/internal.rs):
returns an owned `EchConfigMatch` carrying `version`, `config_id`,
`max_name_length`, `hpke_suite`, plus owned `pub_bytes` (~65 B) and
`encoded_bytes` (a few hundred B).  The caller
[`libssl-extensions/src/clnt.rs::tls_construct_ctos_ech`](../rust/libssl-extensions/src/clnt.rs)
destructures the match and proceeds with `&mut sc` writes without
restriction.  Copy cost is negligible against the HPKE encapsulation
that follows.

### 10.4 What never to do

**Do not return `&'static T` from an `unsafe fn` to launder lifetimes.**
`'static` tells the compiler the data lives forever — a lie when the
data lives inside a heap-allocated parent.  The borrow checker will
*trust* the lie at every call site, silently permitting the reference
to be stored in fields, returned upward, captured in closures, or sent
across threads.  Use of `&'static` for FFI-borrowed data laundered
through an `unsafe` contract is rejected at code review.  If none of
options 1–3 fit, escalate to a design discussion — possibly upstream
(adding C-side refcounting) — rather than reaching for `'static`.

**Do not introduce phantom lifetime parameters that the function body
ignores.** A signature `fn pick<'a>(parent: &Parent) -> &'a Entry`
where `'a` is unconstrained is informationally equivalent to `&'static`
but harder to spot in review.  Same review rule applies.

**Do not return raw pointers from a "safe" wrapper.** If the safe layer
can't express the relationship, the function is `unsafe` and uses a
raw pointer with an explicit `# Safety` contract — keeping the
unsafety visible at every deref.  Raw-pointer returns are subject to
the "Raw-pointer allowlist" policy above.

---

## 11. Generic-over-`T` memory ops

`memcpy` / `memmove` / `memset` / `memcmp` and their project-defined
variants are generic over element type — they have no per-system
identity and do not belong on any wrapper type's method surface. Lower
each C op to its std-level shape:

| C op | Safe shape (preferred) | Unsafe primitive (FFI / `void*` / uninit) |
|---|---|---|
| `memcpy(dst,src,n)` over `[T]` | `dst.copy_from_slice(src)` (`T: Copy`) | `core::ptr::copy_nonoverlapping` |
| `memmove(dst,src,n)` over `[T]` | `slice.copy_within(range, dest)` (`T: Copy`) | `core::ptr::copy` |
| `memset(dst,b,n)` over `[T]` | `slice.fill(value)` (`T: Clone`) | `core::ptr::write_bytes` |
| `memcmp(a,b,n)` over `[T]` | `a == b` / `a.cmp(b)` (`T: PartialEq`) | — |
| `memcpy` of a single struct | `let dst = src;` (move) or `src.clone()` (`T: Clone`) | `core::ptr::copy_nonoverlapping(_, _, 1)` |
| `memset(&x, 0, sizeof T)` | `x = T::default()` (`T: Default`) | `core::ptr::write_bytes` — UB unless all-zero is a valid `T`; prefer `bytemuck::Zeroable` |

`CVec<T, S>::as_(mut_)slice` reaches the safe-shape column from every
clustered array system, so no per-system copy methods are needed.

### 11.1 Project-defined variants (`<P>_constant_time_memcmp`, `<P>_cleanse`, …)

A project's hardened byte-buffer routines (constant-time compare,
secure zeroing, etc.) are also generic over `T`. Canonical landing
spots when the project variant maps onto an existing Rust API — use
them directly, no wrapper:

- `subtle::ConstantTimeEq` — constant-time compare.
- `zeroize::Zeroize` — secure zero.
- `bytemuck::{Pod, Zeroable}` — raw byte views / all-zero validity.

If neither std nor a well-known crate covers the variant, add **one**
free generic function in `<library>-wrappers/src/memops.rs`, generic
over `T` with the capability bound (`T: ConstantTimeEq` + `&T` for
ct-eq; `T: Zeroize` + `&mut T` for cleanse). Never hang these methods
off `CVec`, `CBox`, or per-system newtypes.

---

## 12. Generic C collection with split-ownership elements — marker subtrait pattern

Some C collection types (`OPENSSL_STACK` / `STACK_OF(T)`, certain
`*_LIST` types, hash tables) are uniformly typed at the C level — one
container struct, parameterised only by element type via macro
machinery — but the **ownership semantics differ by element type**.
A `STACK_OF(X509)` owns its elements (push transfers a refcount; the
container's destructor down-refs each on drop).  A `STACK_OF(SSL_CIPHER)`
borrows its elements (push stores a pointer into a file-static table;
the container's destructor only frees the array, not the elements).
The C type system does not distinguish these; both are `OPENSSL_STACK`.

When the Rust wrapper exposes typed operations, a single API shape
cannot serve both cases — owning `push` traffics a Rust-side owned
handle and returns it on failure, while borrowing `push` traffics
`&T` with no failure recovery.  Rust forbids defining the same method
name across two `impl` blocks with disjoint trait bounds: even if a
type T cannot satisfy both bounds, the compiler treats the method
names as duplicate definitions.

### 12.1 The pattern

Decompose the element trait into three layers:

1. **A base trait carrying the destructor info.**  Every element type
   declares its C-side destructor (or its absence) as a constant.  The
   container's `Drop` reads this constant to decide between
   pop-and-free and free-the-container-only.
2. **An *owning* marker subtrait with an associated handle type.**
   Implementors choose `CArc<W>` (refcounted) or `CBox<W>` (single-owner)
   for the `Owned` associated type, where `W` is the
   `#[repr(transparent)]` Rust wrapper for the C type.  The owned-side
   `impl<T: OwningElement> Container<T>` exposes typed
   `push_owned` / `insert_owned` / `pop_owned` / `delete_owned`
   methods returning `Result<(), T::Owned>` (push/insert) or
   `Option<T::Owned>` (pop/delete).  Ownership transfer in both
   directions goes through `COwnable::into_foreign` /
   `COwnable::from_foreign`, which means the same impl block covers
   refcounted and single-owner types uniformly.
3. **A *borrowing* marker subtrait with no associated data.**  The
   borrowed-side `impl<T: BorrowingElement + 'static> Container<T>`
   exposes typed `push_borrowed` / `insert_borrowed` / `pop_borrowed` /
   `delete_borrowed` methods on `&'static T`.  See *Lifetime scope*
   below for why `'static`.

The two `impl` blocks have disjoint trait bounds, so they cover
disjoint instantiations of `Container<T>`.  The `_owned` / `_borrowed`
suffixes prevent the duplicate-definition error and make the
ownership story visible at every call site.

### 12.2 Base trait shape — typed per-element callbacks via associated `Raw`

Whenever the base trait carries a function pointer that the C
container will invoke per element — destructor, comparator, hash
function, equality test, copy/dup function, traversal callback —
type each pointer parameter through an associated `Raw` type that
names the bindgen C-side type, **not** through `*mut c_void`:

```rust
pub trait Element: Sized {
    type Raw;

    // Destructor (e.g. for sk_pop_free / lh_doall_arg cleanup callbacks).
    const FREE_FN: Option<unsafe extern "C" fn(*mut Self::Raw)>;

    // Add other per-element callbacks here in the same shape, e.g.
    // for a hash table:
    //   const HASH_FN: Option<unsafe extern "C" fn(*const Self::Raw) -> u64>;
    //   const CMP_FN:  Option<unsafe extern "C" fn(*const Self::Raw,
    //                                              *const Self::Raw) -> c_int>;
}
```

This is **not** a destructor-only rule. The same shape applies to
every callback parametric in `T`. For example, OpenSSL's `LHASH_OF`
requires both an `OPENSSL_LH_HASHFUNC` and an `OPENSSL_LH_COMPFUNC`
per element type; both would be typed `*const Self::Raw` constants
on the `Element` trait, and the bridging transmute to the C ABI's
`*const c_void` lives once in the wrapper's `lh_new` call site.

Two reasons the rule holds across all callback shapes:

**It closes the wide soundness gap.** With `*mut c_void` (or `*const
c_void`) any ABI-compatible function would type-check as a valid
constant — wrong-allocator destructors, no-op stubs, off-by-one
comparators, hash functions returning a wrong field's bytes. The
implementor's choice is unverifiable, which is the textbook situation
justifying `unsafe trait`. With `*mut Self::Raw` the compiler verifies
the registered function takes a pointer to the right C type;
signature-incompatible substitutes fail to compile. The narrow
residual claim ("this is the *correct* function semantically, not
some other function with matching type") is small enough that
codegen-only impls (which draw each callback directly from the
manifest's verified fields) reduce it to "is the manifest correct?"
— the type analyzer's responsibility, not the impl site's.

**It removes per-impl boilerplate.** Each callback constant is one
line on the impl and zero `unsafe`:

```rust
impl Element for X509 {
    type Raw = ffi::X509;
    const FREE_FN: Option<unsafe extern "C" fn(*mut ffi::X509)>
        = Some(ffi::X509_free);
    // const HASH_FN / CMP_FN: same shape, one line each.
}
```

vs. the `*mut c_void` design's per-impl `unsafe impl` block with one
`mem::transmute` per callback that re-establishes at every impl site
what the trait should have encoded once at the declaration site.

With this shape the trait is **safe trait** — no `unsafe trait`, no
`unsafe impl`. Sub-traits (owning / borrowing markers, sortable /
hashable markers) inherit the same safety story.

The fn-pointer casts the C ABI ultimately requires — the C container
callbacks take `*mut c_void` / `*const c_void` — live in the generic
wrapper's call sites, one cast per callback per generic file, where
they happen once for any `T` and are auditable in one place rather
than scattered across N impl sites:

```rust
impl<T: Element> Drop for Container<T> {
    fn drop(&mut self) {
        let p = self.as_ptr();
        match T::FREE_FN {
            Some(free) => {
                // SAFETY: fn-pointer types with identical calling
                // convention and pointer-sized parameter ABI;
                // `*mut T::Raw` and `*mut c_void` agree at the C ABI.
                let c_free: unsafe extern "C" fn(*mut c_void) =
                    unsafe { core::mem::transmute(free) };
                unsafe { ffi::sk_pop_free(p, Some(c_free)) };
            }
            None => unsafe { ffi::sk_free(p) },
        }
    }
}
```

The pattern at hash-table construction is the same — `lh_new` takes
`(hash_fn, cmp_fn)`; the wrapper transmutes each from its typed form
to the ABI form once, at the `lh_new` call site.

### 12.3 Scope of this rule

The typed-`Raw` rule covers **callback function pointers stored as
trait constants** — values the implementor registers and the C
container later invokes. It does not apply to:

- **Container methods on the wrapper** (`push`, `pop`, `len`, `get`,
  `as_ptr`, …). These are not parametric callbacks; they are typed
  Rust methods that call C functions whose signatures the wrapper
  controls directly.
- **Associated types other than `Raw`** (e.g. the `Owned` handle of
  `OwningStackElement`). These describe Rust-side ownership shape,
  not C ABI, and have their own typing concerns.
- **Per-element callbacks the wrapper synthesises itself** (e.g. a
  generic `Drop` glue function the wrapper builds from a Rust method
  via a trampoline). These do not need a trait constant at all.

### 12.4 Naming convention

- `push_owned` / `pop_owned` / `insert_owned` / `delete_owned` —
  on the owning impl block; signature uses the associated `Owned`
  handle.
- `push_borrowed` / `pop_borrowed` / `insert_borrowed` /
  `delete_borrowed` — on the borrowing impl block; signature uses
  `&'static T`.

The shared read methods (`get`, `find`, `len`, `is_empty`, `as_ptr`,
`from_ptr`, `new`, `Drop`) live on the unbounded `impl<T: BaseTrait>
Container<T>` block — they work identically for both ownership
flavours.

### 12.5 Lifetime scope choice for the borrowing variant

`Container<T>` deliberately carries no lifetime parameter — it is
`#[repr(transparent)]` over the C struct and embeds nowhere visible to
the type system.  A signature `fn push_borrowed<'a>(&'a self, item: &'a T)`
would constrain `'a` only to the call duration: after `push` returns
the C container still holds the pointer, but Rust no longer tracks
any relationship to the element's lifetime, and the next operation
(`pop_borrowed`, `get`) cannot produce a borrow that the type system
verifies.

Two sound options remain:

- `&'static T` — current choice.  Works whenever the borrowed
  elements are file-static / lifetime-`'static` in C, which is the
  case for every `BorrowingStackElement` in libssl today.
- Parameterise the container: `Container<'a, T>` with
  `PhantomData<&'a T>`, push/get/pop returning `&'a T`.  Fully
  general for non-static borrow scopes; costs a lifetime parameter
  on every accessor signature, every embedding, every C-side
  conversion site.  Reserve this for when a real non-static
  borrowed-element case appears; the upgrade is mechanical (add
  `<'a>` to the container, replace `'static` with `'a` in the
  borrowing impl, thread `'a` through accessors).

### 12.6 libssl realisation

The pattern is instantiated in
[`rust/crypto/src/stack/stack_of.rs`](../rust/crypto/src/stack/stack_of.rs)
as `StackOf<T>` over `OPENSSL_STACK`:

| Trait | Role | Where |
|---|---|---|
| `StackElement` | base; declares `FREE_FN` (Option<C-side destructor>) | `stack_of.rs` |
| `OwningStackElement` | owning marker; `type Owned: COwnable` | `stack_of.rs` |
| `BorrowingStackElement` | borrowing marker | `stack_of.rs` |

**Note:** the live `StackElement` predates the typed-`Raw` rule
above and declares `FREE_FN` as `Option<unsafe extern "C" fn(*mut
c_void)>`, marking the trait `unsafe trait` to compensate. Per-impl
sites pay the `unsafe impl` + `mem::transmute` boilerplate cost.
This is the conservative legacy design; new generic-container
wrappers emitted by the wrap stage should use the typed-`Raw` form
above. A future refactor will migrate `StackElement` itself.

Per-type impls live on the wrapper type's module:

- `X509`: `OwningStackElement<Owned = CArc<X509>>` (refcounted).
- `X509Name`: `OwningStackElement<Owned = CBox<X509Name>>` (single-owner).
- `SslCipher`, `SrtpProtectionProfile`: `BorrowingStackElement`
  (file-static tables, `&'static` correctly captures their lifetime).

No `unsafe fn push_raw` / `pop_raw` escape hatches.  Stacks of
as-yet-unwrapped C types use `ffi::OPENSSL_sk_*` directly, which is
both more visible in code review and a discipline lever for "this
type needs a wrapper before the port keeps growing."

### 12.7 When this pattern applies

Use the marker subtrait split whenever:

1. A single C collection container is parameterised by element type
   only at the macro / type-alias level, with no in-container
   indication of element ownership.
2. The element types split into two camps — those whose Rust
   wrapper has a `CArc<W>` / `CBox<W>` owned handle, and those
   without (typically static-table entries with no destructor).
3. The container needs safe typed mutation, not just read access.

If only read access is exposed (no `push` / `pop` / `insert` /
`delete`), the split is unnecessary — a single base trait and the
generic `impl<T: BaseTrait>` is enough.  If the C container has a
single uniform ownership model (always owning, or always borrowing,
across all element types), the split is also unnecessary — collapse to
one trait.
