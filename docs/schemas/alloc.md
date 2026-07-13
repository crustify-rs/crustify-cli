# alloc.json schema

Field meaning for the repo-tier `alloc.json` catalogue produced by
`CrustifyAllocAnalyzer`. This file is the single source of field semantics.

Each `## <field>` section documents one top-level key or nested record shape; the
heading name is the field key. The three top-level keys are `families`,
`refcounts`, `locks`.

The catalogue records a codebase's **untyped allocator surface**, organised BY
DEALLOCATOR FAMILY: each `families` entry is ONE free/deallocator plus the flat
list of routines whose output it releases.

**Scope.** Only untyped allocators are catalogued -- byte-level producers
returning `void *` (raw bytes) and string producers returning `char *` /
single-byte scalar pointers. An allocator parameterised by or returning a named
aggregate type is a TYPE CONSTRUCTOR owned by the type-lifecycle / wrapper
analysis, NOT catalogued here; a typed disposer likewise does not anchor a
family. Every in-scope buffer-producer is one `allocators[]` entry -- a plain
allocation, a resize (`realloc`, a `(ptr, size)` signature), or a duplicator
(`*strdup` / `*memdup`) alike -- with the operation read from its `type`
signature and qualifier flags, NOT an explicit `op` tag.

**Downstream.** The buffer pass of `CrustifyTypeAnalyzer` (synthesizes
`string` / `array` clusters), the strings/arrays type-wrapper prompts, and
lifecycle classification (recognising refcount and lock fields). Template values
are GENERIC (libc plus a fictional `proj_*` layer) illustrating every archetype;
a real run substitutes the target's symbols, one family per deallocator.

## syms_base

Every primitive in the catalogue -- each allocator, `free`, copy, refcount op,
lock op, and the refcount / lock C types themselves -- is recorded as the same
six-field **base shape**: `{name, kind, linked_in, declared_in[], defined_in,
type}`.

- **`name`** -- the C identifier exactly as written at the definition site.
- **`kind`** -- the symbol's category, one of
  `function_{exported,static,inline_header,inline_tu}`, `macro`,
  `global_{static,extern}`, or `null`:
  - **`function_exported`** -- external linkage, callable across TUs and directly
    bindable from Rust.
  - **`function_static`** -- a TU-local (`static`) definition.
  - **`function_inline_header`** -- `static inline` defined in a header. There is
    no linkable symbol, so Rust reaches it only through a generated C wrapper
    (`wrapper_inlines.c` or equivalent). Refcount primitives are typically this.
  - **`function_inline_tu`** -- an inline definition confined to one `.c`.
  - **`macro`** -- a `#define`. Rare here: an allocator, dup, refcount or lock
    macro is RESOLVED to the function symbol it expands to (see
    [`macro_variants`](#macro_variants)), so a macro `kind` survives only for an
    entry with no underlying function -- e.g. a refcount `assert` guard.
  - **`global_static`** / **`global_extern`** -- object symbols, TU-local and
    external respectively.
  - **`null`** -- the entry is not classifiable as a symbol kind: a plain typedef
    or a struct declaration. This is the normal value for the `type` records under
    [`refcounts`](#refcounts) and [`locks`](#locks).
- **`linked_in`** -- reserved for link-time attribution; composer-emitted `null`
  today.
- **`declared_in`** -- the sorted list of header files that declare or export the
  primitive. Always a list, even for a single header.
- **`defined_in`** -- the single file holding the definition: `.c` for exported
  functions and globals, `.h` for inline functions and macros. `null` for external
  symbols the target only declares (libc, pthread, ...), which still carry a
  populated `declared_in`.
- **`type`** -- the full C signature for functions, the declared C type for globals
  and for the `refcounts` / `locks` type records, and `null` when the primitive has
  no type (e.g. a bare `assert` macro).

Downstream agents resolve any primitive against the per-stem syms tree by walking
`<repo_root>/.crustify/analysis/<defined_in-stem>/syms.json` (or
`<declared_in[0]-stem>/syms.json` when `defined_in` is null).

## macro_variants

A user-facing macro (e.g. `PROJ_malloc(n)` expanding to `proj_malloc(n,
__FILE__, __LINE__)`) is RESOLVED to the underlying function symbol it expands
to -- there is no separate macro entry and no `expands_to` field. Rust FFI never
calls macros, so the catalogue records only the callable function/global
symbols. Resolve every allocator / dup / refcount / lock macro to its underlying
primitive before emitting.

## families

One entry per deallocator/`free`, which MUST be an untyped free (releasing a
`void *` raw buffer -- e.g. `free`, `CRYPTO_free`, `git__free`). A typed disposer
taking a named struct (`git_str_dispose`, `foo_free(foo *)`) marks a TYPE and is
out of scope here (the type-lifecycle / wrapper analysis owns it).

- **`family`** -- free-form grouping string.
- **`free`** -- the syms-base entry for THE deallocator that anchors the family.
- **`allocators`** -- the flat list of every routine producing a buffer this
  `free` releases (alloc / resize / dup alike). See [`allocator_entry`](#allocator_entry).
- **`copies`** -- the non-allocating copy primitives that write INTO this
  family's buffers. See [`copies`](#copies).

Families come in two flavours, both discriminated by their `free`: BYTE-LEVEL
(allocators return `void *` raw bytes) and STRING (allocators return `char *` /
single-byte scalar pointers over NUL-terminated strings, not aggregate types).

A cleansing or secure deallocator (`*_clear_free`, `*_secure_free`) is a
SEPARATE family from the plain free even when they share an allocator (e.g.
`proj_zalloc` released by both `proj_free` and `proj_clear_free`): the wrapper's
drop strategy is monomorphic in the C free it calls, so a different `free` symbol
means a different `CVec<_, S>` type. The cleansing itself is done C-side by the
`*_clear_free`; the porter just binds the family's `free` (whose `(ptr, size_t)`
signature carries the length). The SAME allocator may therefore appear under
MORE THAN ONE family.

Archetypes the template illustrates: `libc` (the universal `free` with
`malloc`/`calloc`/`realloc` as peers), `proj` (a project wrapper layer anchored
on `proj_free`, one flat list of plain/zeroing/array/aligned allocs, both
reallocs, and the string/mem duplicators), `proj_clear` (zero-on-free drop
semantics for secrets; shares `proj_zalloc` with `proj`), and `proj_secure`
(locked-pages region -- a distinct family even when it falls back to the primary
allocator internally).

## allocator_entry

An `allocators[]` element -- ONE routine producing a buffer owned by this family
(released by its `free`). Its signature MUST be untyped: every pointer in the
params and the return is a raw byte/string pointer (`void *`, `char *`, `unsigned
char *`, `const char *`, `void **`), never a named-struct pointer -- an allocator
that takes or returns a typed object is a type constructor, catalogued by the
type analysis, not here.

Each entry IS a syms-base record (see [`syms_base`](#syms_base)) EXTENDED with
allocator qualifier flags:

- **`zeroing`** -- result is zero-filled (`zalloc` / `calloc`).
- **`sized`** -- takes `(count, size)`, overflow-checked (`*_array` / `calloc`).
- **`aligned`** -- honours an alignment argument.
- **`string`** -- duplicates a NUL-terminated string.
- **`bounded`** -- bounded copy that need not NUL-terminate.

The operation -- plain alloc vs resize (`realloc`, a `(ptr, size)` signature) vs
duplicator (`*strdup` / `*memdup`, a source-to-copy signature) -- is read from the
routine's `type` and these flags; there is no separate `op` tag. Macro variants
are resolved to the underlying symbol.

## copies

A family `copies[]` element -- a non-allocating copy primitive used to fill this
family's buffers (the caller owns the dest; nothing is allocated or freed). Each
is a syms-base record plus the `string` / `sized` / `bounded` flags.

The buffer pass uses these to recognise copy-INTO-existing-buffer call sites (no
cluster is synthesized); the safe Rust shim is a `CCell`-parametric `mem_copy`
over shared interior-mutable borrows, not an owned constructor. A copy primitive
may be listed under multiple families -- every family whose buffers it writes
into -- exactly like a shared allocator.

## refcounts

One cluster per (refcount type, primitive family).

- **`note`** -- free-form prose describing the refcount discipline.
- **`atomic`** -- the backend uses hardware atomics on the common platform.
- **`has_lock_fallback`** -- a non-atomic backend uses a lock.
- **`fallback_lock`** -- a name-reference into `locks[*].type.name` (e.g.
  `"PROJ_RWLOCK"`), or null.
- **`type`** -- the syms-base entry for the C type (struct or typedef
  declaration; `kind: null` is normal).
- **`field_layout`** -- `{field: <name>, type: <C type>}`, the in-struct integer
  field the primitives manipulate (a composer hint for the buffer/lifecycle
  agent, not a full type description).
- **`new` / `up` / `down` / `get` / `free`** -- the primitive set, each a
  syms-base entry; all required when present in the codebase.
- **`assert`** -- OPTIONAL sanity-check macro (e.g. an `ASSERT_NOT_NEGATIVE`-style
  guard) used to catch double-frees.

Primitives are typically `static inline` (`kind: function_inline_header`), so
Rust must call them through C wrappers (`wrapper_inlines.c` or equivalent).

## locks

One cluster per (lock type, primitive family).

- **`note`** -- free-form prose describing the lock and its backing implementation.
- **`kind`** -- one of `"rwlock"`, `"mutex"`, `"spinlock"`.
- **`reentrant`** -- recursive acquisition is allowed.
- **`opaque`** -- the type is opaque to callers (true for `typedef void *` style).
- **`type`** -- the syms-base entry for the lock type.
- **`new` / `read_lock` / `write_lock` / `unlock` / `free`** -- the primitives.
  `read_lock` is null when `kind != "rwlock"`; `write_lock` also acts as the
  single `lock` for non-rwlock kinds -- still named `write_lock` for schema
  uniformity.

The buffer/lifecycle agent resolves `refcounts[*].fallback_lock` references
against this list's `type.name` values.
