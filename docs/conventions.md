# Crustify coding conventions

These are the shared, mechanical contracts between the orchestrator,
scheduler and translator. The playbooks contain the decisions and procedures.

## Rust baseline

Crustify crates use Rust edition 2024. Wrapper crates inherit workspace lints
that deny `clippy::undocumented_unsafe_blocks` and allow
`clippy::module_inception`; generated `-sys` code is exempt.

Every unsafe block carries a specific, falsifiable `// SAFETY:` comment. Native
Rust APIs are safe unless their caller obligation cannot be expressed in the
type system.

## Crates and modules

`crustify/crates.json` is the source of truth for an entity's Rust home. Each
library wrapper crate has a companion `<lib>-sys` crate for raw bindings.

There is one `.rs` home per C translation unit, or per header group when no
translation unit owns the entity. Entities sharing a definition site co-home.
A home is shared across waves; completed items remain in place.

## Wrapped types

The canonical wrapped surface has three types:

| type | role |
|---|---|
| `Foo` | layout-compatible newtype used for embedding and owned storage |
| `FooRef<'a>` | copyable shared borrowed handle with getters |
| `FooMut<'a>` | exclusive borrowed handle with setters and shared reborrows |

Owning handles produce `FooRef` and `FooMut` through `as_ref()` and `as_mut()`;
they do not dereference to `Foo`. Layout access starts from
`FooRef::as_ptr()` or `FooMut::as_mut_ptr()`.

## Functions and FFI names

The raw binding for a C function lives under `ffi::<name>`. Its safe wrapper
uses the bare function name in the owning module. When one C function has
several valid ownership contracts, give each safe variant a distinct,
descriptive name.

A callable macro shim is named `crustify_<NAME>`. Constant macros remain
generated constants in the `-sys` crate.

Each ported C source file has one `mod ffi_export { use super::*; ... }` raw ABI
gateway in its Rust module. Its exports use `#[unsafe(no_mangle)] extern "C"`.
Export externally visible and header-inline symbols under their bare names.
Export static functions, TU-inline functions and static globals as
`crustify_<file>__<name>`.

The per-file C/Rust build switch is `CRUSTIFY_<FILE>`, with `<FILE>` derived by
sanitizing the translation unit's path.

## Anchors

The scheduler inserts one TODO anchor for each scheduled item:

| anchor | meaning |
|---|---|
| `// crustify:todo: <name>` | unfilled function, global, type or callback |
| `// crustify:todo: <name>.<field>` | unfilled owner-qualified field |

The translator replaces it with exactly one filled doc-comment anchor:

| anchor | meaning |
|---|---|
| `/// Wraps: <name>{.<field>}` | safe view over the FFI seam |
| `/// Replaces: <name>{.<field>}` | native Rust translation |
| `/// Field: <name>.<field>` | field accessor over a wrapped type |

`Field:` applies to a field only. It is what a field accessor emitted on a
wrapped type carries, and campaign coverage counts distinct `type.field` paths
that reached one.

The TODO does not survive beside the filled anchor. A surviving TODO is open
work. Duplicate a filled anchor only when several wrappers intentionally
represent the same item. Existing filled anchors are completed work unless the
current objective deliberately promotes that item.
