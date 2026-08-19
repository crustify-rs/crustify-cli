# Crustify Principles for Migrating C to Rust

Crustify translator agents must follow these to guide their work.

---

## Core translation philosophy

Types stay layout-compatible with C, wrapped in safe abstraction so that Rust
consumers can use them safely with Rust-native features (RAII, lifetimes, bounds
checking, type-safety) without introducing undefined behavior hazards. FFI
functions and callbacks are wrapped likewise, with signatures that use the safe
type wrappers. Unsafe footprint reduced to a minimal, auditable surface: field
accessors on the type handles, and safe FFI wrappers for making FFI calls.

---

## Scope policy

Every item sits in one of two sections of `scope.json`, decided by
`scope-config.json`'s two file sets — `impl_files` (what implements the
library) and `api_headers` (what publishes its API) — under its
`campaign_objective`:

- On a **`port`** campaign, `impl_files` + `api_headers` name the code this
  campaign owns. An item is **targeted** when its body lives in one of those
  files, or, having no body anywhere, when all its declarations do. Everything
  targeted code reaches that is not named there is **imported**.
- On a **`wrap`** campaign the campaign owns nothing: `api_headers`'
  declarations seed the imported section directly, and nothing is targeted.

**Imported** items stay C's: a safe wrapper over the FFI seam, layout
compatible, with storage allocation and free still owned by C.

**Targeted** items are on their way to native Rust. They start
layout-compatible and wrapped, get opacified once the C side no longer reads
their fields, and end fully nativized — allocation and free owned by Rust.

The section says which trajectory an item is on; `--objective` says what to do
with it this wave, and it is handed to you.

Rust consumers use the safe type's API, never raw pointers or `unsafe` blocks.

---

## Representation

### The three types

Based on primitives from `ffibox`.

`define_ctype!(Foo, FooRef, FooMut, ffi::foo_st)` emits, per wrapped C type:

| Type | Size | Role |
|---|---|---|
| `Foo` | the C struct's | layout; embeds by value in a `#[repr(C)]` mirror; what an owning handle points at |
| `FooRef<'a>` | one pointer, `Copy` | shared borrow; **getters**, `&self` |
| `FooMut<'a>` | one pointer | exclusive borrow; derefs to `FooRef`; **setters**, `&mut self` |

No aliasing is asserted on the bytes of `Foo`, since C may reach them — no
`&Foo` / `&mut Foo` is taken, in a signature or a body. The handles wrap a
pointer, so a reference to one covers Rust-owned storage: `&mut FooMut` is
ordinary, and reborrows implicitly.

Owning handles (`CBox<Foo>`, `CVal<Foo>`, `CBoxWith<Foo, D>`) yield handles via
`as_ref()` / `as_mut()`, not `Deref`.

### Field access

Project fields with `addr_of!` / `addr_of_mut!` (equivalently `&raw const` /
`&raw mut`), which yield raw pointers without forming a reference to the field,
and never take `&(*p).field` / `&mut (*p).field`. A field-level reference is a
reference over memory C may write — the same rule that keeps `&Foo` out, applied
one level down. It is also the only form that is sound on a field C has not yet
written: naming the place loads nothing, where reading it would produce an
invalid value.

The pointer comes from the handle: reads off `FooRef::as_ptr` (`*const`), writes
off `FooMut::as_mut_ptr` (`*mut`).

### Accessor contract

Every in-scope field that is an owned reference gets at least:

- a setter on `FooMut` that moves ownership into `self`, dropping the old
  reference and setting the new one;

- two getters: one on `FooMut` that transfers ownership out of `self`, leaving
  the field valid, and one on `FooRef` that borrows it as the field type's own
  `TRef<'_>` handle;

- a field embedded by value gets a projecting getter returning `TRef<'_>` /
  `TMut<'_>` over `addr_of!` / `addr_of_mut!`; the caller reads through `T`'s own
  handle accessors.

A field may have multiple accessor variants, depending on its type resolution,
ownership semantics, cardinality, etc.

---

## Functions, callbacks, and inline function pointers

 Wrap-scope functions, callbacks, and inline function pointers get one or more safe
  wrappers that serialize wrapped references before calling their `ffi::` variant,
  and deserialize results back to safe wrappers upon return.
 
 Port-scope ones are translated to native Rust and call the safe API when
 needing FFI dependencies.

---

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

---

## Ownership analysis

To leverage idiomatic Rust features, we express each pointer argument,
return, field, and variable based on ownership facets submitted through the
`crustify-oracle` skill via the smart pointers and traits from the
`ffibox` skill.

### Footprint

For struct fields, pointer args and return (both functions and function pointers)
analysis is codebase-wide, unscoped, to catch the complete usage footprint of the
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

---

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
    (1) inside `impl FooRef` / `impl FooMut` blocks, for reaching a wrapped
    type's state through its accessors; you never access a type's fields outside
    the accessors, you use the accessors instead.
    
    (2) calling an `ffi::` routine inside its own safe wrapper or inside Rust-native
    functions if the `ffi::` routine does not have a safe wrapper yet (e.g. SCC cut cycles
    of the DAG).

    (3) calling Rust-native functions declared unsafe. The allowed cases are:
    - calling an unsafe setter that passes a borrowed reference, which requires
    declaring the setter unsafe;
    - calling `CBoxWith::into_box` to promote a construction-phase handle, held
    under a storage-only dropper, to the formed `CBox`.
 
- **Inner-module `raw pointers` are ONLY allowed in the following cases:**
    (1) the above scenarios where `unsafe` blocks are allowed.

- **Every `unsafe` block** carries a specific, falsifiable `// SAFETY:` stating the
    safety contract and discipline.

---

## File contract (file-grained - load-bearing)

One Rust module per C source file, shared across batches. Find the module for a
target or a dependency (type / callback / symbol) with `crustify-cli scaffold`,
which reflects the established placement policy.

Each item you own arrives as one todo line, laid in your own worktree:

- `// crustify:todo: <C_ITEM>` for a function / global / type
- `// crustify:todo: <C_ITEM>.<field>` for that type's `<field>` accessor

A field item is always owner-qualified: a file-grained module holds many types,
and two of them with a `data` field would otherwise collide on one line.

The module may also hold `///` anchors filled by earlier waves. Those are done -
read them for context, never rewrite them.

The fill contract, per item:

- locate the item by its `// crustify:todo: <C_ITEM>{.<field>}` line;
- REPLACE that line with a doc comment on the item you emit. You pick the verb
  from what you did: `/// Wraps: <C_ITEM>{.<field>}` for a safe view over the
  FFI seam, `/// Replaces: <C_ITEM>{.<field>}` for a native Rust translation;
- the todo line does not survive alongside the doc comment. A surviving
  `crustify:todo` is the only record that an item is still open.

## Re-export ported symbols

**`#[unsafe(no_mangle)]` re-export**: we write one for each ported symbol into **that
file's `mod ffi_export { use super::*; ... }`** submodule (the **raw C-ABI
gateway**; create it once per file); the export carries the C signature (from
the record's entries + the C source), **reconstructs the wrappers from the raw
params, and delegates to the idiomatic `pub(crate) fn`** (its `super::`
sibling). The re-export uses the following guideline for naming:
  - `function_exported` / `global_extern` / `function_inline_header` -> export under
    the **bare name**;
  - `function_static` / `function_inline_tu` / `global_static` -> export under the
    unique **`crustify_<file>__<name>`** symbol (the TU-local collision-safe form).

## C/Rust build switch

We wire the build switch (per-file feature flags). The variant is selected **per C
file** at compile time, via the `CRUSTIFY_<FILE>` guard macro (path-sanitised
`defined_in`):

a. **C side** - we fence each ported body with `#ifndef CRUSTIFY_<FILE>` by
   **reading the C source** to find each ported symbol's extents. We use **tight
   blocks** around adjacent ported functions - never a single
   file-level wrap. In the `#else` branch, emit each ported symbol's re-export:
   an `extern` declaration for every kind, plus a `#define <name>
   crustify_<file>__<name>` redirect for the TU-local kinds.

b. **Build wiring** - we make `CRUSTIFY_<FILE>` definable from the build. Emit
  (if it's not emitted already) a build config and wire it into the
   project's build (the `build.json` `build_commands` / configure / link
   pipeline), so defining the file's flag (i) compiles the owning library's Rust
   port-crate staticlib (the one carrying the re-exports - the crate the file's
   module lives under), (ii) adds that crate's archive to
   the C link line, and (iii) injects `-DCRUSTIFY_<FILE>`.
   Link pipelines differ per build system - inspect the actual scripts.