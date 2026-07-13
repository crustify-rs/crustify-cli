# syms.json schema

Field **meaning** for the per-stem `syms.json` manifests (produced by
`compose/syms_manifest.py`). This file is the single source of field semantics;
`crustify query syms --schema` emits it. The exact JSON shape an analyzer submits
-- and its validation rules -- is the *contract*, served separately by `crustify
query syms --update-help`, so meaning and shape never duplicate.

One entry per symbol (function, macro, global, or callback -- a function-pointer
typedef) whose definition -- or, when the symbol is never defined (a header
typedef/decl), its declaration -- lives in a file of this stem-group. Two entry
shapes share one schema: a BASE shape carried by every entry, plus an optional
PORT-SCOPE ADDITIONS layer (`used_by`, `depends_on`) the composer adds when the
entry's defining file is listed in `crustify/targets/<target>/scope.json`. There
is no separate wrap vs port template: a wrap entry is simply base-only.

Each `## <field>` section documents one record field; the heading name is the
field key.

## partition

Files are grouped into one manifest dir per `path_partition.manifest_dir_for(file)`
-- stem-grouped. `ssl/record/record.c` and `ssl/record/record.h` both land in
`analysis/ssl/record/record/`. System / external files (CodeQL reports these by
absolute path) route under `analysis/system/` (e.g. `/usr/include/string.h` ->
`analysis/system/usr/include/string/syms.json`).

## port_additions

The two port-scope fields (`used_by`, `depends_on`) are emitted only when the
entry's `defined_in` -- or `declared_in[0]` for declaration-only entries -- is
listed under `.port` in `crustify/targets/<target>/scope.json`. Wrap-scope
entries omit both. A macro's body is never emitted; the agent reads the
expansion from source when it needs to classify or port it.

## name

The C identifier exactly as written at the definition site. Composer-filled;
agents never edit it.

## kind

The symbol's category. Composer-filled and **terminal** -- the agent never edits
it. For functions and globals it comes deterministically from CodeQL linkage:
`function_exported`, `function_static`, `function_inline_header`,
`function_inline_tu`, `global_static`, `global_extern`. Every `#define` is
`macro`; what a macro *expands to* is recorded in the separate [`macro`](#macro)
block, not by subdividing `kind`.

The enum is `function_{exported,static,inline_header,inline_tu}`, `macro`,
`global_{static,extern}`, `callback`. Wrap output never carries the TU-bounded
kinds `function_static`, `function_inline_tu`, `global_static`.

**`callback`** -- a function-pointer typedef (CodeQL identifies it
deterministically: a typedef whose unwrap chain reaches a RoutineType).
Composer-filled kind; signature-shaped (carries `ptr_args` / `ptr_ret` /
`used_by.{call,ref}` / a signature `depends_on`, NO body). `defined_in` is null (a
header typedef). The agent fills ONLY its per-arg/return ownership (same ptr
rules as functions), inferring it from `used_by.call` (the invokers). When
invokers realize DIFFERENT ownership, the agent FORKS the callback: `--update`
splits it into multiple `kind:callback` entries, same name/type but distinct
`ptr_args`/`ptr_ret`, disambiguated by a `variant` index (0 = primary,
composer-emitted; >=1 = agent-created fork) and a partitioned `used_by.call`. One
entry = one Rust wrapper. The `variant` field is absent/0 for the common
single-contract case.

## macro

Agent-filled; `null` for every kind other than `macro`. Three booleans describing
what the `#define` expands to. The body is NOT in the manifest -- read the source
at `defined_in` and locate the `#define`.

- **`alias`** -- the expansion REFERENCES existing symbol(s) or type(s): a
  function call, a global read/write, an object-like alias, or a type alias.
  E.g. `OPENSSL_malloc(n)` -> `CRYPTO_malloc(n, __FILE__, __LINE__)`, or
  `ERR_raise(lib, r)` -> `ERR_raise_data(lib, r, NULL)`.
- **`const`** -- the expansion is a typed compile-time constant: a numeric, char,
  or string literal, an enum value, or a substitution chain terminating in one.
- **`typegen`** -- the expansion DECLARES new types and/or their ops, once per
  instantiation, expanded at FILE scope: `DEFINE_STACK_OF(T)`,
  `DEFINE_LHASH_OF_EX(T)`.

The flags are independent, and **all three may be false**: a token-paste utility,
a header-guard sentinel, or a pure-arithmetic expression references nothing,
yields no constant, and declares no type.

**Downstream (the shim rule).** Rust FFI can never call a macro. Bindgen already
has a binding for a `const` (a `pub const`), for an `alias` (its target -- Rust
calls the target directly), and for a `typegen` (the types and ops its expansion
emits). So a macro needs a C shim exactly when it is **none of the three**.

## declared_in

Sorted list of header files that declare or export the symbol. Composer-filled.

## defined_in

The single file holding the definition: `.c` for functions and globals, `.h` for
inline functions and macros. Null when the symbol is declared in the DB but never
defined. Composer-filled.

## type

The full C signature for functions, the declared C type for globals, and null
for macros (a macro has no type). Composer-filled.

## loc

Body line span (`endLine-startLine+1`) of a function's definition; 1 for a
global, 0 for a macro; 0 when absent (a pre-loc extraction). Composer-filled from
`functions.csv`. Feeds the port bin-packer's lines-of-code batch budget
(`config.PORT_MAX_LOC`), which binds together with the symbol-count cap.

## ptr_args/ptr_ret

The two pointer records at a call boundary. `ptr_args` is a LIST -- one record
per pointer parameter, ordered by position (empty when the function has no
pointer params); `ptr_ret` is a single record, or null when the return type
isn't a pointer. Both carry the SAME fields: we extract identical properties for
an argument and a return, so there is no reason to fork their schemas. The only
difference is that a `ptr_args` record also names the parameter (`position` /
`name`).

COMPOSER fields (skeleton, always set): on each `ptr_args` record `position` and
`name` (param name as written, falling back to `arg<N>`); on both `ptr_args` and
`ptr_ret` `type` (verbatim innermost pointee type -- a user tag like `EVP_PKEY`,
a primitive like `char`/`void`, or the synthetic markers `(routine)`/`(array)`),
`const` (is the innermost pointee const-qualified?), and `depth` (1 for `T*`, 2
for `T**`, ...).

AGENT fields -- the SAME structured ownership block a struct field's
[`ptr`](types.md#ptr) carries, so a pointer is described identically whether it
lives in a struct or crosses a call:

- **`array`** -- `null` (single pointee) | `{by_val: true}` (buffer of inline
  values) | `{by_ref: {owned, borrowed}}` (buffer of element pointers -- a
  container; `owned`/`borrowed` is the ELEMENT ownership). Mutually exclusive
  with `string`.
- **`string`** -- pointee is a NUL-terminated string.
- **`owned`** -- `null`, or `{exclusive, shared}`: ownership TRANSFERRED across
  the call (on an arg the callee takes it, on the return the caller receives it).
  `exclusive` = sole ownership -> `CBox`; `shared` = refcounted -> `CArc`.
- **`borrowed`** -- `null`, or `{lifetime}`: the pointer is borrowed, bound to
  another entity's lifetime. Sources are `arg:<name>`, `arg:<name>->path`,
  `static`, or `other` -- the arg-oriented vocabulary (a struct field instead
  borrows from `self` / a sibling field).
- **`nullable`** -- may be NULL -> Rust `Option<...>`.
- **`mutable`** -- null/true/false: (i) `const=true` forces `false`; (ii)
  otherwise the agent decides by body inspection -- does the callee write through
  the pointer?; (iii) `null` ONLY when undeterminable (no definition available,
  e.g. an external symbol).
- **`note`** -- free-form.

`owned` and `borrowed` MAY both be non-null -- runtime-conditional dual ownership
(owned on one path, borrowed on another); likewise `owned.exclusive`+`.shared`
and `array.by_ref.owned`+`.borrowed`. A pointer that is neither owned nor
borrowed is a transient read the callee does not retain. The judgement is a fact
about the C code, uniform for every symbol: it does not depend on port/wrap
scope, nor on the Rust representation the pointer eventually gets.

**Invariants** (enforced on `--update`): `array` is null | exactly one of
`{by_val, by_ref}`; `string` XOR `array`; a borrowed pointer needs a lifetime;
`exclusive`/`shared` only under `owned`; `const`-in-type implies `mutable != true`;
`ptr_ret` only on a pointer-returning symbol.

## used_by

PORT-SCOPE addition; composer-filled (agents must not modify). `call` and `ref`
name the enclosing functions (or file paths for file-scope macro expansions) that
reach this entry. By kind: `function_*` -> `{call:[callers], ref:[addr-of users]}`,
where a site that both calls and takes the address is listed only under `call`;
`global_*` -> `{call:null, ref:[accessors]}`; `macro_*` -> `{call:[expansion
sites], ref:[]}`. The composer's default bucketing is fine for most kinds; the
analyzer may re-bucket `call`<->`ref` when a macro kind justifies it.

## depends_on

PORT-SCOPE addition; composer ground-truth (agents must not modify).
`depends_on.syms` is the forward callee/reference set: each record is `{name,
defined_in, declared_in}`, with `defined_in` nullable for externals (e.g. libc
functions not in the DB) -- resolve via `defined_in` first, else
`declared_in[0]`. `depends_on.types` is the forward type-use list: each record is
`{type, fields}`, where `type` is the canonical struct/union/enum tag (typedef
chains resolved) and `fields` are the accessed field names. The composer unions
signature types (parameters/return) with body-touched types from
`t2/field_accesses.csv`; signature types come first (signature order), body-only
types follow (first-encounter order); a signature type whose body touches no
field carries `fields:[]` (opaque use).

## lifetime

Agent-filled, `null` for ordinary symbols. Present when the symbol is a
**lifecycle primitive**: a member of the byte-level allocator surface, or of a
refcount / lock cluster. 

Exactly ONE subfield is populated, and the subfield name IS the primitive kind:

- **`alloc`** -- a byte-level (de-)allocator. `{role, freed_by, synth}`:
  - **`role`** -- `"allocator"` (produces a raw buffer) or `"free"` (the untyped
    deallocator that releases one).
  - **`freed_by`** -- allocator only: the LIST of `free` symbol names that release
    this allocator's output. A list, because one buffer may be released by more
    than one deallocator (a plain free and a clearing free). Each pairing is a
    distinct Rust drop strategy, so `(free, synth)` -- not the allocator -- is the
    family identity.
  - **`synth`** -- allocator only: `"string"` (result is a NUL-terminated string
    -> `CrustifyStr<D>`) or `"array"` (a sized byte buffer -> `CVec<T, S>`). A
    semantic judgement, NOT readable from the signature: a `char *` return may be
    either.
- **`clone`** -- a byte-level duplicator (`*strdup`, `*memdup`): it allocates a
  fresh, independent copy. `{freed_by, synth}`, same meaning as under `alloc` --
  a duplicator allocates, so it belongs to a family. It is a separate kind because
  its Rust slot is the `Clone` impl (`CCloned::c_clone` for a string,
  `CLenCloned::c_clone_len` for an array), not a constructor.
- **`refcount`** -- a raw refcount manipulation op. `{op, cluster}`:
  - **`op`** -- `"new"` | `"up"` | `"down"` | `"get"` | `"free"` | `"assert"`.
  - **`cluster`** -- the refcount type's name (e.g. `"PROJ_REF_COUNT"`), grouping
    the op with its siblings. An opaque grouping id; nothing else is recorded
    about the cluster.
- **`lock`** -- a raw locking op. `{op, cluster}`:
  - **`op`** -- `"new"` | `"read_lock"` | `"write_lock"` | `"unlock"` | `"free"`.
    `read_lock` appears only for reader-writer locks; `write_lock` doubles as the
    single `lock` for mutex / spinlock kinds.
  - **`cluster`** -- the lock type's name (e.g. `"PROJ_RWLOCK"`).

**Duplicator vs copy.** A routine that PRODUCES a buffer is a duplicator
(`clone`). One that merely FILLS a caller-owned buffer (`memcpy`, `strncpy`) is a
copy: it never owns, so it fills no lifecycle slot and carries `lifetime: null`.
Its safe shim is a `CCell`-parametric `mem_copy`, not a constructor.

**Untyped only (`alloc` / `clone`).** Tag an allocator, free, or duplicator only
when the buffer it produces or releases is raw bytes or a string (`void *`,
`char *`, `unsigned char *`, `void **`). A routine parameterised by or returning a
named aggregate type is a TYPE constructor/destructor and belongs in that type's
`types.json` `lifetime` block, never here. This restriction does NOT apply to
`refcount` / `lock` ops, which necessarily take their cluster's own type.

**Tag the callable, never the macro.** Rust FFI cannot call macros, so a macro
that aliases an allocator (`OPENSSL_malloc` -> `CRYPTO_malloc`, i.e.
`macro.alias = true`) carries `lifetime: null`; the underlying function carries
the block.

**Derived, not authored.** Allocator families and the synthetic `string` /
`array` cluster entries are composed by grouping allocators on `(freed_by,
synth)`. This block records the per-symbol facts; nothing stores the groups.

## provenance

Illustrative slice only; a real run emits hundreds-to-thousands of entries per
manifest dir depending on stem-group density.
