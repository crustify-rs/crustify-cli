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

COMPOSER fields (skeleton, always set), at the record's TOP level: on each
`ptr_args` record `position` and `name` (param name as written, falling back to
`arg<N>`); on both `ptr_args` and `ptr_ret` `type` (verbatim innermost pointee
type -- a user tag like `EVP_PKEY`, a primitive like `char`/`void`, or the
synthetic markers `(routine)`/`(array)`), `const` (is the innermost pointee
const-qualified?), and `depth` (1 for `T*`, 2 for `T**`, ...).

AGENT field -- each record carries a single `ptr` sub-object holding the
ownership block, the SAME structured block a struct field's
[`ptr`](types.md#ptr) carries (so a pointer is described identically whether it
lives in a struct or crosses a call). It nests under `ptr` -- isolated from the
composer's top-level structural keys -- so `--update` replaces `ptr_args[i].ptr`
/ `ptr_ret.ptr` WHOLESALE, and a submitted block must be complete. Its keys:

- **`scalar`** -- Is there any execution path where this pointer references a
  SINGLE pointee? If not, `null`; otherwise `{by_val: true}` (points at one inline
  value -- the ordinary `T*`) | `{by_ref: {owned, borrowed}}` (points at one
  POINTER -- a `T**`, e.g. an out-param). Under `by_ref`, `owned`/`borrowed` is
  the INNER pointee's ownership (the top-level `owned`/`borrowed` then describes
  the OUTER slot -- for an out-param, borrowed); each is the same block as the
  top-level below. May co-exist with `array`; mutually exclusive with `string`.
- **`array`** -- Is there any execution path where this pointer references an array
  of elements? If not, `null`; otherwise `{by_val: true}` (buffer of inline
  values) | `{by_ref: {owned, borrowed}}` (buffer of element pointers -- a
  container). Under `by_ref`, `owned`/`borrowed` is the ELEMENT ownership, and
  EACH is the same block as the top-level `owned`/`borrowed` below -- so a
  container of owned elements carries the element's release/clone bindings.
  May co-exist with `scalar`; mutually exclusive with `string`. `scalar.by_ref`
  vs `array.by_ref` differ only in cardinality (one pointer vs a buffer of them).
- **`string`** -- Is there any execution path where this pointer is a NUL-terminated string?
  If yes, `true`; otherwise `false`.
- **`owned`** -- `true` if ownership TRANSFERRED across the call, `false`
  otherwise. On an arg the callee takes it, on the return the caller receives it.
- **`borrowed`** -- `null`, or `{lifetime}`: the pointer is borrowed, bound to
  another entity's lifetime. Sources are `arg:<name>`, `arg:<name>->path`,
  `static`, or `other` -- the arg-oriented vocabulary (a struct field instead
  borrows from `self` / a sibling field). Args are referenced BY NAME (the
  composer names every arg, real or synthetic `arg<pos>`); the positional
  `arg:<idx>` form is rejected. A transient read that doesn't outlive the call
  borrows from its OWN arg -- `arg:<its own name>`.
- **`lifetime`** -- ARG-ONLY (absent on `ptr_ret`, a global `ptr`, and a struct
  field's `ptr` -- a return is produced not acted on, a field derives from its
  field-type). Which lifecycle-primitive role THIS method plays on THIS arg; from
  it each type's `Drop`/`Clone` is reverse-derived (the type-analyzer collects the
  methods whose arg of that type carries the flag). Independent, several may be set
  on one arg (a full dtor
  is `is_dropped` + `is_disposed`).
  - **`is_dropped`** -- `true` if the method frees the arg's own STORAGE (i.e. heap
  allocation), `false` otherwise. Requires `owned`.
  - **`is_disposed`** -- `true` if the method frees the storage of the arg's
  FIELDS (a full destructor, or a teardown / cleanup that resets the fields but
  keeps the storage), `false` otherwise.  Independent of `owned`/`borrowed` (a
  cleanup borrows the container).
  - **`is_cloned`** -- `null`, or `{deep, upref}`: the method produces a copy of
    the arg. `deep` = a fresh allocation -> `Clone for CBox` on a type with no
    refcount, else a plain method (a refcounted type's `Clone` is its up_ref).
    `upref` = a refcount bump -> also `Clone for CBox`, via
    `impl_cloned_upref!`. Both MAY be set: a body that branches
    between the two, or a `void *` whose concrete element decides at
    runtime. Requires `borrowed` (it reads the source to copy it).
- **`nullable`** -- may be NULL -> Rust `Option<...>`.
- **`mutable`** -- null/true/false: (i) `const=true` forces `false`; (ii)
  otherwise the agent decides by body inspection -- does the callee write through
  the pointer?; (iii) `null` ONLY when undeterminable (no definition available,
  e.g. an external symbol).
- **`note`** -- free-form; justify the above, highlight gaps and corner cases if any.

`owned` and `borrowed` MAY both be set -- runtime-conditional dual ownership
(owned on one path, borrowed on another); likewise, `array.by_ref.owned`+`.borrowed`.

**Invariants** (enforced on `--update`): a `ptr` block replaces the record's
prior block wholesale, so it must be complete -- `scalar` and `array` are each
null | exactly one of `{by_val, by_ref}`; `string` XOR (`array` | `scalar`);
`string`, `owned`, and `is_cloned.deep`/`.upref` are explicit booleans (never
null); a pointer sets at least one of `{scalar, array, string}` (the floor); a
pointer is either owned, or borrowed, or both (never none) -- as is each `by_ref`
element; a borrowed pointer needs a lifetime, and an `arg:<name>` lifetime names
a real arg BY NAME; `is_dropped` implies `owned`; `is_cloned` implies `borrowed`;
`const`-in-type implies `mutable != true`; `ptr_ret` only on a pointer-returning
symbol.

## ptr / locked_by (globals)

A `global_*` entry has no call boundary, so it carries no `ptr_args`/`ptr_ret`.
Two agent-filled slots take their place (both `null` in the composer skeleton):

- **`ptr`** -- `null`, or the SAME ownership block a `ptr_args`/`ptr_ret` record
  nests under its `ptr` (`scalar`, `array`, `string`, `owned`, `borrowed`,
  `nullable`, `mutable`, `note`), for a global that stores a pointer (i.e. the
  pointee is allocated on the heap, or is another global).  Here the
  entry's `ptr` IS that block directly (a global has no `position`/`type`/`const`
  to un-mix, since those live at the entry level), so it too is replaced
  wholesale. `null` for a non-pointer global (a scalar or a by-value struct).
- **`locked_by`** -- `null`, or `{lock, lock_op, unlock_op}`: the concurrency
  binding on ANY global (pointer or not) accessed under a lock.
  - **`lock`** -- name of the lock object (a global or field) that guards the slot.
  - **`lock_op`** -- the LIST of acquire routines (e.g.
    `["CRYPTO_THREAD_read_lock", "CRYPTO_THREAD_write_lock"]`); which ops appear
    captures the read-vs-write discipline.
  - **`unlock_op`** -- the LIST of release routines (e.g. `["CRYPTO_THREAD_unlock"]`).

  `locked_by` sits at the entry level (a sibling of `ptr`), NOT inside `ptr`,
  because the guarded datum is often a non-pointer (a refcount `int`, a flag).
  The struct-field form of the same binding lives on the field record (see
  [types.md](types.md)).

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

## provenance

Illustrative slice only; a real run emits hundreds-to-thousands of entries per
manifest dir depending on stem-group density.
