# syms.json schema

Field **meaning** for the per-stem `syms.json` manifests (produced by
`compose/syms_manifest.py`). This file is the single source of field semantics;
`crustify query syms --schema` emits it. The exact JSON shape an analyzer submits
-- and its validation rules -- is the *contract*, served separately by `crustify
query syms --update-help`, so meaning and shape never duplicate.

One entry per symbol (function, macro, global, or callback -- a function-pointer
typedef) whose definition -- or, when the symbol is never defined (a header
typedef/decl), its declaration -- lives in a file of this stem-group.

Each `## <field>` section documents one record field; the heading name is the
field key.

## partition

Files are grouped into one manifest dir per `path_partition.manifest_dir_for(file)`
-- stem-grouped. `ssl/record/record.c` and `ssl/record/record.h` both land in
`analysis/ssl/record/record/`. System / external files (CodeQL reports these by
absolute path) route under `analysis/system/` (e.g. `/usr/include/string.h` ->
`analysis/system/usr/include/string/syms.json`).

## reach fields

`used_by` and `depends_on` are emitted for EVERY entry, port-scope and
wrap-scope alike, and carry the full scope-agnostic reach: `depends_on`
includes body-level callees and field accesses even for an entry no target
ever ports. The manifest is a shared repo-tier record of facts; it holds no
port/wrap shape, and consumers apply scope themselves against
`crustify/targets/<target>/scope.json`.

Two do. `analyze dag` narrows edges per node -- a wrap-scope node contributes
only its signature, since a binding is emitted from the signature alone and
its body is never translated. `query --methods` filters the consumer
footprints to scope at read time. Both derive scope; neither is baked here.

A macro's body is never emitted; the agent reads the expansion from source
when it needs to classify or port it.

## name

The C identifier exactly as written at the definition site. Composer-filled;
agents never edit it.

## kind

The symbol's category. Composer-filled and **terminal** -- the agent never edits
it. For functions and globals it comes deterministically from CodeQL linkage:
`function_exported`, `function_static`, `function_inline_header`,
`function_inline_tu`, `global_static`, `global_extern`. Every `#define` is
`macro`, and that is a macro's ONLY classification -- what it expands to is not
stored anywhere; a consumer that needs to know reads the `#define` from the
source at `defined_in`.

The enum is `function_{exported,static,inline_header,inline_tu}`, `macro`,
`global_{static,extern}`, `callback`. Wrap output never carries the TU-bounded
kinds `function_static`, `function_inline_tu`, `global_static`.

**`callback`** -- a function-pointer typedef (CodeQL identifies it
deterministically: a typedef whose unwrap chain reaches a RoutineType).
Composer-filled kind; signature-shaped (carries `ptr_args` / `ptr_ret` /
`used_by.{call,ref}` / a signature `depends_on`, NO body). `defined_in` is null (a
header typedef). The agent fills ONLY its per-arg/return ownership and its
`lifetime` role (same rules as functions -- an invoked `free_func` typedef IS a
dropper), inferring both from `used_by.call` (the invokers). When invokers
realize DIFFERENT contracts, the agent FORKS the callback: `--update` splits it
into multiple `kind:callback` entries, same name/type but distinct
`ptr_args`/`ptr_ret`/`lifetime`, disambiguated by a `variant` index (0 = primary,
composer-emitted; >=1 = agent-created fork) and a partitioned `used_by.call`. One
entry = one Rust wrapper. The `variant` field is absent/0 for the common
single-contract case.

## declared_in

Sorted list of header files that declare or export the symbol. Composer-filled.

## defined_in

The single file holding the definition: `.c` for functions and globals, `.h` for
inline functions and macros. Null when the symbol is declared in the DB but never
defined. Composer-filled.

## type

The full C signature for functions, the declared C type for globals, and null
for macros (a macro has no type). Composer-filled.

## is_variadic

`true` when the function takes a trailing `...` (a C variadic like `printf`),
`false` otherwise. Composer-filled from CodeQL (`Function.isVarargs()`) and
**terminal**. Present on function kinds; absent on macros, globals and
callbacks.

Load-bearing because the `type` signature string does not reliably reveal it,
and a variadic has no safe Rust binding: `extern "C"` variadic functions are
unstable, so the wrap stage must either bind the `v*`-suffixed `va_list` sibling
(`vfprintf` for `fprintf`) or emit a fixed-arity C shim. Knowing this before
codegen is what stops a wrapper being generated against a signature Rust cannot
express.

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

A **bare** (un-typedef'd) function-pointer parameter reports `type:
"(routine)"`, depth 1 -- it names nothing depend-able, which is what makes it
bare, but it IS a pointer crossing the boundary and so carries an ownership
block like any other. A *typedef'd* callback instead reports the typedef name
(`SSL_verify_cb`), which routes to that callback's own `kind:"callback"` entry.
The user types inside a bare signature are not lost: they land on the enclosing
symbol's `depends_on.types`.

AGENT field -- each record carries a single `ptr` sub-object holding the
ownership block, the SAME structured block a struct field's
[`ptr`](types.md#ptr) carries (so a pointer is described identically whether it
lives in a struct or crosses a call). It nests under `ptr` -- isolated from the
composer's top-level structural keys -- so `--update` replaces `ptr_args[i].ptr`
/ `ptr_ret.ptr` WHOLESALE, and a submitted block must be complete.

The composer emits `ptr: null`, so **`null` means unanalyzed**: whether a pointer
has been through the analyzer is a null check on one key. (An all-null keyed
skeleton could not carry that signal -- it is indistinguishable from a block the
agent filled with nulls -- and would be pointless anyway, since a submission
replaces the block wholesale rather than patching keys into it.) The keys of a
filled block: 

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
`string` and `owned` are explicit booleans (never null); a pointer sets at least
one of `{scalar, array, string}` (the floor); a pointer is either owned, or
borrowed, or both (never none) -- as is each `by_ref` element; a borrowed pointer
needs a lifetime, and an `arg:<name>` lifetime names a real arg BY NAME;
`const`-in-type implies `mutable != true`; `ptr_ret` only on a pointer-returning
symbol. `lifetime` inside a `ptr` block is rejected -- it is a symbol-level
field, see [`lifetime`](#lifetime).

## lifetime

Which lifecycle-primitive role THIS symbol plays, and on which of its args.
Agent-filled; `null` in the composer skeleton. Present on functions and
callbacks (the kinds with a call boundary); absent on globals and macros -- a
global's ownership has no acting method, and a macro has no args.

The role belongs to the SYMBOL, not to one of its pointer records: `SSL_free` IS
a dropper. The arg it acts on is named in `for`, so a single block per symbol
suffices and there is no ambiguity about which pointer the role refers to.

- **`for`** -- the arg the role acts on, BY NAME, as a bare name (`s`, not
  `arg:s` and not a position). Same referencing rule as a borrowed pointer's
  `arg:<name>` source, so every arg-dependent fact in the schema names args one
  way. Must be one of this symbol's pointer args.
- **`is_dropper`** -- `true` if the symbol frees the arg's own STORAGE (i.e. the
  heap allocation): a full destructor. Requires the arg to be `owned`.
- **`is_disposer`** -- `true` if the symbol frees the storage of the arg's
  FIELDS but KEEPS the arg's own storage: a teardown / cleanup / reset that
  leaves the object reusable. Independent of `owned`/`borrowed` (a cleanup
  borrows the container).
- **`is_cloner`** -- `null`, or `{deep, upref}`: the symbol produces a copy of
  the arg. `deep` = a fresh allocation -> `Clone for CBox` on a type with no
  refcount, else a plain method (a refcounted type's `Clone` is its up_ref).
  `upref` = a refcount bump -> also `Clone for CBox`, via `impl_cloned_upref!`.
  Both MAY be set: a body that branches between the two, or a `void *` whose
  concrete element decides at runtime. Requires the arg to be `borrowed` (it
  reads the source to copy it).

`is_dropper` and `is_disposer` are **mutually exclusive**: the arg's storage is
either released or retained, never both. A full destructor is `is_dropper` alone
-- it tears the fields down on the way, but its observable contract is that the
allocation is gone; a cleanup that resets the fields in place is `is_disposer`
alone.

From these each type's `Drop`/`Clone` is reverse-derived: the type-analyzer
collects the symbols whose `lifetime.for` arg is of that type
(`query symbols --lifetime-for <TAG>`).

**Invariants** (enforced on `--update`): `null` is both "not a lifecycle
primitive" and the unanalyzed state, and is always accepted; a non-null block
must assert at least one role (`is_dropper` | `is_disposer` |
`is_cloner.{deep,upref}`) -- otherwise submit `null`; `for` is required and
names a real pointer arg of this symbol, by bare name; `is_dropper` and
`is_disposer` are mutually exclusive booleans; `is_cloner.deep`/`.upref` are
explicit booleans (never null); `is_dropper` implies that arg is `owned`;
`is_cloner` implies that arg is `borrowed` -- both checked against the arg's
`ptr` as it stands AFTER the update, and skipped while that `ptr` is still
`null` (there is no ownership fact yet to contradict).

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

Composer-filled (agents must not modify). `call` and `ref`
name the enclosing functions (or file paths for file-scope macro expansions) that
reach this entry. By kind: `function_*` -> `{call:[callers], ref:[addr-of users]}`,
where a site that both calls and takes the address is listed only under `call`;
`global_*` -> `{call:null, ref:[accessors]}`; `macro_*` -> `{call:[expansion
sites], ref:[]}`. The composer's default bucketing is fine for most kinds; the
analyzer may re-bucket `call`<->`ref` when a macro kind justifies it.

## depends_on

Composer ground-truth (agents must not modify).
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
