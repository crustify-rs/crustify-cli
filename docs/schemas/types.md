# types.json schema

Field and lifecycle-slot meaning for the per-stem `types.json` manifests
produced by `compose/types_manifest.py`. This file is the single source of
field semantics.

Each `## <field>` section documents one record field or lifecycle slot; the
heading name is the field key. Sections tagged *(cluster-only)* apply to the
synthetic string/array analyzers and are dropped from the struct
type-analyzer's schema.

## name

The type's identifier: the C struct/union/enum tag, or the synthetic name for a
string / array cluster entry. Composer-filled for native kinds, agent-filled for
synthetic kinds.

## typedef

List of typedef aliases that resolve to this tag (e.g. `SSL_SESSION` for
`ssl_session_st`). Composer-filled; `[]` when there is none.

## kind

Composer-emitted: `struct`, `union`, or `enum`.

Agent-synthesized: `string` (a NUL-string buffer strategy) and `array` (a
sized-buffer strategy, carrying `elems` for its typed aliases), both
made by the buffer pass.

## declared_in

List of repo-root-relative header paths that declare the type (always a JSON
array, even for a single header). Same shape as the syms base. Composer-filled
for composer-emitted types; agent-synthesized kinds (string / array) must emit
it as a list too -- never a bare string.

## defined_in

Repo-root-relative path to the file holding the definition; nullable.
Composer-filled. Port-vs-wrap scope is NOT a manifest field -- the orchestrator
computes it (`defined_in` in `scope.json`) and passes it to the analyzer as a
prompt arg per entry.

## allocs

Agent-filled: the byte-level allocator routine(s) that produce a raw
uninitialized `T`. These are the malloc/calloc-family allocators from the alloc
manifest; a type MAY have several, e.g. a heap-vs-mmap split. Empty for
stack/embedded types never heap-allocated.

## clone

Agent-filled `{shared, exclusive}` -- the routines behind Rust `Clone`:

- **`shared`** -- the refcount bump (the `up_ref`), or null -> `CRefCounted` / `CArc`.
- **`exclusive`** -- LIST of deep-copy / `dup` routines, `[]` if none -> `CCloned` / `CBox`.

Both may be set: a refcounted type is often also deep-copyable (`X509_up_ref` + `X509_dup`).

## drop

Agent-filled `{shared, exclusive, fields}`, each a LIST of destructor function names
(`[]` when none) -- the releasers that map to `impl Drop`:

- **`exclusive`** -- called when `T` is owned exclusively.
- **`shared`** -- refcount decrement then free; pairs with `clone.shared`.
- **`fields`** -- disposes owned *fields* separately from the storage free (a
  `*_dispose` / `*_cleanup`).

A role may hold several routines (e.g. a container's shallow free plus its deep
element-owning free). The same C function must not appear in two roles.

## locking

Agent-filled list of `{acquire, release, locks, locked_fields}` records, or `[]`
when `T` carries no internal lock discipline. A type may own multiple locks
guarding different groups of its own fields. Should be extracted by reasoning
semantically over the routines that touch this type's fields.

- **`acquire`** -- the routine that takes the lock.
- **`release`** -- the routine that releases it.
- **`locks`** -- the `fields[]` name(s) whose values STORE the lock object(s)
  (the mutex/rwlock members of `T`).
- **`locked_fields`** -- the fields the lock pair GUARDS (the state the critical
  section protects).

This drives the wrapper's synchronization: the `locks` field(s) hold the lock
primitive, and `locked_fields` name the state reachable only while it is held.

## conditional_drop

Agent-filled `{skip_when, skip_when_kind}`, or null. Names a gate a hypothetical
Rust `Drop` on `T` must honor BUT that the `drop` routine itself doesn't encode. A
`drop` whose prologue already checks the condition (e.g. `if (--p->refcount > 0)
return;`) keeps `conditional_drop:null` -- Rust `Drop` calling the C routine
short-circuits naturally. A `drop` that unconditionally releases storage while a
sibling release function does the gating (refcount-dec in `git_mwindow_put_pack`
-> `git_packfile_free`) fills it so Rust can port the gate into its impl. Plain
NULL-on-entry guards (`if (!p) return;`) are NOT `conditional_drop`.

## casted

`{to: [tags], from: [tags]}`, composer-filled from the raw struct<->struct
pointer-cast graph (`edges/casts.ql`). `to` = tags this type is cast INTO (it is
a cast operand); `from` = tags cast INTO this type (it is a cast result). Both
are canonical struct tags (typedef spellings resolved), `[]` when none.

## opaque_in

Composer-filled `{file: [symbols]}` footprint: functions that touch this type
but only as an OPAQUE HANDLE, never accessing a field. The agent reads it for
lifecycle/forwarder op candidates. ALL STRUCTS carry it. COMPLETE: every consumer
tree-wide is listed.

## non_opaque_in

Composer-filled `{file: [symbols]}` footprint: functions that read/write a FIELD
of this type and so need its concrete layout (incl. transitive `a->b->field`
reachers). ALL STRUCTS carry it. COMPLETE: the FULL cross-codebase footprint for every
type.

## fields

Per-field records:

- Scalar single -> `{name}` (layout-agnostic; bindgen handles it).
- Scalar array / by-value aggregate / aggregate array -> `{name, type, ref:"value", array?}`.
- Pointer (single or array) -> `{name, type, ref:"pointer", ptr:{...}, array?}`.

`type` is the element type (full, including `*`), omitted for scalar singles.
`ref` is value/pointer (the element's reference kind), omitted for scalar
singles. `array` is `{size:N}` (fixed) / `{size:null}` (flexible/incomplete
member), omitted when not an array.

## ptr

Per pointer field. The composer emits a null skeleton; the agent fills it.

- **`array`** -- `null` (single pointee) | `{by_val: true}` (buffer of inline values) |
  `{by_ref: {owned, borrowed}}` (buffer of element pointers -- a container; `owned`/
  `borrowed` is the ELEMENT ownership). Mutually exclusive with `string`.
- **`string`** -- pointee is a NUL-terminated string.
- **`owned`** -- `null`, or `{exclusive, shared}` (the pointer/buffer's own ownership):
  `exclusive` = sole ownership -> `CBox`; `shared` = refcounted -> `CArc`.
- **`borrowed`** -- `null`, or `{lifetime}`: `self` = the enclosing struct; `field:<name>` =
  a sibling field's storage; `static` = global; `other` = note.
- **`nullable`** -- can be NULL -> Rust `Option<...>`.
- **`mutable`** -- null/true/false: const-in-type -> false; pointer-to-user-type -> null;
  buffers/out-scalars -> agent decides.
- **`note`** -- free-form.

`owned` and `borrowed` may both be set -- runtime-conditional dual ownership (owned on one
path, borrowed on another); likewise `owned.exclusive`+`owned.shared` and
`array.by_ref.owned`+`.borrowed`.

**Invariants** (enforced on `--update`): `string` XOR `array`; a borrowed pointer needs a
lifetime; const-in-type implies `mutable != true`.

## array_fields

*(cluster-only -- dropped from the struct type-analyzer's `--schema`.)*

`elems` -- ARRAY-cluster only (a string is a single buffer and carries none). Lists the
concrete element types the buffer holds at call sites -- rows `{type, note}` -- which become
the array wrapper's typed aliases.