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

## Retire `config.json` (fold into `scope.json` + `build.json`)

`config.json` is *not* redundant, but two of its fields duplicate the CLI args
and the rest could live closer to where it's consumed:

- `repo_root` -- DEAD. Never read; `repo_root` is the CLI's first positional
  (pinned via `set_repo_root`, `layout.py`). Drop it (the on-disk value already
  drifted stale -- some targets point at a `/root/git/openssl` that no longer
  exists).
- `target` -- CLI-duplicate, but currently *read* at `scope_manifest.py:96`
  (`config["target"]` -> `target_dir`). Rewire that to take the target from the
  pinned layout / the `targets/<target>/` dir path, then drop it.
- `port_files` + `out_of_scope.paths` -- authored *scope* inputs. Could move into
  an authored section of `scope.json` -- but `scope.json` is a computed output
  (`analyze scope` regenerates it), so the composer must **merge-preserve** the
  authored section on every regen (input + output share one file: a regen bug
  can clobber the authored scope). Weigh against keeping a tiny scope-def file.
- `out_of_scope.features` (OPENSSL_NO_* preproc defines) + `version_anchor` --
  build/preprocessor inputs, not scope. Move to `build.json`.

Low-risk first step: delete the dead `repo_root` field + rewire `target` off the
CLI. The scope.json/build.json fold is the larger, riskier change.

## Global-variable wrapping strategy (const alias + guarded-handle for mutable)

Today wrap-scope globals get a getter accessor (`pub fn <name>()` over
`ffi::crustify_get_<NAME>()`, symbol_wrapper.md). Formalize two paths and (likely)
back them with a crate primitive:

- **Const / immutable-after-init global** -> a thin **safe read alias/accessor**
  over the `ffi::` binding: `pub fn <name>() -> &'static T` (or a value copy).
  The one `unsafe` read (an `extern static` read is unsafe even when non-`mut`)
  is sound *because* the global is const + never mutated. This is the common
  case -- e.g. all 9 statem in-scope globals are const (`tls11downgrade`,
  version tables, ASN.1 templates), 0 `pub static mut` in any bindings.rs.

- **Mutable exported global** -> an **RAII guarded-handle** accessor: take a lock
  on access, return an owned handle that `Deref`/`DerefMut`s to the value and
  releases the lock on `Drop`.

  **CAVEAT (load-bearing):** a Rust-side `std::sync::Mutex` only serializes
  *Rust* callers. If C code or other FFI consumers touch the same global without
  taking that lock, the guard is a **false sense of safety** -- no real mutual
  exclusion. In OpenSSL these globals are already guarded by `CRYPTO_THREAD_*`
  locks, so the wrapper must **bridge to the existing C lock** (acquire it across
  FFI), not invent a fresh Rust mutex -- unless we can guarantee *all* access
  goes through Rust.

Forward-looking: no mutable exported global exists in the current statem scope,
so this is a primitive to have ready, not a live blocker. Cross-ref the
mutable-global gap noted for `DRIFTS.md`.

## `CVec` / `CrustifyStr` `Clone` -- primitives done; wrapper opt-in + deep clone pending

DONE (crate, `crustify-crate/src/smart_pointers.rs`): both now have a
**conditional** `Clone` gated on the strategy registering a copy -- a free-only
strategy is deliberately not `Clone` (a `.clone()` is a compile error, never a
silent shallow double-freeing copy; the types are never `#[derive(Clone)]`).

- **`CrustifyStr<D>`** reuses **`CCloned`** directly: a NUL string's copy is
  `strdup`-shaped (`c_clone(ptr) -> ptr`, length recovered by `strlen`), so
  `impl<D: CCloned> Clone`.
- **`CVec<T, S>`** needed a new length-aware **`CLenCloned`**
  (`clone_len(ptr, byte_len) -> ptr`) -- the analogue of `CLenFreed` vs `CFreed`,
  because a `memdup` needs the byte length that `CCloned::c_clone` cannot carry.
  `impl<T, S: CLenCloned> Clone`.

Both derive a fallible `try_clone` (mirrors `CBox::try_clone`) and an infallible
`Clone` that `abort()`s on the C-copy-failed (`None`) case.

REMAINING:
- **Analyzer classification (`buffer_analyzer.md`).** A `*_memdup` / `*_strdup` /
  `*_strndup` allocates AND copies a source -- it is a **clone**, not a plain
  alloc, so it belongs in a cluster's `clones`, not `allocs`. The analyzer files
  them under `allocs`, leaving every cluster's `clones` empty, so the wrapper has
  no clone to register. Fold the rule into `buffer_analyzer.md`. (Reclassified
  manually on-disk 2026-07-03 for the 6 openssl clusters -- `memdup` -> clones on
  the two `*_free`/`*_clear_free` byte families, `strdup`/`strndup` -> clones on
  the two string families; `secure_*` have no dup, so no clone.)
- **Wrapper opt-in.** A cluster wrapper still registers the copy on its strategy
  ZST: `impl_cloned!` on the string strategy; a `CLenCloned` impl naming the
  family `*_memdup` on the buffer strategy. strings_wrapper.md / arrays_wrapper.md
  should emit it when the port clones that family.
- **Deep (per-element) clone.** `CLenCloned` is a **byte** copy -- POD elements
  only. An `owned_elem` buffer (elements own pointers) needs a per-element
  `T: CCloned` deep clone, not yet modeled.

## Migrate libgit2 consumer to the `*mut T::C` pointer seam (2026-07-06)

The crate's owning pointer seam now speaks the **raw ffi type** instead of the
wrapper type: `CBox` / `CArc` / `CUniqueArc` `as_ptr` / `from_raw` / `into_raw`
changed from `*mut T` to `*mut T::C` (via the enriched `CCell: type C`). This
makes C interop cast-free (`CBox::from_raw(ffi::X_new())`, `ffi::X_free(b.into_raw())`).

DONE: crate (builds + all tests) and **openssl-crustify** (clean, zero fixes --
its `from_raw` calls all have typed context so `T` infers).

REMAINING -- **libgit2** (`crustify/rust/`) has ~86 sites to migrate. `from_raw`
now takes `*mut T::C`; `T::C` is a non-injective projection so `T` must come from
context (return type / binding) or a turbofish. Two mechanical shapes:

- **`X::from_raw(p.cast::<W>())` -> `X::<W>::from_raw(p.cast())`** (uniform: keeps
  the wrapper as the `from_raw` turbofish, retargets the arg cast to `W::C`). Where
  `p` is *already* `*mut ffi::c_type` (the common case -- from a C alloc/fn) the
  cast **deletes** entirely: `from_raw(p.cast::<W>()) -> from_raw(p)`. So the
  change is mostly a net *cleanup* (strips wrapper-cast boilerplate).
- **Context-free `from_raw`** (`let _ = ...`, `drop(...)`) additionally needs the
  turbofish supplied (~14 sites): `CArc::from_raw(x.cast::<W>()) ->
  CArc::<W>::from_raw(x.cast())`.

Files: `odb/{odb_pack,odb,cache,oid,odb_backend_api}.rs`, `util/alloc.rs`,
`pack/{indexer,midx_h,commit_graph_h}.rs`, `object/object_h.rs`. Run the regex,
compile-verify stragglers (a few `as *mut W` variants), then optionally strip the
now-identity `.cast()`s where the source is already `ffi::`.

Alternative if the churn isn't wanted: keep `from_raw: *mut T` (only
`as_ptr`/`into_raw` -> `*mut T::C`) -- no inference cost, libgit2 breakage ~0, but
loses cast-free *adopt-from-C* (keeps cast-free *hand-to-C*).
