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

## dropped_by

Agent-filled `{exclusive, shared}`, each a LIST of destructor function names
(`[]` when none) -- the type's OWN releasers, mapping to `impl Drop`. A TOP-LEVEL
field on the type record (there is no `lifetime` wrapper). The retired
`drop.fields` dispose role is now captured per-field by
[`owned.disposed_in`](#ptr).

- **`exclusive`** -- called when `T` is owned exclusively (plain free -> `CBox::drop`).
- **`shared`** -- refcount decrement then free (-> `CArc::drop`/`down_ref`); pairs
  with `cloned_by.shared`.

A role may hold several routines. The same C function must not appear in both roles.

## cloned_by

Agent-filled `{exclusive, shared}`, each a LIST of clone routines (`[]` when
none) -- the routines behind Rust `Clone`. A TOP-LEVEL field on the type record.

- **`exclusive`** -- deep-copy / `dup` routines -> `CCloned` / `CBox::clone`.
- **`shared`** -- refcount bumps (the `up_ref`s) -> `CRefCounted` / `CArc::clone`.

Both may be set: a refcounted type is often also deep-copyable (`X509_up_ref` + `X509_dup`).

Locking is no longer a type-level block either; the per-field concurrency binding
lives on the guarded field's `locked_by` (see [`## fields`](#fields)).

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

Two agent-fillable keys ride on a field record:

- **`ptr`** -- the ownership block (see [`## ptr`](#ptr)); pointer fields only.
- **`locked_by`** -- the concurrency binding on ANY field (pointer or not) that
  is accessed under a lock: `null`, or `{lock, lock_op, unlock_op}`. `lock` names
  the type's field or global variable storing the lock object that guards this field; `lock_op` is
  the LIST of acquire routines (which ops appear captures the read-vs-write
  discipline); `unlock_op` the LIST of release routines. It sits on the GUARDED
  field (not the lock field), and at the field level -- NOT inside `ptr` --
  because the guarded datum is often a non-pointer (a refcount `int`, a flag).
  The symbol-side form of the same binding lives on a global entry (see
  [syms.md](syms.md)).

## ptr

Per pointer field. Composer emits a null skeleton; the agent fills it. This is
the SAME block a symbol's [`ptr_args`/`ptr_ret`](syms.md#ptr_argsptr_ret)
carries, copied verbatim -- only the `owned` framing (a field's own ownership vs
ownership transferred at a call), the `dropped_by`/`cloned_by` examples, and the
`borrowed` vocabulary differ, marked below.

- **`scalar`** -- `false`: this is never a single pointee, only `array` or `string` |
  `true`: this may be a single pointee. May co-exist with `array`; mutually
  exclusive with `string`.
- **`array`** -- `null`: this may never be an array, only a single pointee or a string
  | `{by_val: true}` (buffer of inline
  values) | `{by_ref: {owned, borrowed}}` (buffer of element pointers -- a
  container). Under `by_ref`, `owned`/`borrowed` is the ELEMENT ownership, and
  EACH is the same block as the top-level `owned`/`borrowed` below -- so a
  container of owned elements carries the element's release/clone bindings.
  May co-exist with `scalar`; mutually exclusive with `string`.
- **`string`** -- pointee is a NUL-terminated string.
- **`owned`** -- `null`, or `{exclusive, shared, dropped_by, cloned_by, disposed_in}`:
  the field's own ownership. `exclusive` = sole ownership -> `CBox`;
  `shared` = refcounted -> `CArc`.
  - **`dropped_by`** -- the release binding: the list of routine names that may
  free this owned pointer, or `null`. On a struct field it names how the enclosing
  type's destructor frees the field (`ssl_st.ctx` -> `["SSL_CTX_free"]`). A list
  because the routine that frees the pointer may be decided by the caller at
  runtime, which signals that a Rust-native representation would fork it to provide
  multiple `Drop` implementations that a Rust caller can choose at runtime. This is
  the single source of "what is a destructor": a routine is a `Drop` iff it is named
  here (exclusive owner -> `CBox::drop`; shared -> `CArc::drop`/`down_ref`).
  - **`cloned_by`** -- the mirror for `Clone`: the list of routines that produce
  a fresh owned copy of this pointer, or `null`. Generally, any existing dup will
  be bundled with the above drop instances to form Rust-native safe types.
  Exclusive -> the deep-copy dup (`strdup`/`memdup` -> `CBox::clone`); shared ->
  the `up_ref` (`CArc::clone`).
  - **`disposed_in`** -- TYPES-ONLY (a symbol arg/return has no fields-only
  teardown, so this key is absent on the shared symbol block): the LIST of THIS
  type's own drop-time methods whose body calls this field's `dropped_by` on the field --
  the teardown call site(s) invoked when the type itself is dropped (i.e. not when the
  field is replaced), or `[]` when no method of the type
  frees it. `dropped_by` is the freeing agent (the `_by`); `disposed_in` is the
  method it is invoked in (the `_in`). Usually uniform = the type's destructor;
  divergence is a field released in a distinct `*_dispose`/`*_cleanup`. Combined
  with each method's own storage-free fact it derives `dropped_by.{exclusive,
  shared}` (the method frees the storage too) vs a fields-only dispose.
- **`borrowed`** -- `null`, or `{lifetime}`: the pointer is borrowed, bound to
  another entity's lifetime. Sources are `self` (the enclosing struct),
  `field:<name>` (a sibling field's storage), `static`, or `other` -- the
  field-oriented vocabulary.
- **`nullable`** -- may be NULL -> Rust `Option<...>`.
- **`mutable`** -- null/true/false: (i) `const=true` forces `false`; (ii)
  otherwise the agent decides by body inspection -- is the field written through
  the pointer?; (iii) `null` ONLY when undeterminable.
- **`note`** -- free-form; justify the above, highlight gaps and corner cases if any.

`owned` and `borrowed` MAY both be non-null -- runtime-conditional dual ownership
(owned on one path, borrowed on another); likewise `owned.exclusive`+`.shared`
and `array.by_ref.owned`+`.borrowed`. A pointer that is neither owned nor
borrowed is a transient reference the type does not retain. The judgement is a
fact about the C code: it does not depend on port/wrap scope, nor on the Rust
representation the pointer eventually gets.

**Invariants** (enforced on `--update`): `array` is null | exactly one of
`{by_val, by_ref}`; `string` XOR (`array` | `scalar`); a borrowed pointer needs a
lifetime; `exclusive`/`shared` only under `owned`; `const`-in-type implies
`mutable != true`.

