# AGENTS.md — crustify C→Rust port: always-on principles

The non-negotiable core every porting/wrapping agent must hold in context at all
times — kept deliberately short, because this is the always-loaded tier (inlined
into every agent prompt; detail and worked cases live in the separate reference).

## Skills

Reusable how-to guides for recurring decisions, loaded alongside these
principles. If a skill's `description` below matches what you're doing, **read
that skill's file in full** before proceeding - the description is the routing
signal; the body is the procedure.

<!-- SKILLS_INDEX -->

## Core translation philosophy

Based on interior mutability: types stay layout-compatible with C, wrapped in
safe abstraction so that Rust consumers can use them without trading safety.
FFI functions and callbacks are also wrapped in safe function wrappers with
signatures that use the safe type wrappers. Unsafe footprint reduced to a
minimal, auditable surface inside type implementations for accessing raw fields
and safe FFI function wrappers for making FFI calls.

## Types

 Both **port- and wrap-scope** types stay layout-compatible with C, having their
 definition and field accessors placed in `impl` blocks on the type.
 
 Lifecycle primitives of **port-scope** types are translated as free functions
 to native Rust. Field accessors and the type's definition stay wrapped. 
 
 Rust consumers of types use the safe type's API instead of raw pointers or
 `unsafe` blocks.

## Functions and callbacks

 **Wrap-scope** functions and callbacks get a safe wrapper that serialize wrapped
 references before calling their `ffi::` variant, and deserialize results back to
 safe wrappers upon return.
 
 **Port-scope** functions and callbacks are translated to native Rust and call
 the safe API when needing FFI dependencies.

## Macros

We never port/wrap C macros in native Rust. When a body you port/wrap uses one,
resolve it **at the call site**:

- **Macro that aliases a symbol**: check the macro's definition in the codebase
and extract the underlying symbol(s) it expands to; bindgen already created a
binding for the underlying symbol(s), so it shows up as an ordinary dep - `query
syms --name <sym>` and call its **safe wrapper** (it is very likely already a
dep of what you're porting).

- **Function-like macro with no wrapper**: look for a
`crustify_<NAME>(<ARG_DECLS>)` shim in `ffi::` - bindgen may have emitted one.
Call it across the FFI seam like any other not-yet-ported C primitive.

- **Constant macro**: use the `ffi::` binding directly.

## Pointers

Read **every** pointer argument, return, and field from the ownership facets in
its symbol/type record, then pick the safe form below. Authoritative facet
*definitions* live in the schema (`query types --schema`); *which* owning wrapper
to use is the **crustify-c-pointer-primitives** skill's call (see Skills). This
table is the facet→form quick-reference.

| Facet | Rust form | Primitive |
|---|---|---|
| **owned + exclusive** (sole owner; plain `*_free`) | owning wrapper **by value** | `CBox` |
| **owned + shared** (refcounted; pointee has `up_ref`) | owning wrapper **by value** | `CArc` |
| **storage but not fully formed** (porting a ctor: allocate, then init in place) | uninit ladder; graduate once formed (`CFreedUninit` frees storage on failure) | `CBoxUninit`→`CBox` / `CUniqueArcUninit`→`CArc` |
| **embedded by-value** (no separate storage; `*_dispose`/`*_cleanup` frees fields) | by value; borrowed view → guard | `CVal` / `CValGuard` |
| **type-erased owned storage** (opaque `void*` you own) | owning wrapper **by value** | `COwn` |
| **borrowed** (non-owning) | `&Wrapper`, by `lifetime`: `self`→enclosing struct, `field:<n>`→sibling, `static`→global, `other`→ladder | `&Wrapper` / `SelfPtr` |
| **mutable / const** | always `&Wrapper`, **never `&mut`** (interior mutability; `const` ⇒ read-only) | `&Wrapper` |
| **array** (buffer + length) | `&[T]` if not moved; by value if moved | `&[T]` / `CVec` |
| **container** (collection of element pointers) | collection; `owned_elem` ⇒ owns & frees elements, else borrows | `CVec` |
| **string** (NUL-terminated) | `&CStr` if not moved; owned family if moved | `&CStr` / NUL-string family |
| **out-parameter** (callee writes `T**`) | the write-slot | `COut<form>` |
| **nullable** | wrap the chosen form | `Option<…>` |
| **scalar** | by value | — |

## Safety discipline

`unsafe` and raw pointers are confined to the few roles below; **everywhere else
is idiomatic, fully-checked Rust**. This is load-bearing - the steps assume it.

- **The per-file `mod ffi_export` is the *only* raw C-ABI gateway.**
    Each ported file's re-exports live in a `mod ffi_export { use super::*; ...
    }` submodule **in that same file**, by the functions they export. A raw C
    signature (`*mut`/`*const ffi::T`, out-pointers) appears **only** inside
    `mod ffi_export`, in the `#[no_mangle] extern "C"` re-export. That boundary
    **reconstructs the safe wrappers** from the raw params and then **calls the
    idiomatic `pub(crate) fn`** (its `super::` sibling).
    
- **Inner-module `unsafe` blocks are ONLY allowed in the following cases:**
    (1) inside `impl T` blocks for reaching a wrapped type's state **through its
    accessors** (the accessors own the `addr_of!` / `addr_of_mut!`); you **never**
    access a types field's outside the accessors, you use the accessors instead.
    
    (2) manual memory management for types / pointers whose lifetime / ownership
    semantics **cannot be expressed** through the smart pointers and traits from
    `crustify-crate`.
    
    (3) calling an `ffi::` routine inside its own safe wrapper or inside Rust-native
    functions if the `ffi::` routine does not have a safe wrapper yet. Known routines
    for which we don't emit wrappers yet:
    - system / external.
    
    (4) indirect calls through function pointers that do not have a safe callback
    wrapper.

    (5) calling Rust-native functions declared unsafe. The allowed cases are:
    - calling an unsafe setter that passess a borrowed reference, which requires
    declaring the setter unsafe, is also allowed.
    - calling `assume_init` to promote a partially initialized reference to a
    fully initialized one.
 
- **Inner-module `raw pointers` are ONLY allowed in the following cases:**
    (1) the above scenarios where `unsafe` blocks are allowed.
    
    (2) a pointer that is both owned and borrowed depending on runtime state (no
    single wrapper expresses both).
    
    (3) an out-param address helper (taking the address of a field to pass as an
    out-pointer).
    
    (4) an intrusive-list sibling link the smart pointers cannot yet model.

- **Never** instantiate a `&mut` to a wrapped type (in a function's signature or body).
    **Always** write through `&self` setters - the principle of interior mutability.

- **Every `unsafe` block** carries a specific, falsifiable `// SAFETY:` stating the
    safety contract and discipline.

## File contract (file-grained - load-bearing)

**Locate your files, then fill.** Find each target's `.rs` module via the
`crustify scaffold` command, homing its anchor - each symbol you wrap/port (each
in the file it lived in), a dep's module, a type's already-wrapped module.

Each module is a **shared, file-grained module** - one Rust module per C source
file, holding `// Replaces:` item anchors (yours: functions / globals) alongside
wrap's `// Field:` / `// Alias:` anchors, for **many** elements (yours *and* other
batches', wrap *and* port). Focus on those assigned to your workset.

- **Locate** each target by its `// Replaces:` **item** anchor.

- **Fill** your assigned anchors and leave every other exactly as-is.

- **Promote** its `// Replaces:` line to a `/// Replaces: <C_FN> (<file>.c)`  doc comment on the
  item you emit and **delete that anchor's `// crustify:todo`** (a surviving todo
  = still pending).