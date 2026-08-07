# AGENTS.md — crustify C→Rust port: always-on principles

The non-negotiable core every porting/wrapping agent must hold in context at all
times.

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

 Both **port- and wrap-scope** types stay layout-compatible with C until they can be
 opacified, which is mainly applicable to port-scope structs. The types that must stay
 interoperable with C
 have their definition wrapped in newtypes and field accessors placed in `impl` blocks
 on the type.
 
 Rust consumers of types use the safe type's API instead of raw pointers or
 `unsafe` blocks.

**Pointer fields.** Every in-scope field that is an owned reference, gets at least:
 
 - a setter that moves ownership into `self`, dropping the old reference
   and setting the new one using `addr_of_mut!(...)`
 
 - two getters: one which transfers ownership out from `self`, leaving
   the field valid, and one which borrows the field's shared reference `&T`.
 
 - fields that are embedded by value get a borrow projecting
   getter `&T` over `addr_of(...)`; the caller reads through
   `T`'s own `self` accessors.

   A field may have multiple accessor variants, depending on its type resolution,
   ownership semantics, cardinality, etc.

## Functions, callbacks, and inline function pointers

 **Wrap-scope** functions, callbacks, and inline function pointers get one or more safe
  wrappers that serialize wrapped references before calling their `ffi::` variant,
  and deserialize results back to safe wrappers upon return.
 
 **Port-scope** ones are translated to native Rust and call the safe API when
 needing FFI dependencies.

## Macros

We DO NOT port/wrap C macros in native Rust. When a body you port/wrap uses one,
resolve it at the call site:

- **Macro that aliases a symbol**: check the macro's definition in the codebase
and extract the underlying symbol(s) it expands to; bindgen already created a
binding for the underlying symbol(s), so it shows up as an ordinary dep - call
its safe wrapper (it is very likely already a dep of what you're porting).

- **Function-like macro with no wrapper**: look for a
`crustify_<NAME>(<ARG_DECLS>)` shim in `ffi::` - bindgen may have emitted one.
Call it across the FFI seam like any other not-yet-ported C primitive. If the
shim is absent, then emit it yourself and rerun bindgen. Then implement a safe
wrapper for it like for regular functions, emitting an anchor for it according
to our conventions.

- **Constant macro**: use the `ffi::` binding directly.

## Pointers

To leverage idiomatic Rust features, we express each pointer argument,
return, field, and variable based on the ownership facets provided by the
`crustify-oracle` skill via the smart pointers and traits from the
`crustify-prim` skill.

## Safety discipline

### `unsafe` blocks and raw pointers

They are confined to the few roles below; everywhere else is idiomatic,
fully-checked Rust. This is load-bearing - the steps assume it.

- **The per-file `mod ffi_export` is the only raw C-ABI gateway.**
    Each ported file's re-exports live in a `mod ffi_export { use super::*; ...
    }` submodule in that same file, by the functions they export. A raw C
    signature (`*mut`/`*const ffi::T`, out-pointers) appears only inside
    `mod ffi_export`, in the `#[no_mangle] extern "C"` re-export. That boundary
    reconstructs the safe wrappers from the raw params and then calls the
    idiomatic `pub(crate) fn` (its `super::` sibling).
    
- **Inner-module `unsafe` blocks are ONLY allowed in the following cases:**
    (1) inside `impl T` blocks for reaching a wrapped type's state through its
    accessors (the accessors own the `addr_of!` / `addr_of_mut!`); you never
    access a types field's outside the accessors, you use the accessors instead.
    
    (2) calling an `ffi::` routine inside its own safe wrapper or inside Rust-native
    functions if the `ffi::` routine does not have a safe wrapper yet (e.g. SCC cut cycles
    of the DAG).

    (3) calling Rust-native functions declared unsafe. The allowed cases are:
    - calling an unsafe setter that passess a borrowed reference, which requires
    declaring the setter unsafe, is also allowed.
    - calling `assume_init` to promote a partially initialized reference to a
    fully initialized one.
 
- **Inner-module `raw pointers` are ONLY allowed in the following cases:**
    (1) the above scenarios where `unsafe` blocks are allowed.

- **Every `unsafe` block** carries a specific, falsifiable `// SAFETY:` stating the
    safety contract and discipline.

### Reference borrows

- Never instantiate a `&mut` to a wrapped type (in a function's signature
or body). Always write through `&self` setters - the principle of interior 
mutability.

### Field accesses

Always read and write through `addr_of!` / `addr_of_mut!`, never through a bare
`(*ptr).field` place expression. These are the only forms permitted for field access through a raw
pointer. The constructs that synthesise a borrow are forbidden.

| Construct | Synthesises a borrow? | Use? |
|---|---|---|
| `addr_of!((*ptr).field).read()` | **No** — pointer to place + byte-copy load | ✅ **mandatory read form** |
| `addr_of_mut!((*ptr).field).write(v)` | **No** — pointer to place + byte-copy store | ✅ **mandatory write form** |
| `addr_of!((*ptr).field)` / `addr_of_mut!((*ptr).field)` | **No** | ✅ for taking inner references |

## File contract (file-grained - load-bearing)

The `.rs` module for each target and dependency (types/symbols) is found via the
`crustify-cli scaffold` command, which reflects the pre-established item placement policy.

Each module is a shared, file-grained module - one Rust module per C source
file, holding the following anchor kinds:
- `// Replaces: <C_ITEM>` for port-scope items (functions / globals / types)
- `// Wraps: <C_ITEM>` for wrap-scope items
- `// Field: <C_ITEM>.<field>` for the `<C_ITEM>` type's `<field>` accessors

Each module includes anchors for many elements at once (across batches,
wrap *and* port). Any one agent owns only the anchors in its workset; the rest
belong to other batches and the other stage. Each anchor may be followed by
a `// crustify:todo` placeholder marking remaining work, which gets deleted
once the target is processed.

The per-anchor fill contract:

- a target is located by its `// <Anchor>:` item anchor;
- an assigned anchor is filled in place, every other left exactly as-is;
- a filled anchor's `// <Anchor>: <C_ITEM>` line is promoted to a
  `/// <Anchor>: <C_ITEM>{.<field>}` doc comment on the emitted item, and its
  `// crustify:todo` is deleted (a surviving todo marks still-pending work).

## Ownership analyis

For struct fields, pointer args and return (both functions and function pointers).
Analysis is codebase-wide, unscoped, to catch the complete usage footprint of the
anlyzed item. For ptr args, walk down the call graph of the target function and
identify paths that free the arg.

### Decision support

Three independent signals; use whichever is decisive:

  1. **Documentation / API contract.** Headers and reference docs often
     spell out ownership, lifetime, and buffer contracts directly.
  2. **Body + callers + name patterns.** What the body does to each
     pointer (frees, stores in a field, just reads); what representative
     callers do after the call. Name patterns are hints, never
     authoritative.
  3. **CodeQL** against the given db for dataflow / pointer-provenance
     on non-obvious cases. If you find a reusable gap, save the query
     under `utils/codeql/` and flag it.