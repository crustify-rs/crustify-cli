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
 definition wrapped in newtypes and field accessors placed in `impl` blocks on the type.
 
 Lifecycle primitives of **port-scope** types are translated as free functions
 to native Rust. Field accessors and the type's definition stay behind wrappers
 and accessors. 
 
 Rust consumers of types use the safe type's API instead of raw pointers or
 `unsafe` blocks.

**Pointer fields.** Every field that is an owned reference, gets:
 
 - a setter that moves ownership into `self`, drops the old reference
   and sets the new one using `addr_of_mut!(...)`
 
 - two getters: one which transfers ownership out from `self`, leaving the field valid,
   and one which borrows the field's shared reference `&T`.
 
 - fields that are embedded by value get a borrow projecting
   getter `&T` over `addr_of(...)`; the caller reads through
   `T`'s own `self` accessors. 

## Functions and callbacks

 **Wrap-scope** functions and callbacks get a safe wrapper that serialize wrapped
 references before calling their `ffi::` variant, and deserialize results back to
 safe wrappers upon return.
 
 **Port-scope** functions and callbacks are translated to native Rust and call
 the safe API when needing FFI dependencies.

## Macros

We DO NOT port/wrap C macros in native Rust. When a body you port/wrap uses one,
resolve it **at the call site**:

- **Macro that aliases a symbol**: check the macro's definition in the codebase
and extract the underlying symbol(s) it expands to; bindgen already created a
binding for the underlying symbol(s), so it shows up as an ordinary dep - call
its **safe wrapper** (it is very likely already a dep of what you're porting).

- **Function-like macro with no wrapper**: look for a
`crustify_<NAME>(<ARG_DECLS>)` shim in `ffi::` - bindgen may have emitted one.
Call it across the FFI seam like any other not-yet-ported C primitive.

- **Constant macro**: use the `ffi::` binding directly.

## Pointers

To leverage idiomatic Rust features, we express each pointer argument,
return, field, and variable based on the ownership facets provided by the
`crustify-oracle` skill via the smart pointers and traits from the `crustify-wrap-crate`
skill. Use `crustify-oracle` to determine the various properties of a C pointer (ownership,
singleton vs. array, typed vs. type-erased, nullable, mutable, etc.) and associate it with
the appropriate smart pointer from `crustify-wrap-crate`.  

## Safety discipline

### `unsafe` blocks and raw pointers

They are confined to the few roles below; **everywhere else is idiomatic,
fully-checked Rust**. This is load-bearing - the steps assume it.

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
    
    (2) an out-param address helper (taking the address of a field to pass as an
    out-pointer).
    
    (3) an intrusive-list sibling link the smart pointers cannot yet model.

- **Every `unsafe` block** carries a specific, falsifiable `// SAFETY:` stating the
    safety contract and discipline.

### Reference borrows

- **Never** instantiate a `&mut` to a wrapped type (in a function's signature or body).
    **Always** write through `&self` setters - the principle of interior mutability.

### Field accesses

Always read and write through `addr_of!` / `addr_of_mut!`, never through a bare
`(*ptr).field` place expression. These are the *only* forms permitted for field access through a raw
pointer. The constructs that synthesise a borrow are forbidden.

| Construct | Synthesises a borrow? | Use? |
|---|---|---|
| `addr_of!((*ptr).field).read()` | **No** — pointer to place + byte-copy load | ✅ **mandatory read form** |
| `addr_of_mut!((*ptr).field).write(v)` | **No** — pointer to place + byte-copy store | ✅ **mandatory write form** |
| `addr_of!((*ptr).field)` / `addr_of_mut!((*ptr).field)` | **No** | ✅ for taking inner references |

## File contract (file-grained - load-bearing)

The `.rs` module for each target and dependency (types/symbols) is found via the
`crustify scaffold` command, which reflects the pre-established item placement policy.

Each module is a **shared, file-grained module** - one Rust module per C source
file, holding the following anchor kinds:
- `// Replaces:` for port-scope items (functions / globals / types)
- `// Wraps:` for wrap-scope items
- `// Field:` for a type's field accessors
- `// Alias:` for typed-array aliases

Each module includes anchors for **many** elements at once (across batches,
wrap *and* port). Any one agent owns only the anchors in its workset; the rest
belong to other batches and the other stage. Each anchor may be followed by
a `// crustify:todo` placeholder marking remaining work, which gets deleted
once the target is processed.

The per-anchor fill contract:

- a target is **located** by its `// <Anchor>:` **item** anchor;
- an assigned anchor is **filled** in place, every other left exactly as-is;
- a filled anchor's `// <Anchor>:` line is **promoted** to a
  `/// <Anchor>: <C_ITEM> (<file>.c/.h)` doc comment on the emitted item, and its
  `// crustify:todo` is **deleted** (a surviving todo marks still-pending work).
