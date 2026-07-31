# types.json schema

Field meaning for the per-stem `types.json` manifests produced by
`compose/types_manifest.py`. This file is the single source of field semantics.

Each `## <field>` section documents one record field; the heading name is the
field key.

A type stores **no lifecycle of its own**. Which routines drop, dispose or
clone it is recorded once on the acting SYMBOL — `syms.json`'s entry-level
`lifetime` block, which names its subject arg (see
[syms.md](syms.md#lifetime)) — and read back by reverse lookup:
`crustify-cli query symbols --lifetime-for <TAG>` groups every such symbol into
this type's `dropped_by` / `fields_disposed_by` / `cloned_by` candidates. One
fact, one home: a routine can never disagree with the type it acts on.

## name

The type's identifier: the C struct/union/enum tag. Composer-filled.

## typedef

List of typedef aliases that resolve to this tag. Composer-filled; `[]` when
there is none.

## kind

Composer-emitted: `struct`, `union`, or `enum`.

## declared_in

List of repo-root-relative header paths that declare the type (always a JSON
array, even for a single header). Same shape as the syms base. Composer-filled
for composer-emitted types.

## defined_in

Repo-root-relative path to the file holding the definition; nullable.
Composer-filled.

## casted

`{to: [tags], from: [tags]}`, composer-filled from the raw struct<->struct
pointer-cast graph (`edges/casts.ql`). `to` = tags this type is cast INTO (it is
a cast operand); `from` = tags cast INTO this type (it is a cast result). Both
are canonical struct tags (typedef spellings resolved), `[]` when none.

## opaque_in

Composer-filled `{file: [symbols]}` footprint: functions that touch this type
but only as an OPAQUE HANDLE, never accessing a field. The agent reads it for
lifecycle/forwarder op candidates. COMPLETE: every consumer
tree-wide is listed.

## non_opaque_in

Composer-filled `{file: [symbols]}` footprint: functions that read/write a FIELD
of this type and so need its concrete layout (incl. transitive `a->b->field`
reachers). COMPLETE: the FULL cross-codebase footprint for every
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

`ref` is decided by the field's true pointer depth (CodeQL `ptr_depth`), not by
looking for a `*` in `type`. C hides the star behind a name in two shapes the
string cannot show, both of which used to collapse to a bare scalar with no
`ptr` block -- an **object-pointer typedef** (`typedef struct _filesec
*filesec_t;`) and a **bare function pointer** (`int (*ctrl)(BIO *, int)`).

- **`sig_types`** -- composer-filled, present ONLY when `type` is a string that
  names nothing: a bare function pointer (`..(*)(..)`) or an anonymous
  aggregate. It lists the user types named inside that type -- for a function
  pointer, its signature (`int (*ctrl)(BIO *, …)` -> `["BIO"]`). Without it a
  vtable struct is a dependency LEAF: every slot renders `..(*)(..)`, so the
  DAG sees no edge and would schedule the struct before the types its function
  pointers traffic in. An ordinary `T *` field needs no help and carries none.

Three agent-fillable keys ride on a field record:

- **`ptr`** -- the ownership block (see [`## ptr`](#ptr)); pointer fields only.
- **`refcount`** -- `true` on the ONE field storing this type's reference count
  (the datum an up_ref bumps and a down-ref decrements), `false`/absent
  otherwise. Any field kind: a refcount is a by-value member, not a pointer. It
  decides which ROUTINE backs the type's `CDropped` / `CCloned` impl (down-ref
  and up_ref vs `*_free` and `*_dup`); the wrapper is `CBox` either way. It also
  names the field a generated shim reads when the type carries a refcount but
  exposes no up_ref routine.
- **`locked_by`** -- the concurrency binding on ANY field (pointer or not) that
  is accessed under a lock: `null`, or `{lock, lock_op, unlock_op}`. `lock` names
  the type's field or global variable storing the lock object that guards this field; `lock_op` is
  the LIST of acquire routines (which ops appear captures the read-vs-write
  discipline); `unlock_op` the LIST of release routines. 

## ptr

Per pointer field. The composer emits `ptr: null`, so **`null` means
unanalyzed** — whether a field has been through the analyzer is a null check on
one key. A submitted block replaces the prior WHOLESALE and must be complete,
so there is nothing to patch into a skeleton. Once filled: 

- **`scalar`** -- Is there any execution path where this pointer references a
  SINGLE pointee? If not, `null`; otherwise `{by_val: true}` (points at one inline
  value -- the ordinary `T*`) | `{by_ref: {owned, borrowed}}` (points at one
  POINTER -- a `T**`). Under `by_ref`, `owned`/`borrowed` is the INNER pointee's
  ownership (the top-level `owned`/`borrowed` then describes the OUTER slot);
  each is the same block as the top-level below. May co-exist with `array`;
  mutually exclusive with `string`.
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
- **`owned`** -- `true` if the field is owned by the type, `false` otherwise.
  Ownership is a `CBox` either way; the FIELD-TYPE's `refcount` decides whether
  that `CBox`'s teardown is a down-ref or a plain free, not this flag.
- **`borrowed`** -- `null`, or `{lifetime}`: the pointer is borrowed, bound to
  another entity's lifetime. Sources are `self` (the enclosing struct),
  `field:<name>` (a sibling field's storage), `static`, or `other` -- the
  field-oriented vocabulary.
- **`nullable`** -- may be NULL -> Rust `Option<...>`.
- **`mutable`** -- null/true/false: (i) `const=true` forces `false`; (ii)
  otherwise the agent decides by body inspection -- is the field written through
  the pointer?; (iii) `null` ONLY when undeterminable.
- **`note`** -- free-form; justify the above, highlight gaps and corner cases if any.

`owned` and `borrowed` MAY both be set -- runtime-conditional dual
ownership (owned on one path, borrowed on another); likewise
`array.by_ref.owned`+`.borrowed`.

**Invariants** (enforced on `--update`): `scalar` and `array` are each null |
exactly one of `{by_val, by_ref}`; `string` XOR (`array` | `scalar`); a pointer
sets at least one of `{scalar, array, string}` (the floor); `string` and `owned`
are explicit booleans (never null -- a fact left `null` where `false` was meant
is rejected);
a pointer is owned and/or borrowed, never neither -- as is each `by_ref`
element; a borrowed pointer needs a lifetime; `const`-in-type implies `mutable
!= true`.
