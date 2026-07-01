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

### Progress + the remaining ~35 (2026-07-01)

Path **(b)** was executed for a **15-op subset** across 9 layer-ordered `port
--name` waves (L1..L22), each green on cargo check/clippy/test + both C-matrix
ctest variants: `git_indexer_options_init`, `git_commit_graph_{entry_parent,
file_open,new,entry_find,open,writer_new}`, `git_midx_{open,writer_new}`,
`git_packbuilder_new`, `pack_entry_find(_prefix)`, `pack_backend__{writepack_free,
writepack,alloc}`. (9 candidates were correctly rejected wrap-scope by the port
gate — `git_str_init`, `git_pool_init`, `p_mmap`, `git_futils_mmap_ro(_file)`,
`git_zstream_init`, `git_vector_dup`, `git_filebuf_open`, `git_commit_lookup` —
they legitimately stay `ffi::`-forwarding.)

**That subset was NOT the whole port-scope class.** The candidate set was derived
ad hoc (the prior session's demoted forwarders + a hand-picked stub list), so it
missed every pending op that was a *pure stub* (no forwarding `fn` to grep) and
not hand-listed — e.g. `packed_commit_new`. The **authoritative** pending set is:

```
scope.json port.functions
  ∩ { SYM : `// Replaces: SYM` + `// crustify:todo`, and no realized `fn SYM(` in the tree }
  − { SYM bound as a dtor/clone hook inside impl_freed!/impl_cvalued!/impl_ref_counted!/impl_cloned! }
```

As of 2026-07-01 that yields **35 genuinely-pending port-scope ops** (51 pending
fn-stubs − 16 macro-realized dtors whose `todo` anchors are merely stale):

- **git_odb core:** `git_odb_new`, `git_odb_new_ext`, `git_odb_open`,
  `git_odb_open_ext`, `git_odb_open_rstream`, `git_odb_open_wstream`,
  `git_odb_write_pack`, `git_odb_object_dup`, `git_odb_object_free`,
  `git_odb_object__free`
- **odb cache (khash):** `git_cache_init`, `git_cache_dispose`,
  `git_cache_oidmap_dispose`, `free_cache_object`, `retrieve_object`,
  `lookup_walk_object`
- **loose backend streams:** `loose_backend__readstream`,
  `loose_backend__writestream`, `init_fake_wstream`
- **packfile / packbuilder:** `git_packfile_alloc`, `git_packfile_stream_open`,
  `git_pack_entry_find`, `git_packbuilder_pobjectmap_dispose`,
  `git_packbuilder_walk_objectmap_dispose`
- **midx / commit_graph:** `git_midx_close`, `git_commit_graph_file_close`,
  `git_commit_graph_entry_get_byindex`, `packed_commit_new`, `packed_commit_free`
- **mwindow:** `git_mwindow_get_pack`, `git_mwindow_free_all`,
  `new_window_locked` ⚠️
- **indexer / oidarray:** `git_indexer_oidmap_dispose`,
  `git_oidarray__from_array`, `git_oidarray_dispose`

**Caveats before porting:**
- ⚠️ `new_window_locked` is **intentionally kept in C** (the `git_mwindow`
  owner-mediated teardown is incompatible with the crate's dtor primitives —
  it has the unconditional `..._new_window_locked_shim`). **Exclude it.**
- `packed_commit_free` — verify it is not already `Drop`-covered by the one-arg
  `impl_freed!(PackedCommit)` before porting (the macro-scan above only catches
  symbols named inside the macro args, not the one-arg inherent-`free` form).
- `git_commit_graph_entry_get_byindex`, `git_pack_entry_find`,
  `git_mwindow_get_pack` are non-ctor `function_static` helpers reached via the
  `crustify_<file>__<name>` shim — proof that "the regular free fns are already
  ported" had holes; the khash `*_oidmap_dispose` / `*pobjectmap_dispose` ops are
  the "un-macro'd map dtors" this section already predicted.

**To finish:** run the enumeration above, scope-recheck each (drop wrap-scope +
`new_window_locked`), then layer-ordered `port --name` waves — same recipe as the
15-op run. Every wave then also needs the two janitorial fixes below (they recur):
the scaffold re-adds stale `// Replaces:`+`todo` anchors for already-native syms
(agents don't promote `// Replaces:` → `/// Replaces:`, so the scaffold thinks the
anchor is missing), and call-site migration off the `crustify_*` shims is gated on
porting the C consumers (see the callback-gate note below).

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
