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

## Wrap callback gate misses call-site-reached callbacks (raw `ffi::` escapes)

Callback typedefs the port reaches show up as naked `ffi::<cb>` with "no wrapper
yet" doc-flags — `git_treewalk_cb` (passed to `git_tree_walk` in
`pack_objects.rs:2468`), likely `git__tsort_cmp` (`git__tsort`). They are in
`syms.json` + the dag but **absent from `scope.json`** (neither `port.functions`
nor `wrap.functions`), so the wrap scheduler never sees them and no safe callback
wrapper is emitted — violating the "avoid `ffi::`, use safe wrappers" discipline.

The machinery is NOT the gap: callbacks are `subkind=="callback"` sym-units
(`_schedule.py:199`), routed to `symbol_wrapper.md`'s callback section
(`wrap.py:38`), and `wrap_closure.py:431-449` has a dedicated callback path. The
gap is the **reachability gate**: it admits a callback only when an in-scope
function's **signature** mentions it. These callbacks are reached through a port
**call site** — the fn-pointer argument actually passed to `git_tree_walk` /
`git__tsort` — not an in-scope signature, so the gate misses them. Exactly the
type-gate's old body-usage blind spot (`git_config_entry` / `git_error`), already
fixed for types but not for callbacks.

**Fix:** extend the callback reachability gate to admit callbacks passed at port
**call sites** (the fn-ptr argument), not just in-scope signature mentions, so
they land in `scope.json` → get scheduled → wrapped. The fix is in the gate
(`wrap_closure.py` callback walk / `scope_manifest` callback collection), **not**
the wrap scheduler (which already handles callbacks correctly).

**Adjacent (distinct, don't conflate):** the same per-type-gate-vs-wrap-surface
divergence also yields dead raw `ffi::` via **over-emission** — `git_signature`
(`GitTag::tagger()`, `GitCommit::author()`/`committer()`) and similar field
accessors expose un-admitted field-types but have **zero callers**. Those are
dead over-emission (delete the accessors), the opposite of the callback gap
(wrap them). Discriminator: **does the emitting accessor/wrapper have a live
caller?** No → over-emission, remove; yes → gate gap, wrap.
