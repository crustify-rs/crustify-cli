# TODO

## Actually port lifecycle ops that are port-scope

Port-scope lifecycle ops (ctors / dtors / clones registered in `types.json`) are
currently in a **structural blind spot** — realized by neither stage:

- the **port** scheduler refuses them: *"a wrapped type's lifecycle method — the
  WRAP stage emits these as the type's `&self` methods; port must not re-port
  them … belongs to `wrap`"* (28/29 rejected via `port --dry-run --name`);
- the **wrap** stage, asked to wrap the owning type, plans `+Nf/0ops` — it emits
  fields/accessors but **~0 lifecycle ops** (there is no constructor mechanism),
  so it doesn't realize them either. (Only `git_odb` showed `2ops`.)

Result: **~29 port-scope lifecycle ops** (≈21 constructors + dtors of un-macro'd
types — khash maps, role-mislabeled refcounts — + the `git_vector_dup` clone)
sit as `// crustify:todo` stubs, reached only by raw `ffi::` / `crustify_*` shim
calls (`git_mwindow_open` ×6 direct, `pack_entry_find` ×6 via shim, …). 10 of the
29 are already wired live across the FFI seam; the other 19 are dormant.

Root cause (the through-line): the `define_*!` family binds only the destruction
side (`impl_freed!`/`impl_cvalued!` → dtor, `impl_ref_counted!` → ref,
`impl_cloned!` → clone). **There is no constructor macro**, so constructors have
nothing to enumerate them in either stage.

**Two paths:**
- **(a) Extend the wrap stage** with a constructor convention (`from_*`
  generation / an `impl_ctor!`) so `0ops` → `Nops` and re-wrapping the ~18 owning
  types realizes the class in one tooling change. *Leveraged fix.*
- **(b) Let port treat port-scope lifecycle ops as free functions** — translate
  each body as a standalone `fn` (the framing of this item), bypassing the
  wrap-method guard for the port-scope subset.

**Blast radius:** localized — ~18 owning types / 13 files, with a shallow
dependency front (31 first-layer wrap-type deps, nearly all already realized).
Not a sprawling closure; the only hard part is the missing constructor pathway.

## Treat mmap-like resource acquirers as byte-level allocators (alloc stage)

The alloc stage (`alloc.md` analyzer → `alloc.json`) currently catalogues only
the malloc-family byte allocators (malloc/calloc/realloc/strdup + the `git__*`
and backend variants). Resource acquirers that hand back **owned memory needing a
non-`free` releaser** — chiefly `p_mmap` ↔ `p_munmap` (and the
`git_futils_mmap_*` wrappers over them) — aren't catalogued. Under the byte-level
allocator ctor definition this leaves resource-owning types ctor-less: e.g.
`git_map` resolves to **zero ctors** while its dtor (`p_munmap`) is real.

Teach `alloc.md` to recognize a second allocator shape — a routine that maps/opens
an owned resource and pairs with a dedicated releaser (mmap↔munmap) — and emit it
as its own allocator family. **Interim:** a `posix_mmap` family (`p_mmap` /
`p_munmap`) was added to `<repo>/crustify/alloc.json` by hand.

Related: a ctor detector built on this definition must read the **CodeQL
`function_calls.csv`** for the "calls a byte-level allocator" edge, **not** the
composed `depends_on.syms` — the latter is empty by design for wrap-scope bodies
(`_wrap_additions_function` emits `syms: []`), so wrap-scope ctors like
`git_object__from_raw`, `git_vector_dup`, and the `git__strdup` family would
otherwise be missed.
