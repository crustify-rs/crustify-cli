# crates.json schema

Field meaning for `<repo_root>/crustify/crates.json` — the whole-repo
crate/module decomposition and the placement oracle. Layout example:
[`specs/crates.json`](../../specs/crates.json).

Target-agnostic and cumulative: "which unique Rust `.rs` homes this C entity",
independent of porting progress. Port/wrap status and per-target scope live in
`scope.json`, never here.

## crates.\<name\>

One entry per link unit, keyed by crate name. Crate names ARE the link-unit
keys — they match `build.json`'s `libraries` or `executables`, and bindgen uses them as the
library identity.

| field | meaning |
|---|---|
| `kind` | `library` (→ staticlib/cdylib) or `executable` (→ bin) |
| `in_tree` | is the library's SOURCE in this repo? Provenance only — gates nothing. |
| `crate_path` | repo-relative path of the wrapper crate |
| `sys_crate` | repo-relative path of the FFI companion. Present for every library with bound entities |
| `depends_on` | inter-crate edges, from `build.json` `link_dependencies`. A DAG — a cycle between two crates is an error |
| `modules` | `{}` in a freshly seeded shell |

## crates.\<name\>.modules.\<name\>

Logical subsystems within a crate. A module is not a directory — it may span
several, and it is what makes a TU and its public header one unit.

| field | meaning |
|---|---|
| `rust_path` | the module's ROOT dir under `crate_path`. Every one of its `rs` keys must start with it |
| `rs` | `.rs` file → its entry. The single source of truth: the module's source and header surface are DERIVED from these, never stored |

`rust_path` is the module's root, not any one file's directory — a module that
spans several source dirs still has ONE root, and its `.rs` may nest below it:

```
core     rust_path: src           src/ssl_lib.rs            # top-level ssl/*.c
record   rust_path: src/record    src/record/record.rs
                                  src/record/methods/tls_common.rs
```

Boundary headers = the union of the modules' `headers`, zoned by path to Rust
visibility: `*_local.h` → `pub(crate)`; `include/internal/*` → `pub` to sibling
crates; `include/openssl/*` → published API.

## crates.\<name\>.modules.\<name\>.rs.\<path\>

Keyed by `.rs` path under `crate_path`. One entry per stem-group.

| field | meaning |
|---|---|
| `tu` | the ONE translation unit (`.c`) this module mirrors, or `null` |
| `headers` | headers through which its members are declared or defined |
| `members` | entities homed here, by kind, as BARE NAMES |

`tu` is scalar — one TU per `.rs` — which is what lets a same-stem `foo.c` and
`foo.h` be one Rust module rather than colliding. `headers` is many-to-many: one
header is listed by every `.rs` whose members it declares.

`tu: null` covers every entity with no in-tree definition site, uniformly:
callbacks, externs, opaque/forward-declared structs, and all entities of an
out-of-tree library. Those are placed by `headers`.

Provenance lives once on the `.rs`, not per member. `members` is authoritative
for placement; `tu`/`headers` disambiguate.

### members

| bucket | anchored? | note |
|---|---|---|
| `functions` | yes | |
| `globals` | yes | |
| `types` | yes | plus one owner-qualified anchor per field |
| `callbacks` | yes | function-pointer typedefs; anchored like any other member |
| `macros` | **no** | homed for library attribution only |

Macros are the one deliberate exclusion from porting and wrapping: their whole
surface belongs to the `-sys` crate — a `crustify_<NAME>` shim for a callable
one, a `pub const` for a value one — and the C `#define` stays. They are listed
so bindgen can resolve which library owns each (it puts them in that crate's
`ALLOWED_MACROS`); no `.rs` anchor is ever laid for them.

**Anonymous types are never members.** CodeQL names every anonymous
struct/union/enum with one synthetic placeholder (`(unnamed enum)`,
`(unnamed class/struct/union)`), so dozens of distinct definitions collide on a
single string that nothing can reference. They are filtered before `scope.json`,
so they never reach here. Their contents are not lost: `entities/fields.ql`
flattens an anonymous member into its named parent under a qualified field name
(`asn1_type_st` gets `value.asn1_string`), and that parent is a member as usual.

## placement

```
INPUT   entity = (name, kind, defined_in, declared_in)
OUTPUT  exactly one .rs

1. CRATE  = the link unit owning it                 (build.json `libraries` or `executables`)
2. MODULE = the subsystem within that crate         (from defined_in/declared_in)
3. RS     = defined_in ? <stem(defined_in)>.rs      # .c and .h treated alike
                       : <stem(best-fit declared_in)>.rs
4. KEY    = (name, defined_in) | (name, declared_in) when defined_in is null
```

A header does not decide its own crate: `include/` sits in the `include_dirs`
of several libraries and in the `source_dirs` of none, so step 1 has no path to
match. Resolve in order:

1. **stem-partner** — the header shares a stem with a TU
   (`include/internal/quic_ackm.h` ↔ `ssl/quic/quic_ackm.c`). Take that TU's
   crate and module, and co-home both in one `.rs`.
2. **section** — no partner: target-section → the crate that owns it,
   import-section → the crate that defines the entities it declares.

An orphan header (no stem-partner, e.g. `include/openssl/types.h`) takes a
module named for its own stem, so it stays one `.rs` rather than being folded
into an unrelated subsystem.

Invariants:

- entities sharing a `defined_in` co-home
- one `.rs` ↔ one `tu` (or none)
- `(kind, name, tu)` is unique across the whole file — two `.rs` claiming it is
  a duplicate Rust definition. The `tu` component is what keeps same-named
  file-local statics in different TUs distinct
- `depends_on` is acyclic

## anchors

The scheduler lays one anchor per member of a batch, in that agent's worktree,
resolving each member's home through this file. The agent replaces the line with
a doc comment naming what it emitted.

| anchor | for |
|---|---|
| `// crustify:todo: <name>` | a member, unfilled |
| `// crustify:todo: <name>.<field>` | one per field of a type member |
| `/// Wraps: <name>{.<field>}` | filled — a safe view over the FFI seam |
| `/// Replaces: <name>{.<field>}` | filled — a native Rust translation |

A field item is owner-qualified because a file-grained module holds many types.
Which fields a type carries follows the composer's section shaping: an
import-section type carries the fields target code touches, a target-section
type its full layout.
