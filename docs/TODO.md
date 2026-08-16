# TODO

Known limitations, deferred work, and design questions parked for later, plus
the active near-term work. Each entry should describe the problem clearly
enough that someone returning to it months later can pick it up without
re-discovering the context. Merged from the former TODO.md + TODOS.md
(2026-07-07). Dated tags (`YYYY-MM-DD`) preserve when an item was surfaced;
completed work is archived at the bottom.

---

**Active** -- current near-term focus.

## Actually port lifecycle ops that are port-scope

Port-scope lifecycle ops (ctors / dtors / clones registered in `types.json`) are
currently in a **structural blind spot** -- realized by neither stage:

- the **port** scheduler refuses them: *"a wrapped type's lifecycle method -- the
  WRAP stage emits these as the type's `&self` methods; port must not re-port
  them ... belongs to `wrap`"* (28/29 rejected via `port --dry-run --name`);
- the **wrap** stage, asked to wrap the owning type, plans `+Nf/0ops` -- it emits
  fields/accessors but **~0 lifecycle ops** (there is no constructor mechanism),
  so it doesn't realize them either. (Only `git_odb` showed `2ops`.)

Result: **~29 port-scope lifecycle ops** (~21 constructors + dtors of un-macro'd
types -- khash maps, role-mislabeled refcounts -- + the `git_vector_dup` clone)
sit as `// crustify:todo` stubs, reached only by raw `ffi::` / `crustify_*` shim
calls (`git_mwindow_open` x6 direct, `pack_entry_find` x6 via shim, ...). 10 of the
29 are already wired live across the FFI seam; the other 19 are dormant.

Root cause (the through-line): the `define_*!` family binds only the destruction
side (`impl_freed!`/`impl_cvalued!` -> dtor, `impl_ref_counted!` -> ref,
`impl_cloned!` -> clone). **There is no constructor macro**, so constructors have
nothing to enumerate them in either stage.

**Two paths:**
- **(a) Extend the wrap stage** with a constructor convention (`from_*`
  generation / an `impl_ctor!`) so `0ops` -> `Nops` and re-wrapping the ~18 owning
  types realizes the class in one tooling change. *Leveraged fix.*
- **(b) Let port treat port-scope lifecycle ops as free functions** -- translate
  each body as a standalone `fn` (the framing of this item), bypassing the
  wrap-method guard for the port-scope subset.

**Blast radius:** localized -- ~18 owning types / 13 files, with a shallow
dependency front (31 first-layer wrap-type deps, nearly all already realized).
Not a sprawling closure; the only hard part is the missing constructor pathway.

### Progress + the remaining ~35 (2026-07-01)

Path **(b)** was executed for a **15-op subset** across 9 layer-ordered `port
--name` waves (L1..L22), each green on cargo check/clippy/test + both C-matrix
ctest variants: `git_indexer_options_init`, `git_commit_graph_{entry_parent,
file_open,new,entry_find,open,writer_new}`, `git_midx_{open,writer_new}`,
`git_packbuilder_new`, `pack_entry_find(_prefix)`, `pack_backend__{writepack_free,
writepack,alloc}`. (9 candidates were correctly rejected wrap-scope by the port
gate -- `git_str_init`, `git_pool_init`, `p_mmap`, `git_futils_mmap_ro(_file)`,
`git_zstream_init`, `git_vector_dup`, `git_filebuf_open`, `git_commit_lookup` --
they legitimately stay `ffi::`-forwarding.)

**That subset was NOT the whole port-scope class.** The candidate set was derived
ad hoc (the prior session's demoted forwarders + a hand-picked stub list), so it
missed every pending op that was a *pure stub* (no forwarding `fn` to grep) and
not hand-listed -- e.g. `packed_commit_new`. The **authoritative** pending set is:

```
scope.json port.functions
   intersect  { SYM : `// Replaces: SYM` + `// crustify:todo`, and no realized `fn SYM(` in the tree }
  - { SYM bound as a dtor/clone hook inside impl_freed!/impl_cvalued!/impl_ref_counted!/impl_cloned! }
```

As of 2026-07-01 that yields **35 genuinely-pending port-scope ops** (51 pending
fn-stubs - 16 macro-realized dtors whose `todo` anchors are merely stale):

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
  `new_window_locked` (!)
- **indexer / oidarray:** `git_indexer_oidmap_dispose`,
  `git_oidarray__from_array`, `git_oidarray_dispose`

**Caveats before porting:**
- (!) `new_window_locked` is **intentionally kept in C** (the `git_mwindow`
  owner-mediated teardown is incompatible with the crate's dtor primitives --
  it has the unconditional `..._new_window_locked_shim`). **Exclude it.**
- `packed_commit_free` -- verify it is not already `Drop`-covered by the one-arg
  `impl_freed!(PackedCommit)` before porting (the macro-scan above only catches
  symbols named inside the macro args, not the one-arg inherent-`free` form).
- `git_commit_graph_entry_get_byindex`, `git_pack_entry_find`,
  `git_mwindow_get_pack` are non-ctor `function_static` helpers reached via the
  `crustify_<file>__<name>` shim -- proof that "the regular free fns are already
  ported" had holes; the khash `*_oidmap_dispose` / `*pobjectmap_dispose` ops are
  the "un-macro'd map dtors" this section already predicted.

**To finish:** run the enumeration above, scope-recheck each (drop wrap-scope +
`new_window_locked`), then layer-ordered `port --name` waves -- same recipe as the
15-op run. Every wave then also needs the two janitorial fixes below (they recur):
the scaffold re-adds stale `// Replaces:`+`todo` anchors for already-native syms
(agents don't promote `// Replaces:` -> `/// Replaces:`, so the scaffold thinks the
anchor is missing), and call-site migration off the `crustify_*` shims is gated on
porting the C consumers (see the callback-gate note below).

---

## Relax the blanket interior-mutability representation (2026-08-14)

`CType<T> = UnsafeCell<MaybeUninit<T>> + PhantomPinned` is applied to every
wrapped type. It is the correct *floor*, but it is currently also the ceiling:
every type pays the union of four separate taxes whether or not the hazard each
answers is present.

**The four properties are orthogonal, and the taxonomy must not fuse them.**

| property | mechanism | decided by |
|---|---|---|
| C may access concurrently | `UnsafeCell` | escape closure + thread-sharing |
| memory may be uninitialized | `MaybeUninit` | constructor state |
| C holds the ADDRESS | `PhantomPinned` | escape closure |
| may form `&mut Self` | -- (follows from aliasing) | whole-object, not exemptible |

Init-ness is independent of aliasing: a fully-constructed object C still writes
to needs the cell but not `MaybeUninit`; a half-built object Rust exclusively
owns needs the reverse. So init is a STATE, not a family of its own.

**What is and is not UB.** Holding an aliasing pointer is not UB on its own --
the UB is a conflicting ACCESS during the reference's lifetime (Stacked/Tree
Borrows retag, or `noalias` violated at the LLVM level). In single-threaded
code with no intervening call into C that reaches the object, control flow
alone closes the window. That is a real licence, not a heuristic.

The predicate is therefore REACHABILITY, never call count: zero intervening
calls can still be unsound if an EARLIER call stored the pointer
(`SSL_set_bio`, `BIO_push`, any callback registration taking `void *arg`), and
five hundred are fine if none escape. Decidable per METHOD from
`depends_on.syms` plus the callback nodes: does the method's transitive callee
set intersect the functions that touch `self`?

**Threads are the hard limit.** Once an object is shared across threads the
window never closes and no local analysis helps. Signals for it already exist
per field: `locked_by`, `refcount`. Refcount is the SHARING signal, not the
threading one, and it is neither necessary nor sufficient -- `SSL_CTX` hands
out pointers to its `CERT` and session cache, which are shared by containment
without being refcounted.

**Granularity.** `UnsafeCell` is per-FIELD: `&Struct` grants immutability over
every byte except those inside a cell. `&mut Struct` is whole-object and NOT
exemptible -- one concurrently-mutated field means `&mut Self` is illegal for
that type forever. But `&mut *addr_of_mut!((*p).f)` covers only that field's
bytes, so private fields can still get real `&mut` accessors (`split_at_mut`
reasoning). The loss is confined to the object, not its fields.

The refcount field itself stays off-limits regardless: C bumps it atomically or
under a lock, so a plain Rust read/write of those bytes is a DATA RACE, which
`UnsafeCell` does not address. Delegate to `X_up_ref` / `X_free`, or expose it
as a real atomic.

### DONE -- handles instead of references (2026-08-15)

Implemented in `crustify-prim` on branch `ref`. Per-field cells stay open,
below.

The representation question dissolves once no reference to a wrapped C object is
formed: there is nothing for `UnsafeCell` to suppress and nothing asserting
validity, so `CType<T>` is `T + PhantomPinned` and no more. Each C type gets
three Rust types:

```rust
Foo          // repr(transparent) over CType<ffi::foo> -- the C layout, embeds
             // by value in a #[repr(C)] mirror, is what CBox points at
FooRef<'a>   // one pointer, Copy -- the getters
FooMut<'a>   // one pointer, Deref to FooRef -- the setters
```

`&FooRef` covers the HANDLE, one pointer of Rust-owned stack, so `&self` /
`&mut self` methods are sound on it and `&mut FooRef` reborrows implicitly the
way `&mut T` does. Field access projects a raw pointer out of the handle and
goes through `addr_of!` / `addr_of_mut!`.

`CCell` is a linking trait: `type C` plus `type Ref<'a>` / `type Mut<'a>` and
their constructors, all bounded `Self: 'a`. Owning handles hand out handles --
`as_ref()` / `as_mut()` rather than `Deref`, since `Deref::Target` cannot name a
lifetime taken from `&self`.

**What this costs.** `noalias` / `readonly` on the C object are unreachable for
every type, permanently -- the same ceiling ffmpeg, libgit2 and rust-openssl
hit. Reaching past it needs a reference over the object's bytes, which needs the
escape closure below, and the header measurement put the reachable set at ~25
leaf types per library (crypto contexts, key schedules, arithmetic scratch) --
none of them the types an API is built around.

**What it buys.** Soundness by construction on every type, with no analysis:
`&mut` ergonomics on day one, and an audit rule that is pure syntax
(`ref_to_type_wrapper`, target 0) rather than a per-type judgement.

**Measured against the alternatives.** A ZST borrowed type at the object's
address (the `foreign-types` shape) is rejected by Stacked Borrows: the retag
covers `[0x0..0x0]`, so a pointer cast out of it carries no provenance for the
object's bytes. Tree Borrows accepts it. Holding the pointer by value is clean
under both.

**Costs to price before committing (per-field cells).**

- Per-field cells require emitting a `#[repr(C)]` MIRROR struct with a layout
  assertion, not wrapping bindgen's opaque `ffi::T` as today. Highest value,
  by far the most work, and where layout drift would bite. Separate step.
- Every representation multiplies against the owning handles (`CBox`, `CVal`,
  `CVec`, `CDropped`, `CCloned`). Few types with explicit conversions, not
  parallel families.
- The escape closure has to be TRANSITIVE and is not recorded that way today:
  the store has per-argument `borrowed: {lifetime}`, but not "is this type
  reachable from a shared parent". That analysis is the prerequisite for any
  of this being safe to switch on.

**Scoreboard.** `audit --all` on `ssl` today: 2,531 unsafe LoC, 93.5% inside
`impl` blocks, 7.39% of 34,235 code lines. Any relaxation should move the first
two down without moving `field_proj_outside_impl` or `ref_to_type_wrapper` off
zero.

---

## Harden `crates.json` validation (2026-07-29)

`crates.validate()` checks three things: `(kind, name, tu)` uniqueness, the
`depends_on` DAG, and that every `depends_on` names a defined crate. Everything
else in `docs/schemas/crates.md` is documented but unenforced -- and the file is
hand-authored, so an invariant nobody checks is one that silently rots.

Two real bugs shipped past a clean `--validate` while authoring the `ssl/`
oracle, both in `rust_path`:

- module `core` claimed `rust_path: "src/core"` while its `.rs` sat at
  `src/*.rs` (no such dir on disk);
- module `record` spans `ssl/record/` and `ssl/record/methods/`, and `rust_path`
  was taken from each file's own dir, so the last one written won -- leaving
  four `.rs` above the recorded root.

Neither failed loudly because **nothing reads `rust_path`** (it appears only in
a `crates.py` docstring). That is the worst case: wrong data, no signal.

Candidate checks, cheapest first:

- every `rs` key starts with its module's `rust_path` (catches both bugs above);
- `tu` ends in `.c`; every `headers` entry ends in `.h`/`.hpp` (a `.c` can
  legitimately appear in an entity's `declared_in` -- a file-local forward
  declaration -- and must not leak into `headers`);
- no member name is anonymous (`(unnamed …)`) or empty;
- each `rs` key is unique across the WHOLE file, not just within its module;
- `tu` is unique across the file (two `.rs` mirroring one TU is a split module);
- optionally, against the analysis tree: every member exists in `scope.json`,
  and every `tu`/`headers` path exists on disk.

The last group needs a target (scope.json is per-target) while `crates.json` is
target-agnostic, so it belongs behind a flag rather than in the default gate.

---

## Use cbindgen for the C-side re-export declarations instead of the port agent (2026-07-28)

`port.md` currently makes the agent hand-write **both** halves of the re-export
seam:

- **Sec 5** -- the `#[no_mangle]` body in `mod ffi_export`, which reconstructs
  wrappers from raw params and delegates to the idiomatic `pub(crate) fn`;
- **Sec 6a** -- the C `#else` branch: an `extern` declaration for every ported
  symbol, plus the `#define <name> crustify_<file>__<name>` redirect for the
  TU-local kinds.

Sec 6a is pure transcription -- the C signature already exists, and the agent is
copying it into a declaration that must match the Rust `#[no_mangle]` item
exactly or the link is silently ABI-wrong. That is bookkeeping, not judgement,
and by the pipeline's own split it belongs in a deterministic composer.
`cbindgen` does exactly this: it parses Rust source (via `syn`, no rustc) and
emits a C header declaring the crate's `#[no_mangle] extern "C"` items and
`#[repr(C)]` types.

**What cbindgen can and cannot take over.** It generates *declarations for items
that are already exported* -- it does **not** synthesise the marshalling body.
So Sec 5 stays agent work (or becomes a macro; see the constructor-macro item
above): reconstructing a wrapper from a raw param is exactly the judgement
cbindgen has no basis for. Sec 6a is the part that moves.

**Three candidate uses, increasing ambition:**

1. **Verification oracle (cheapest, do first).** Keep the agent writing Sec 6a,
   run cbindgen over the port crate, and diff its output against what the agent
   wrote *and* against the original C declarations in the header. Any mismatch
   is an ABI bug the two-variant build matrix (Sec 7) may not catch -- a wrong
   pointer depth or a missing `const` links fine and corrupts at run time.
2. **Replace Sec 6a.** Emit the `#else` branch from cbindgen output. Needs the
   TU-local `crustify_<file>__<name>` naming to be expressible; `cbindgen.toml`
   has renaming rules, but whether they can key off our per-symbol `kind`
   (`function_static` / `function_inline_tu` / `global_static` vs the exported
   kinds) is unverified -- probably needs a post-pass over cbindgen's output
   rather than pure config.
3. **Header parity check for the whole seam.** Once (2) holds, cbindgen output
   *is* the contract between the C build and the port crate, so the
   `CRUSTIFY_<FILE>` switch can be validated structurally instead of only by
   building both variants.

**Open questions:**

- Does cbindgen handle the `#[repr(C)]` types our ported files re-export, or
  only the function signatures? Types crossing the seam by value would need it.
- Interaction with the per-file `mod ffi_export` layout: cbindgen works
  per-crate, our fencing is per-C-file. Emitting one header per crate and
  fencing per file may be fine, or may need `--config` per module.
- It parses syntactically, so macro-generated `#[no_mangle]` items (anything the
  `define_*!` family might grow) are invisible to it. Check before relying on it
  as an oracle -- a false "no drift" is worse than no check.
- Scope: this is the **port** stage only. The wrap stage's direction is C -> Rust
  and cbindgen has nothing to say about it.

---

## Wrap callback gate misses call-site-reached callbacks (raw `ffi::` escapes)

Callback typedefs the port reaches show up as naked `ffi::<cb>` with "no wrapper
yet" doc-flags -- `git_treewalk_cb` (passed to `git_tree_walk` in
`pack_objects.rs:2468`), likely `git__tsort_cmp` (`git__tsort`). They are in
`syms.json` + the dag but **absent from `scope.json`** (neither `port.functions`
nor `wrap.functions`), so the wrap scheduler never sees them and no safe callback
wrapper is emitted -- violating the "avoid `ffi::`, use safe wrappers" discipline.

The machinery is NOT the gap: callbacks are `subkind=="callback"` sym-units
(`_schedule.py:199`), routed to `symbol_wrapper.md`'s callback section
(`wrap.py:38`), and `wrap_closure.py:431-449` has a dedicated callback path. The
gap is the **reachability gate**: it admits a callback only when an in-scope
function's **signature** mentions it. These callbacks are reached through a port
**call site** -- the fn-pointer argument actually passed to `git_tree_walk` /
`git__tsort` -- not an in-scope signature, so the gate misses them. Exactly the
type-gate's old body-usage blind spot (`git_config_entry` / `git_error`), already
fixed for types but not for callbacks.

**Fix:** extend the callback reachability gate to admit callbacks passed at port
**call sites** (the fn-ptr argument), not just in-scope signature mentions, so
they land in `scope.json` -> get scheduled -> wrapped. The fix is in the gate
(`wrap_closure.py` callback walk / `scope_manifest` callback collection), **not**
the wrap scheduler (which already handles callbacks correctly).

**Adjacent (distinct, don't conflate):** the same per-type-gate-vs-wrap-surface
divergence also yields dead raw `ffi::` via **over-emission** -- `git_signature`
(`GitTag::tagger()`, `GitCommit::author()`/`committer()`) and similar field
accessors expose un-admitted field-types but have **zero callers**. Those are
dead over-emission (delete the accessors), the opposite of the callback gap
(wrap them). Discriminator: **does the emitting accessor/wrapper have a live
caller?** No -> over-emission, remove; yes -> gate gap, wrap.

---

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
- `out_of_scope.features` (OPENSSL_NO_* preproc defines) -- a build/preprocessor
  input, not scope. Move to `build.json`. (`version_anchor` was listed here too;
  it has since been deleted outright -- nothing read it.)

Low-risk first step: delete the dead `repo_root` field + rewire `target` off the
CLI. The scope.json/build.json fold is the larger, riskier change.

---

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

---

## `CVec` / `CrustifyStr` `Clone` -- primitives done; wrapper opt-in + deep clone pending

DONE (crate, `crustify-prim/src/owned_refs.rs`): both now have a
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
- **Deep (per-element) clone.** `CLenCloned` is a **byte** copy -- POD elements
  only. An `owned_elem` buffer (elements own pointers) needs a per-element
  `T: CCloned` deep clone, not yet modeled.

---

## Migrate libgit2 consumer to the `*mut T::C` pointer seam (2026-07-06)

The crate's owning pointer seam now speaks the **raw ffi type** instead of the
wrapper type: `CBox`'s `as_ptr` / `from_raw` / `into_raw`
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
  turbofish supplied (~14 sites): `CBox::from_raw(x.cast::<W>()) ->
  CBox::<W>::from_raw(x.cast())`.

SUPERSEDED IN PART (2026-07-29): `CArc` / `CUniqueArc` / `CArcWith` were dropped
from the crate; every refcounted site is now a `CBox` whose `c_free` is the
down-ref and whose `c_clone` is the `up_ref` (`impl_dropped!` +
`impl_cloned_upref!`). The libgit2 sites still need the `*mut T::C` migration
above, but the wrapper rename folds into the same pass.

Files: `odb/{odb_pack,odb,cache,oid,odb_backend_api}.rs`, `util/alloc.rs`,
`pack/{indexer,midx_h,commit_graph_h}.rs`, `object/object_h.rs`. Run the regex,
compile-verify stragglers (a few `as *mut W` variants), then optionally strip the
now-identity `.cast()`s where the source is already `ffi::`.

Alternative if the churn isn't wanted: keep `from_raw: *mut T` (only
`as_ptr`/`into_raw` -> `*mut T::C`) -- no inference cost, libgit2 breakage ~0, but
loses cast-free *adopt-from-C* (keeps cast-free *hand-to-C*).

## Analyze + wrap unions and enums (2026-07-08)

Unions and enums are composed as skeletons but never wrapped. (Historically
this was the type-analyzer worklist being structs-only; the analyzer is gone —
`analyze` is composer-only and the judgement fields are the wrapper's — so the
gap is now purely on the wrap side.)

- **Unions** carry the struct-like member-layout skeleton (see
  `docs/schemas/types.md` -> `kind`), but their lifecycle / `ptr` slots stay at
  defaults -- no ownership analysis, no accessor generation. A C union needs
  discriminant-aware access (which arm is live is a runtime invariant the
  enclosing struct usually tracks), so a safe wrapper is a real gap, not just
  missing boilerplate: raw `ffi::` union access is unchecked.
- **Enums** are emitted `kind:"enum"` with no fields / lifecycle; today they
  fall through to bindgen (plain Rust enum / integer consts). Wrapping could map
  them to a real Rust `enum` with the C repr for exhaustive matching instead of
  raw integer compares.

Shape if pursued: (1) extend the wrap worklist to include unions
(member-pointer ownership + the discriminant field when the union is embedded in
a tagged struct) and optionally enums (repr + variant mapping); (2) extend the
type wrapper to emit a Rust `union` / tagged-enum wrapper (discriminant-gated
accessors) and a `#[repr(C)]` enum. Until then port code touches union / enum
members raw.

Tracker: none.

---

## 2026-07-17 - `used_by` / `depends_on` are complete only within the extracted build config + call graph

The schemas claim `used_by` (and by symmetry `depends_on`) is *"COMPLETE:
every consumer tree-wide"* (`docs/schemas/{syms,types}.md`). It is complete only
**within the one preprocessor configuration the CodeQL DB was built under, and
only for statically-resolved call edges.** Two structural blind spots:

**(a) Conditional compilation.** A call inside an inactive `#ifdef` branch does
not exist in the compiled TU CodeQL extracts, so the edge is absent from both
`used_by` and `depends_on` (symmetric -- so it is NOT a composer bug; the
composer builds both directions from the same CodeQL call-edge relation).
Concrete: `EVP_PKEY_generate -> evp_pkey_free_legacy` (`crypto/evp/pmeth_gn.c`,
inside `#if !defined(FIPS_MODULE) && !defined(OPENSSL_NO_DEPRECATED_3_6)`) is
missing from `evp_pkey_free_legacy.used_by.call`, while its *unconditional*
caller `evp_pkey_free_it` IS captured -- proof the query works and the config is
the cause.

**(b) Vtable / function-pointer dispatch.** An indirect call through a method
table produces no static call edge, so `used_by.call` is empty for vtable-only
targets (`dtls1_free`/`tls1_free`/`ossl_quic_free`/`ossl_ssl_connection_free` via
`SSL_free`'s `s->method->ssl_free`; `dtls1_clear` via `SSL_clear`'s
`s->method->ssl_reset`). Empty `used_by` is then AMBIGUOUS -- it can mean
"dtor-only" (a `ssl_free` slot) or "reset" (a `ssl_clear` slot). Same root as the
`SSL_clear` discovery blind spot: `--calling` reachability can't cross a fn
pointer either.

**Why it matters.** The lifecycle subsumed-vs-standalone discriminant is
caller-based: "reached only via the dtor -> fold into `Drop`; reached from
re-init too -> a genuine manual `is_disposer`." An incomplete `used_by` (a) can
hide a re-init caller and wrongly mark a real disposer subsumed -- silently
dropping a manual method a FIPS/deprecated build needs; (b) can't disambiguate
the vtable case at all. It also under-reports the wrap surface (dispatched
teardowns invisible).

**Fixes.** (1) Resolve `SSL_METHOD`/`EVP_*`-style method-table assignments in a
CodeQL query and inject the indirect edges -- closes (b) AND recovers
vtable-dispatched lifecycle ops for discovery. (2) For cross-config completeness,
extract DBs under the relevant `#ifdef` variants (min: FIPS on/off) and union the
edge relations -- closes (a). (3) Soften the "COMPLETE tree-wide" wording in both
schema docs to "complete within the extracted build configuration + static call
graph." Pairs with the 2026-06-04 macro-expansion-provenance item (same
CodeQL-single-view family) and the 2026-06-16 inline-fn-ptr callback item.

**Note (2026-07-28) -- maximal-feature build as the cheap version of fix (2).**
Before paying for N builds + an edge-relation union, try a single build
configured with **every optional feature on** (`--enable-all` /
`./config enable-...` for openssl, all `-D<feature>=ON` for CMake). One build,
no composer changes, and it recovers the whole `#ifdef FEATURE` class -- which is
most of blind spot (a). The configure line is authored by hand in
`build.json`, so this is a choice at database-creation time, not a
composer change.

Two things it does **not** fix, so fix (2) stays on the table:
- **Mutually-exclusive branches** (`#ifdef _WIN32 / #else`, FIPS on/off). These
  are a genuine either/or, not optional extras -- only multi-config union
  reaches both arms.
- **Files the build system excludes entirely** (platform backends never handed
  to the compiler). Bigger in practice than the `#ifdef` loss and invisible to
  a feature flag. Cheap complement: also create a `--build-mode=none` DB (CLI
  >= 2.21.4, GA 2025-10) -- it infers TUs from file extensions rather than from
  the build, so it picks up untraced files, while the traced DB keeps correct
  flags and build-time generated code. Union the two.

**Measure before fixing.** CodeQL retains the preprocessor directives even for
branches it never parsed, so the loss is queryable from the existing substrate:
`PreprocessorBranch.wasTaken()` holds only where some TU took the branch, and
`PreprocessorBranchDirective.getNext()` walks to the matching `#else`/`#endif`
for the skipped source range. A `not pb.wasTaken()` query plus a diff of `*.c`
on disk against the DB's `File` entities gives a coverage number -- worth having
regardless of which fix is chosen, and it tells us whether this is worth paying
for on a given target.

**Counterpoint worth recording:** for wrapper generation, config-conditional
extraction is arguably *correct*, not lossy -- a symbol behind a disabled
`#ifdef` is absent from the `.so` we link against, so wrapping it is a link
error, not a recovered feature. The real consequence is that the generated
`-sys` crate is valid only for the extracted configuration, i.e. the C feature
flags ought to surface as Cargo features. That reframing may make the whole item
lower priority than it looks.

---

## ~~`orchestrator.md` names config files that do not exist~~ (2026-08-04, FIXED)

**Fixed by retiring the file.** `prompts/orchestrator.md` was never loaded by
any stage — `agents/base.py:_prompt()` resolves `prompts/<stage>.md`, and only
`types` and `symbols` are stages — so it was an orphan shipping inside the
installed package. It is now `skills/crustify-orchestrator/SKILL.md`, which uses
`cli-config.json`, `scope-config.json` and `docs/schemas/crates.md` throughout.
The `templates/scope-config.json` back-reference was repointed at the same time.
The original report follows.

`prompts/orchestrator.md` refers to both tiers of config by the wrong filename,
so an orchestrator following it verbatim authors files nothing reads:

| orchestrator.md says | actual (`src/crustify/layout.py`) |
|---|---|
| `crustify/config.json` (L22) | **`cli-config.json`** (`layout.py:146`) |
| `crustify/targets/<target>/config.json` (L44) | **`scope-config.json`** (`layout.py:172`) |
| `config.json.port_files` (L46) | `scope-config.json.port_files` |
| "`config.json` when all files are port-scope" (L48) | same |

The irony is that `layout.py:143-145` documents *exactly* this trap in the
`cli_config` docstring -- "Named apart from the per-target `config`
(`scope-config.json`) on purpose: two files both called `config.json`, one
repo-tier and one ..." -- i.e. the two files were deliberately renamed to avoid
the ambiguity, and the prompt still carries the pre-rename names.

Failure mode is silent-ish rather than loud: the pipeline errors with a missing
`scope-config.json` at `analyze scope`, which is several manual steps (build,
CodeQL DB creation, `extract-ql`) after the file was authored, so the fix is
cheap but the feedback loop is long.

**Fix:** s/`config.json`/`cli-config.json`/ at L22 and
s/`config.json`/`scope-config.json`/ at L44/L46/L48.

**While in the file** (same class, not tracked separately): L63 points at
`docs/crates.md`, which is at **`docs/schemas/crates.md`**. Worth a grep for
other stale paths in `prompts/` at the same time -- these were all found by one
orchestrator run, so the prompt tree has probably not been path-checked since
the renames.

Surfaced driving the full pipeline on the libgit2 `src` target.

---

## `templates/scope-config.json` mis-names itself in its own `_comment` (2026-08-04)

The template's first `_comment` line reads:

```
"Example crustify/targets/<target>/config.json (user-authored,"
```

but the file is `scope-config.json`, and that is the name `layout.py:172` looks
for. So the one artifact whose job is to show the user what to author tells
them to author it under a name the loader will not find.

Worse than the `orchestrator.md` case above ([same rename drift]): a template is
normally copied verbatim, and the `_comment` is the only place a reader would
check the intended filename -- there is no schema doc for this file to
cross-check against. Anyone who trusts the comment over the filename gets a
`scope-config.json`-shaped file named `config.json`.

**Fix:** one-word edit in the `_comment`. Then sweep the other templates for the
same self-description drift -- `templates/cli-config.json`'s `_comment` is
correct today, but nothing enforces that a template's `_comment` agrees with its
own basename, and both files were renamed at the same time. A trivial test
(assert each `templates/*.json` `_comment[0]` mentions its own basename) would
close the class permanently and is cheaper than re-finding this by hand.

Surfaced driving the full pipeline on the libgit2 `src` target.

---

## `dag.Node.key` drops `node_kind`, so a tag and an identifier collide (2026-08-07)

C keeps **separate namespaces for struct tags and ordinary identifiers**, so one
file may declare both. `static struct { … } typelen[] = {…}` in `src/util/date.c`
yields a TYPE node `typelen` and a GLOBAL node `typelen`, same `defined_in`.

`Node.key` is `(id, defined_in)` (`src/crustify/dag.py:54`), so `load_nodes`
writes both into `by_key` under one key and the second overwrites the first. The
symbol always sits at a higher layer, so the TYPE node is the one lost. On
libgit2/`src`: 6,835 nodes -> 6,830 distinct keys, 5 collisions.

| name | file | type | symbol |
|---|---|---|---|
| `tree_key_search` | `src/libgit2/tree.c` | L0 | L8 |
| `typelen` | `src/util/date.c` | L0 | L1 |
| `special` | `src/util/date.c` | L1 | L6 |
| `merge_driver_registry` | `src/libgit2/merge_driver.c` | L2 | L3 |
| `stream_registry` | `src/libgit2/streams/registry.c` | L2 | L3 |

**Two visible consequences.**

1. `query dag --layer N --port-only` emits 641 of 646 port types. Not a scope
   filter: `_scope_predicate` returns True for all five; they are gone from
   `by_key` before `keep()` runs. `crustify/evaluation/port-closure/deps-dag.md`
   records the disagreement in its cross-check caveat.
2. All five names are **unschedulable**. `wrap --name typelen` and `query dag
   --name typelen` both refuse as ambiguous and offer two identical
   `--file src/util/date.c` lines -- the disambiguator cannot disambiguate,
   because what differs is `node_kind`, which the key does not carry.

The composer already gets this right: `deps_dag._build_edges.res_sym` has
`if name in types: dt.add(name)  # name collides with a type tag (C's separate
namespaces) -> the type node`, and `deps-dag.json` holds both nodes with correct
deps and layers. Only the LOADER conflates them.

**Fix:** key `by_key` on `(node_kind, id, defined_in)`. Ripples to ~55 call sites
across `dag.py`, `query.py`, `wrap.py`, `_schedule.py`. The awkward one is
`query.py:2037` --
`[dk for dk in (*n.dep_types, *n.dep_syms) if dk in by_key]` -- which merges the
two namespaces into one lookup and discards exactly the information the fix
needs; `dep_types` vs `dep_syms` already says which space each edge belongs to.

Low urgency: all five are file-static structs in port scope, so no wrap-stage
work touched them. It becomes blocking when the port stage reaches those TUs.

Surfaced cross-checking the regenerated port-closure table against the oracle.

---

**Parked / deferred** -- design notes and known gaps, older; each keeps its
surfaced-date tag.

## 2026-06-20 - Per-agent "relevant tests only" instead of full ctest per port batch

### The idea

port.md sec 5 currently has every port agent run the **full** `build.json` test
(`ctest`) twice -- C-flag OFF (regression guard) and ON (Rust linked). With many
batches per wave that's a lot of redundant full-suite runs. Since the variant is
selected **per C file** (`CRUSTIFY_<FILE>`), an agent's ON build only swaps in its
own file's symbols -- so it could run just the clar suites that exercise those
symbols (`libgit2_tests -s<suite>`) as a fast per-changeset smoke.

### Why it's not a drop-in

- **Global symbol swap:** a ported low-level primitive (alloc / khash / str /
  vector / oid) is called by ~every suite, so its "relevant" set is "everything"
  -- and a regression surfaces in a suite the agent skipped. Core/util ports need
  the full suite as a fallback.
- **Combined linkage is never agent-tested:** each agent flips only its own flag,
  so no agent tests the full combined shim (the whole wave ON together). That's
  what the post-wave OFF/ON gate does -- narrowing per-agent is only safe if that
  comprehensive gate stays an explicit wave-level step.
- **Speedup is bounded:** per-agent cost is build + relink + test-run; narrowing
  cuts only the test-run, and the relink often dominates. Measure first.
- **Attribution moves later:** cross-area breaks shift from the causing agent to
  the post-wave gate, where you must bisect across N parallel agents.

### Shape if pursued

port.md sec 5 -> run the relevant clar suite(s) for the ON smoke (full-suite fallback
for core/util), drop the per-agent full OFF run to a build-guard, and make the
comprehensive post-wave OFF/ON gate over the combined shim an explicit step.
Compose with sharding the post-wave gate (clar isolates each `-s<suite>` process,
so it parallelises safely).

### Tracker

Surfaced 2026-06-20 looking at wave wall-clock. Orthogonal to (and better than)
parallelising ctest, since it removes redundant work rather than spreading it.

---

## 2026-06-20 - Capture typegen polymorphic relationships for pure-macro-generated families

### The gap

The `casted: {to, from}` graph that links a synthetic typegen **generator** to
its concrete **instances** is populated automatically for **type-erased**
relationships (where the erasure site is visible to the analyzer). For families
generated by a **pure C macro** that loses every type-level relationship at
compile time -- the `GIT_HASHMAP_FUNCTIONS(name, ...)` khash families are the
canonical case -- there is no residual signal to recover from, so the graph comes
back empty and the generator has to be authored (and `casted` wired) by hand.

### Why it matters

An empty `casted` graph means no generic wrapper is inferred, so every instance
falls back to hand-rolled raw field projection (this is exactly what produced
the `GitMwindowPackmap` / `GitPackOffsetmap` raw-projection smell -- see
PITFALLS.md and the `util/hashmap.rs` generic `HashmapRepr`). We fixed it by
hand-adding the `git_hashmap` generator + per-key instance casts; the open work
is to recover (or reconstruct from the macro definition) those relationships
automatically so the scaffolder emits the generic wrapper without manual help.

### Tracker

Surfaced 2026-06-20 fixing the char\*/off64_t khash families. Relates to the
type-erased ownership model already captured automatically; the macro-generated
case is the uncovered half.

**Note (2026-07-07 audit):** the scheduler already carries a dormant polymorphic-family co-scheduling path -- `load_polymorphic` / `expand_polymorphic` / `group_polymorphic_batches` / `family_members` in `_schedule.py` -- that is currently dead code (defined, never called). If this item is picked up, wire those in or delete them; don't re-implement.

---

## 2026-06-20 - CVec array-family elems not fully bound when allocated via expanded macros

### The gap

Some array-family elements are not bound into their array family's `elems` when
the allocation happens through a **macro that CodeQL expands inline** (the
khash `kh_resize` `git__mallocarray`/`git__reallocarray` allocations were
missing from the cluster `elems`). The cluster therefore under-reports its
owned arrays, and a `CVec<T, S>` wrapper that should cover those buffers is not
derived -- the port falls back to raw `git__malloc`/`reallocarray` + manual free.

### Why it matters

A missing `elems` entry means the owning-buffer relationship is invisible, so
`CVec` (owning C-allocated array; `into_raw_parts` suppresses Drop) is never
suggested for that buffer and the raw allocator survives the port. We patched
the missing `u32`/element entries by hand in `types.json` (recorded in
PITFALLS.md sec 9) and supplied the `CVec<u32, GitMallocarrayArray>` alias; the
open work is detecting macro-expanded allocations during clustering so `elems`
is complete without the manual patch.

### Tracker

Surfaced 2026-06-20 (the khash `new_flags` CVec conversion). Pairs with the
"more semantic auditor" item -- both are downstream of regex/CodeQL missing
macro-expanded sites.

---

## 2026-06-20 - Semantic (AST-based) auditor instead of pure regex

### The gap

(RETIRED — `compose/audit_manifest.py` was deleted once `utils/unsafe_metrics`
became the only backend; `audit.py` imported nothing from it. Kept as the
rationale for why the resolution-aware pass replaced it.)

`compose/audit_manifest.py` was a pure-regex scanner over comment/string-stripped
source. It has no notion of scopes, types, or expression structure, so every
heuristic is a brittle pattern: impl/trait/seam regions are matched by
brace-balancing on `_strip_noise`d text, and "smells" are regexes (`_RE_RAW_PTR`,
`_RE_RAW_FIELD_PROJ`, ...). This bites -- e.g. `_strip_noise` mis-parsed Rust
lifetimes (`'this`) as char literals and truncated every impl span containing
one (fixed 2026-06-20), and the raw-field-projection metric needs lookbehinds to
avoid matching `addr_of!(*x.as_ptr()).read()` as a field projection.

### Why it matters

Regex heuristics trade recall/precision for zero build deps, but they cannot see
through type information (is `p` a wrapper with an accessor, or a bare FFI
pointer with none?) -- which is exactly the distinction the projection auditor
wants. A real parse (`syn` over the Rust tree, or `rust-analyzer`/MIR for
type-resolved queries) would let the auditor reason about scopes and types
directly instead of approximating them with brace counting and naming
conventions.

### Tracker

Surfaced 2026-06-20 adding the raw-field-projection metric. Not urgent -- the
regex auditor is useful and dep-free -- but every new metric pays the
approximation tax, so a semantic backend is the eventual direction.

---

## 2026-06-17 - Opaque wrap-scope type embedded by-value in a port struct: raw now, RAII handle wrapper later

### The case

A port-scope struct embeds an **opaque wrap-scope type by value** and only
ever passes its address to wrap-scope ops -- never touches its fields. Canonical
example: `git_odb.lock: pthread_mutex_t` (also `git_cache`, `git_mwindow_ctl`).
The port calls `git_mutex_init` (ctor), `git_mutex_lock`/`unlock` (ops),
`git_mutex_free` (dtor) on `&db->lock`; it never reads the mutex's internals.

### Decision (v1): keep it raw

Embed the bindgen binding directly (`lock: pthread_mutex_t`, `#[repr(C)]`) and
call the wrap-scope ops through `ffi::` with an `addr_of_mut!(self.lock)` cast,
inside `unsafe`. No wrapper, no accessors, no lifecycle trait. Matches the
"primitives stay in C" stance of sec 2026-06-10 (lifecycle/sync primitive
migration).

### Why a wrapper is eventually wanted (not v1)

- **No field accessors** -- opaque, so the ops->accessor refactor correctly
  derives none. That part needs nothing.
- **But lifecycle is real and owner-driven**: init (ctor) + destroy (dtor) +
  lock/unlock (ops). Skipping `pthread_mutex_destroy` on drop leaks/UB, so
  *something* must carry that `Drop`.
- **Soundness, not just ergonomics**: a lock is acquired through a *shared*
  handle (`&GitOdb` across threads), i.e. mutation through `&self`. On a bare
  field that's UB under Rust aliasing -- it needs `UnsafeCell`. The raw-embed +
  `addr_of!(self.lock) as *mut` cast is the unsound pattern; this is exactly
  why `std::sync::Mutex` wraps the OS mutex in `UnsafeCell`.

The eventual shape: a `#[repr(transparent)] struct GitMutex(UnsafeCell<pthread_mutex_t>)`
handle wrapper -- layout-identical so the port struct embeds it by value with
**zero ABI change** -- exposing `init`/`lock`/`unlock` through `&self`,
`Drop`=destroy, and **no** accessors. Reused by every struct embedding the
primitive (wrap once, not per-owner). This is the `locking` metadata already on
`git_odb` (acquire/release/`locked_fields`) made concrete.

### Discriminating rule (for whoever builds it)

For an opaque wrap-scope type embedded by a port struct: emit the transparent
handle wrapper **iff the port invokes any op/lifecycle routine on it**
(lock/init/destroy/...); otherwise (pure carry -- a reserved/padding blob) embed
the raw binding and skip the wrapper entirely.

### Tracker

Surfaced diagnosing why `pthread_mutex_t`/`z_stream` had no wrap-scope
type-entry (the wrap-closure under-capture: the closure walks port-*symbol*
deps but not port-*type* field deps, and never harvests a frontier wrap
function's signature types). Gated behind that closure fix: the analyzer can
only decide wrapper-vs-raw
once the embedded opaque type is captured with its op set attributed (its
lock/unlock ops reachable via the per-symbol lifetime model + `locked_by`; the
alloc.json `locks` category this originally named is gone with the alloc stage).
Keep raw until then.

---

## 2026-06-16 - Header-defined `macro_symbol` is forced wrap-scope; a port-internal one may want mirroring, not a shim

### The rule today

`entry_scope` (`compose/scope.py`) has one carve-out for macros: a macro is
port-scope **only** if its home is a `.c` TU that's itself in port scope;
otherwise wrap -- *regardless of the file rule*. So a `macro_symbol` defined in a
**header** is always wrap, even when that header is in `port_paths` (where the
ordinary `classify` file rule would say port).

```python
if (kind or "").startswith("macro"):
    home = def_file or (decl_files[0] if decl_files else "")
    if home.endswith(_C_TU_SUFFIXES) and home in port_paths:
        return "port"
    return "wrap"
return classify(def_file, decl_files, port_paths)
```

This is shared by the composer's macro `is_port`, the bindgen surface filter,
and the deps-DAG scope query, and the port prompt states the same contract
("header-defined macros are wrap-scope already; mirroring only applies to TU
macros").

### Why it's the default

A `#define` has no ABI and can't be re-exported Rust->C, so a header macro can't
be "ported" in the `#[no_mangle]` + `#ifndef`-guard sense -- the remaining C
callers still need it visible, so it stays a C define read through bindgen. For
`macro_symbol`/`macro_misc` that means a wrap-side `static inline crustify_<NAME>`
shim (the `_MACRO_SHIM_KINDS` path), which bridges to the underlying symbol --
itself possibly port-scope and ported normally. So the target isn't lost; only
the macro stays C.

### The gap / question

The carve-out is uniform across `macro_constant` / `macro_symbol` / `macro_misc`.
For a header `macro_symbol` whose expansion is **purely port-internal logic**
(not a stable public-API alias -- e.g. a convenience wrapper over a port-defined
static), freezing it as a C `#define` + FFI shim is arguably wrong: we'd rather
**mirror** it into a Rust macro/fn (as TU macros already are) and let the C
`#define` stay only where genuine C callers remain. Today there's no way to
express "this header macro is port-internal -> mirror it"; everything header-home
is wrap.

### What to consider

- A signal to distinguish *public-API* header macros (keep wrap/shim -- external
  C callers depend on them) from *port-internal* header macros (mirror, like TU
  macros). Candidates: whether the macro's expansion references only port-scope
  symbols, or whether any out-of-port-scope caller expands it (the macro's
  `used_by`/expansion sites -- analogous to the callback call-site analysis).
- If introduced, `entry_scope` would gain a third branch for header macros whose
  expansion + use sites are entirely port-internal -> port (mirror), and the port
  prompt's "TU-only mirroring" rule would widen to "TU  union  port-internal-header".

### Tracker

Surfaced reviewing the macro carve-out alongside the callback call-site work
(the use-site analysis there is the same shape needed here -- who expands the
macro, in vs out of port scope). Not urgent; the shim path is correct for the
common public-API case.

---

## 2026-06-16 - Callbacks are tracked only as typedefs; inline/anonymous fn-pointers are invisible

### The gap

A callback **node** is minted solely for a **named typedef** -- `kind ==
"callback"` is `reachesRoutineType(TypedefType.getBaseType())` in
`entities/types.ql`, and all the callback machinery (`callback_signature_type_uses.ql`,
`callback_call_sites.ql`, `_enrich_callback`'s `ptr_args`/`depends_on`/`used_by`)
keys off that typedef identity. An **inline / anonymous** function pointer -- a
struct field `int (*read)(...)`, a raw `void (*cb)(...)` arg, a fn-ptr return
without a typedef -- gets **no callback node, no signature deps, no call sites,
no wrapper identity**. It surfaces only as a plain pointer (`ptr_args`/`ptr_ret`
with the `"(routine)"` pointee marker; struct fields get a `ptr` block).

### Why it matters

The dominant inline fn-ptr in libgit2 is the **struct vtable**: `git_odb_backend`
(~14 slots: `read`/`write`/`free`/...), `git_odb_stream`, `git_odb_writepack`,
`git_transport`, the smart-subtransport tables -- all **inline** fn-ptr fields,
not typedefs (bindgen renders e.g. `git_odb_backend.free` as an inline
`Option<unsafe extern "C" fn(...)>`, no alias). So the backend-dispatch
callbacks -- arguably *the* callbacks for the ODB port -- are exactly the ones the
machinery can't see. Inline fn-ptr **args** exist too but are rare (args are
usually typedef'd); the struct-field case is where it bites.

### The crux -- identity

A typedef has a **name** -> one reusable wrapper, one node. An inline fn-ptr is
**anonymous** -> its only identity is its *site*: `(struct, field)` or
`(function, arg-pos)`. Two structurally-identical inline fn-ptrs are distinct
anonymous types. Tracking them means **synthesizing identities** per site -- the
pattern the string/array clusters already use.

### Design fork

The named-vs-anonymous split lines up with the wrap-stage split:
- **Typedef callback** (named, reusable) -> standalone node -> **symbol wrapper**
  newtype + `.call` (the path already built).
- **Inline vtable field** (anonymous, struct-bound) -> part of the **struct's
  contract** -> the **type wrapper's** sec 12.2 / sec 7.3 pattern ("per-element
  behaviour via callbacks -> safe element trait"; "fn-pointer half -> an `unsafe
  extern "C" fn` trampoline"). So inline vtable fn-ptrs arguably should NOT
  become standalone callback nodes -- they belong to the struct.

### But the *facts* gap is the real problem

Even granting the type wrapper owns the vtable, it lacks the facts the typedef
callbacks now have:
- **Signature deps**: `git_odb_backend.read` takes `const git_oid *`, but that
  `git_oid` isn't captured as a struct dep *through the field* (the field-type->tag
  resolver chokes on a fn-ptr type string). Usually masked because the struct
  references `git_oid` elsewhere -- but the *slot's* deps aren't clean.
- **Call sites**: `backend->read(...)` is an indirect call through a field --
  capturable, but `callback_call_sites.ql` requires the callee expr's type to be
  a `TypedefType`, so it skips field-dispatched inline calls. Easy to extend:
  `ExprCall` where the callee is a `FieldAccess` of a fn-ptr field -> key by
  `(struct, field)`, enclosing fn = call site. Still no points-to.

### Options

- **A -- leave untracked** (status quo): vtable slots stay raw `Option<fn>` in
  wrappers; no typed `.call`, naked `ffi::` in the vtable accessors.
- **B -- synthesize per-`(struct,field)` callback identities** (mirror string/array
  clusters): full callback treatment for vtable slots. Most power, most
  machinery (synthetic naming/homing, the field<->node relationship).
- **C -- capture the *facts* only** for inline fn-ptr struct fields (deps + call
  sites keyed by `(struct,field)`), feed the type wrapper's sec 12.2 pattern, no
  standalone nodes. Lighter; respects the "vtable belongs to the struct" fork.

Lean: **C, scoped to struct fields** -- extend the two queries to also emit inline
fn-ptr struct fields (keyed by struct+field) so the type wrapper gets the same
signature-dep + call-site facts for vtable trampolines and correct layering,
without the synthetic-node overhead of B. Leave inline fn-ptr args/returns as
`"(routine)"` ptrs (too rare to pay for).

### Tracker

Surfaced reviewing the callback-deps work (`sec 2026-06-16` callback signature/
call-site capture, commit `c6d90b6`). The real open question is whether the ODB
port wants per-slot typed dispatch (-> C/B) or is fine treating the vtable as one
opaque trait object (-> A suffices) -- a question about how `git_odb_backend`
itself gets ported.

---

## 2026-06-13 - Crate boundary is the *link artifact*: `_in_tree_libs` must cover executables, not just libraries

### What

`scaffold_manifest._in_tree_libs` (and the equivalent gate in `bindgen_manifest`)
collects only `build.json.libraries` with `source_dirs`:

```python
libs = {name for name, lib in (doc.get("libraries") or {}).items()
        if lib.get("source_dirs")}
```

So the crate-per-`linked_in` model currently recognises **libraries** as crates
but **not executables**. An executable target -- e.g. openssl's `apps/`, where
`build.json.executables.openssl.source_dirs == ['apps/']` and the app's
port-scope symbols get `linked_in == "openssl"` -- would have its **own port
crate dropped** (`_crate_for` returns None for `"openssl"`), because the
executable isn't in the in-tree set.

The crate boundary is really the **link artifact** = *library OR executable*.

### What works already (no change needed)

The *wrap deps* an executable target pulls in are attributed correctly today:
the libssl/libcrypto types & syms the apps import through the shared public
headers carry their own `linked_in` (`libssl`/`libcrypto`), independent of the
importing target or the declaration header. So bindgen routes them to
`libssl-sys`/`libcrypto-sys`, wrap routes them to `rust/libssl`/`rust/libcrypto`,
and the app's port crate cross-depends on those. Only the executable's **own**
crate is the gap.

### Fix

Include executables-with-`source_dirs` in `_in_tree_libs` (and any matching
bindgen filter) -- union `libraries`  union  `executables`. The executable's port crate
stays a `staticlib` (`libopenssl_app.a`-style), linked into the final `openssl`
binary exactly as a library staticlib is linked into its consumers; the
`openssl` -> `libssl`/`libcrypto` dependency stays a DAG edge. Confirm
`_port_crate_cargo_toml` is still appropriate for a binary-backing crate (it is --
the C build does the final link; the crate just supplies the re-exported staticlib).

### Tracker

Surfaced reasoning about picking openssl `apps/` as a target while validating the
crate-per-`linked_in` model. Not needed for the libgit2 odb target (a single
library); needed before any executable target. Composes with the `linked_in`
analyzer-fill fix below and the overlapping-`source_dirs` item above.

**Note (2026-07-07 audit):** crate attribution has since moved off `linked_in`/`_in_tree_libs` to the `crates.json` model (`src/crustify/crates.py`, sourced from `build.json`). The concern here (crate boundary = link artifact = library OR executable) still stands, but re-target the fix at `crates.json` / `build.json` library-vs-executable handling, not `_in_tree_libs`.

---

## 2026-06-08 - `decl_files` keeps an alphabetical sort with no semantic value

### What

The T1 entity queries (`entities/functions.ql`, `types.ql`,
`globals.ql`) emit `decl_files` as `concat(..., "|" order by pathOf(h))`.
The `order by` exists only to make the CSV deterministic -- alphabetical
order carries **no semantic meaning**, and a positional `decls[0]` reading
of it is actively misleading (it biases toward `.c` over `.h` since
`c` < `h`, and toward `build/` generated artifacts since `b` sorts first).

### Current mitigation

Consumers no longer read `decls[0]` positionally: `compose.scope.canonical_decl`
picks the declaration by **priority** (in-repo header > in-repo source >
external/absolute, deprioritizing `build/`). So the list order is now
irrelevant to correctness -- the alphabetical sort is dead weight kept
purely for reproducible CSVs.

### To revisit

Either drop the `order by` entirely (and confirm CodeQL `concat` is
deterministic enough across runs for our reproducibility needs), or
replace it with an order that *is* meaningful (e.g. the priority order
`canonical_decl` uses), so the CSV itself is self-describing. Low
priority -- purely a cleanliness / reproducibility-contract question.

---

## 2026-06-04 - CodeQL flattens function calls through macro expansions

### What

cpp-all's `FunctionCall.getEnclosingFunction()` returns the function
whose body lexically contains the call site, regardless of whether
the call appeared directly in source or was emitted by a macro
expansion. The `function_calls.csv` table (and analogously
`global_accesses.csv` / `function_addresses.csv`) records the union
without provenance.

Concrete example from the openssl-crustify-statem analysis:

  - `ssl/statem/statem_dtls.c` line 1218 source code reads
    `ossl_assert(ret > 0)`.
  - `ossl_assert` is a wrap-scope macro in
    `include/internal/common.h` whose body expands to
    `__builtin_expect(!!(...), 1)`.
  - Our T2 CSVs record BOTH of:
    - `macro_expansions`: `dtls1_buffer_message -> ossl_assert` at
      line 1218.
    - `function_calls`: `dtls1_buffer_message -> __builtin_expect`
      at line 1218.

There is no column on `function_calls` indicating that the call
came through `ossl_assert`'s expansion vs being written directly in
`dtls1_buffer_message`'s source.

### Why this matters

For accurate Rust-port generation we want to distinguish:

  - **Direct calls in P's source** -- Rust port emits an explicit
    call to the corresponding extern (`unsafe extern "C" fn`).
  - **Calls inside an expanded C macro M** -- Rust port should
    invoke a hand-written C wrapper `crustify_M(...)` that bindgen
    can bind; the inner calls live inside the wrapper, not in the
    Rust port.

Without provenance, an automated port generator might over-generate
direct extern bindings for symbols that should be reached through
macro wrappers, leading to:

  - Wrong call shape in the Rust port (calling the inner function
    directly with arguments shaped for the macro's API).
  - Possible silent semantic drift if the macro does anything
    non-trivial around the inner call (argument transformations,
    `do { } while(0)` guarding, ifdef-conditional logic).

### Why we're not fixing it now

  1. `macro_expansions` already gives us the `(P, M)` edges, which
     is sufficient to:
     - Include M in the closure of P during composer emission.
     - Tell the wrap-stage agent that a callable macro wrapper is
       needed for M.
     - Generate the C wrapper from M's body recorded in
       `macros.csv`.
  2. bindgen generates extern declarations from headers
     unconditionally -- adding an over-broad set of externs into the
     `-sys` crate is harmless (the unused ones are dead code).
  3. We don't yet have an automated Rust-port generator that needs
     the distinction; hand-port-driven workflows look at source,
     not at our CSVs.

### What we'd do when it matters

Add a `via_macro` column to `entities/function_calls.ql` (and
optionally `global_accesses.ql` / `function_addresses.ql` for
symmetry):

```ql
import cpp

from FunctionCall call, Function caller, Function callee
where caller = call.getEnclosingFunction()
  and callee = call.getTarget()
select caller.getName(),
       caller.getLocation().getFile().getRelativePath(),
       callee.getName(),
       callee.getLocation().getFile().getRelativePath(),
       call.getLocation().getStartLine(),
       call.getEnclosingElement+()
           .(MacroInvocation).getMacro().getName()  // null when direct
```

The composer-side change:

  - When building each port symbol's forward-symbol set for the
    closure, split function_calls rows by `via_macro` IS NULL vs
    NOT NULL.
  - Direct rows -> "P directly calls X" -- extern decl is the right
    binding.
  - Via-macro rows -> "X is reached through macro M's body" -- no
    direct extern from P's perspective; the macro's C wrapper
    handles it.
  - The macro M is in the closure regardless (via
    macro_expansions); the inner X is informational about M's
    body, not about P's needs.

This is a one-column CodeQL change plus a per-edge split in the
composer; estimated half a day of work + retest.

### Tracker

  - First surfaced during analyze-symbols testing for openssl-crustify-statem.
  - Decision: accept the flat data as the source of truth for now;
    revisit when automated Rust port generation work begins.

---

## 2026-06-04 - Deep anonymous-aggregate field inlining (types)

### What

`compose/types_manifest.py` composes each struct's `fields[]` from
`entities/fields.csv`. A field whose *type* is an anonymous
`struct {...}` / `union {...}` (no tag, no typedef) currently emits with
the raw `(unnamed class/struct/union)` type string and is NOT
recursively inlined -- its nested members don't appear in the
manifest.

The locked field schema calls for an `anon` block on such fields:

```jsonc
{"name": "inner", "ref": "value",
 "anon": {"kind": "struct",
          "fields": [{"name": "a"}, {"name": "b", "type": "EVP_PKEY *", "ref": "pointer", "ptr": {...}}]}}
```

recursive (anon-in-anon), with the field-type closure descending
through `anon.fields[*].type` and pointer fields inside `anon` getting
their own `ptr` ownership blocks.

### Why it's deferred

`entities/fields.ql` filters out fields whose *declaring* type is
anonymous (`f.getDeclaringType().getName().prefix(1) = "("`), so the
anonymous aggregate's own members are absent from `fields.csv`.
Inlining needs the composer to (a) know a field's type is anonymous
and (b) enumerate that anonymous aggregate's members. That requires
a `fields.ql` rewrite emitting:

  - a synthetic owner id (`anon:<file>:<line>`) for anonymous
    declaring types instead of skipping them;
  - a per-field `field_anon_id` linking a named field to the
    anonymous aggregate that is its type;
  - (ideally) a field ordinal / byte-offset column so the Rust port
    can reconstruct layout-faithful order -- the current CSV row order
    is CodeQL `Field` iteration order, not guaranteed declaration
    order.

Then the composer reconstructs the nesting by joining on the
synthetic ids and recurses.

### Why it's low-risk to defer for statem

The anonymous aggregates in the statem analysis are the big
anonymous unions inside `SSL_CONNECTION` internals (`ssl/ssl_local.h`)
-- wrap-scope for the statem target, where they surface as narrowed
opaque fields. Statem's own port-scope types are function-pointer
tables + small enums with no anonymous-aggregate fields.

### Tracker

  - Field schema + composer landed without anon inlining; anon-typed
    fields carry the raw type string and the closure correctly skips
    them (no phantom entry).
  - Revisit alongside the field-ordinal column when layout-faithful
    native Rust struct generation begins.

---

## 2026-06-05 - Workload-weighted batching for the per-dir analyzer agents

**OBSOLETE** — the analyze stage spawns no agents; there is no per-dir
batching left to weight. Kept for the reasoning, which transfers to any
future per-dir agent scheduling.

### What

`_run_analyze_parallel` spawns **one agent per manifest dir** and
hands it *every* entry in that dir (selection `"<subject> files:
<files-in-dir>"`), regardless of how many structs the dir has or how
complex their layouts/footprints are. A dir like `ssl/ssl_local` puts
~21 structs -- including the codebase's largest god-objects (`ssl_st`,
`ssl_connection_st`, `ssl_ctx_st`) -- onto a single agent.

Add a **parametric workload budget** so an over-budget dir is split
into **sequential** sub-batches (one agent per batch), keeping each
agent's effort concentrated on a tractable slice.

### Why this matters

This is the structural mitigation for the non-deterministic op
attribution in PITFALLS sec 2026-06-05. The `ssl_st` / `ssl_connection_st`
bail (206 / 0 ops, empty + rationalizing comment) happened in the
full-dir e2e; the scoped 2-struct rerun produced the correct
signature-subject partition (25 / 441, zero overlap). Same inputs,
opposite outcome -- and the difference correlated with per-agent load.
Capping the load should make the correct outcome repeatable.

### Design options (from the 2026-06-05 discussion)

**Weight metric** (computable deterministically from the composed
manifest, weakest->strongest predictor):

  1. entry count -- `<= X structs`;
  2. field sum -- `Sum len(fields) <= N` (captures sec 6b per-pointer load);
  3. footprint sum -- `Sum (|opaque_in| + |non_opaque_in|) <= M` (captures
     sec 4 op-selection load -- the actual driver of the bail; e.g.
     `ssl_connection_st` = 462);
  4. **composite** `w = alpha*1 + beta*fields + gamma*footprint` (recommended,
     footprint-dominant). Enums/callbacks weigh ~0.

**Bin-packing.** Greedy, largest-first, with a hard **">=1 entry per
batch"** rule so a single over-budget struct gets its own agent (that
alone isolates the `ssl_connection_st` case).

**Scheduling.** Entries in the same dir share one `types.json`, so
batches within a dir must run **sequentially** (read-modify-write).
Batches in different dirs stay independent. The model shifts from a
flat pool of dir-jobs to a **pool of dir-chains** -- each chain a
sequential list of batch sub-jobs -- scheduled `parallel_max` chains at
a time. Net: the long-pole dir becomes K sequential focused agents
(more wall-time for that dir, better per-entry quality); cross-dir
parallelism unchanged.

**Sub-batch selection + preservation (main correctness risk).** A
batch targets a subset, so it uses `"<subject> tags: <subset>"` rather
than `files:`. Each batch agent must preserve sibling entries it
isn't processing. Safeguards: (a) the per-entry skip already makes a
later batch skip already-annotated entries; (b) strengthen the prompt
to "preserve other entries' agent annotations too"; or -- more robust --
(c) have the orchestrator write each batch to a scratch file and
`merge_manifest_file`-union into the dir manifest (the merge primitive
already does field-level union by entry key), so correctness doesn't
depend on the agent round-tripping siblings.

**Parameterization.** A `--type-weight-budget` flag (+ optional
`kilo.json`/config default), e.g. composite <= 150, min 1 entry.
Generalizes to symbols (same per-dir overload risk) but scope to types
first.

### Why we're not doing it now

It's a structural orchestrator change (weight computation, bin-pack,
chain scheduler, merge-on-write) with a real correctness risk
(same-file preservation). The cheaper PITFALLS sec 2026-06-05 prompt
guardrail (attribute overlap by signature subject; never empty a type
due to embedded-base overlap) addresses the *attribution* error
directly; the workload budget addresses the *overload trigger*. Ship
the guardrail first, then revisit batching if bails persist.

### Tracker

  - Pitfall + the two-run evidence recorded in PITFALLS sec 2026-06-05.
  - Change point: `analyze.py::_run_analyze_parallel` job-build loop
    (`entries_by_dir` -> jobs) + `_build_per_dir_selection`.
  - Recommended first cut: composite (field+footprint) weight,
    dir-chains scheduling, merge-on-write preservation, behind
    `--type-weight-budget`.

---

## 2026-06-06 - Mixed-scope manifest dirs under `out_of_scope.paths`

### What

(OBSOLETE — the analyzers are gone.) Their manifests-list contract carried
`scope: "port" | "wrap"`
as a **per-manifest** tag, not a per-entry one. The orchestrator
derives this tag from `compose()`'s `dir_scope` map (composer-emitted
alongside `entries_by_dir`), which records per-stem-group scope based
on the in-memory `is_port` flag of the entries it emits.

This works because the file-mapper's stem-grouping
(`utils/codeql/compose/path_partition.py:manifest_dir_for`) puts
files with a shared basename stem under one manifest dir, and
scope.json is typically authored at full-file granularity -- every
file in a stem-group falls on the same side of `port`.

The contract assumes a stem-group never carries entries from BOTH
scopes. The orchestrator collapses any mixed-scope dir to `"port"`
(any port entry -> dir is port) -- which over-classifies wrap entries
in mixed-scope dirs as port-scope, sending them through the
port-scope rule branches (mutability=null forced; depends_on
expected; ops global-footprint regime for types).

### Why it can break

`config.json.out_of_scope.paths` lets the user exclude individual
files within an otherwise port-scope subsystem. If the excluded file
shares a stem with a non-excluded file (e.g. excluding
`foo_internal.c` while keeping `foo.c`/`foo.h`), the resulting stem
manifest dir contains entries from both scopes:

  - symbols defined in `foo.c`/`foo.h` -> port-scope entries (carry
    `used_by`/`depends_on`)
  - symbols defined only in `foo_internal.c` -> wrap-scope entries
    (base shape only)

Today's orchestrator tags the dir `"port"` and the agent applies
port rules to all of them. Wrap entries get wrong mutability
treatment, and wrap types get the wrong footprint regime.

### Likely fix paths

  1. **Composer-side split**: when emitting `entries_by_dir`,
     partition any stem-group with mixed scopes into two distinct
     manifest dirs (e.g. `<stem>__port/` and `<stem>__wrap/`). Keeps
     the per-manifest scope invariant; complicates path-partition.
  2. **Per-entry scope field**: persist `scope` to disk as a per-
     entry field in `syms.json`/`types.json`. The agent dispatches
     per entry. Cleanest at the cost of one extra always-emitted
     field per entry.
  3. **Per-entry orchestrator hints**: keep the on-disk shape
     unchanged but pass per-entry scope via the `names` filter
     value (`names: {"port": [...], "wrap": [...]}` instead of a
     flat list/sentinel). Avoids schema change but distorts the
     filter semantics.

(2) is structurally cleanest and the prior `is_port` field on
in-memory candidates already carries the info; we only dropped it at
emission time. The merge primitive's never-overwrite rule keeps it
safe across re-runs.

### Tracker

  - Change point: `utils/codeql/compose/syms_manifest.py` and
    `types_manifest.py` `compose()` pass-4 emission step; orchestrator
    `analyze.py::_run_subject_manifests_list` -> `_build_chains`.
  - Currently no test target exercises mixed scopes (libgit2 ODB
    cluster has all-in-port-scope authoring; openssl ssl/statem same).
    The bug is latent until someone authors a scope.json with
    `out_of_scope.paths` that splits a stem.

---

## 2026-06-10 - Lifecycle/sync primitive migration C->Rust + pinned native handle

### What

The port stage (v1) keeps a ported type's **low-level lifecycle and
synchronisation primitives in C**: the byte allocator / destructor, the
refcount `up_ref` / `down_ref`, the deep clone (`*_dup`), and the lock
acquire / release. Each is called through the crustify lifecycle traits /
macros (`impl_ref_counted!` -> `CArc`, `impl_freed!` -> `CBox`,
`impl_cloned!` -> C `*_dup`, a `Guard` over the C lock), whose bodies just
forward to `ffi::`. Only the *rest* of the ctor/dtor logic (field
initialisation, validation, teardown) is ported to Rust. Migrating any of
these primitives to Rust -- Rust-side allocation (construct by-value, move
into `Box`/`Arc`), a Rust atomic refcount, a Rust deep clone, a Rust
`Mutex`/`RwLock` -- is deferred.

Two pieces of work are parked here:

  1. **Allocator migration policy.** Decide *when* (if ever) a type's
     allocation should move from C to Rust. The standalone move (Rust
     allocates a still-C-layout struct) buys little soundness -- the
     crustify smart pointers already provide RAII + borrow-checking +
     the field-access discipline regardless of who calls `malloc` -- and
     carries real downside: mismatched-allocator UB if alloc and free
     don't move together, and, for projects with a **pluggable
     allocator** (curl's `Curl_cmalloc`/`Curl_cfree` via
     `curl_global_init_mem`), it breaks the user-override contract
     outright. The real payoff is **layout sovereignty** (native Rust
     fields with their own `Drop`), which is coupled to opacity
     (`--opacify`) and is a *layout* decision, not an *allocator* one.
     So: treat allocator migration only as an enabler of full
     nativization, never as a standalone step.

     The same "stay in C" logic covers the **refcount, clone, and lock**
     primitives, for analogous reasons: a Rust atomic refcount must match
     the C type's exact ordering/observable refcount, a Rust deep clone
     must replicate the C `*_dup` semantics, and a Rust lock must honour
     the C-side lock discipline other C callers still rely on -- all
     error-prone with no soundness gain over forwarding to the audited C
     primitive through the crustify trait. Migrate them only as part of
     full nativization (after layout sovereignty), never standalone.

  2. **Pinned native handle in `crustify-prim`.** Once a type *is*
     Rust-allocated but still exchanges its (opaque) handle with C, the
     allocation must not move after exposure. Today this is expressible
     with std `Pin<Box<T>>` over the `!Unpin` `CType` (`define_type!`
     already bakes in `PhantomPinned`, see `crustify-prim/src/c_type.rs`),
     but there is no ergonomic crate-level primitive (a `CPinBox<T>` /
     pinned `CBox` analogue) that ties the pinned heap handle to the
     crate's ownership model the way `CBox` does for C-allocated
     pointers. Add one when the first real use site lands.

### Why we're not doing it now

v1 keeps allocation in C uniformly, which sidesteps both the
mismatched-allocator hazard and the pluggable-allocator breakage, and
needs no new crate primitive. The native/opacify path that *would* need
(2) is gated behind `--opacify` and is not exercised by the first
smoke-test batches.

### Tracker

  - Surfaced during the port-agent design discussion (synthetic-types /
    native-vs-layout representation).
  - Prompt contract: `prompts/port/port.md` sec 2 "Lifecycle ops (v1
    scope)" states allocator/destructor stay in C and points here.
  - Revisit (2) when the first `--opacify` type that re-exposes a
    Rust-allocated handle to C is ported; revisit (1) alongside the
    deterministic opacity classifier (the future replacement for the
    manual `--opacify` flag).

---

**Landed / done (archived)** -- shipped; kept only so the history stays legible.
Any residual open work has been pulled up into the sections above.

## 2026-06-13 - Deterministic `audit` (entity-seeded, no LLM) -- DONE

`crustify-cli <target> audit` shipped: `src/crustify/audit.py` +
`utils/codeql/compose/audit_manifest.py` (the latter since DELETED — superseded
by `utils/unsafe_metrics`), with a `cli.py` `audit` subparser
(`--name/--file/--dir/--mod/--crate/--all`, naked-FFI search always global).
Replaced the parked CrustifyAuditAgent proposal (`docs/AUDIT_AGENT.md` deleted).
Still deferred (acceptable): the `own`-surface counts can double-count a shared
`unsafe` block across co-homed entities (sharpen with `syn` if it matters); the
LLM-adjudicator verdict/severity layer was dropped with AUDIT_AGENT.md.

## 2026-06-13 - dtor: {storage, fields} split; placement booleans dropped -- DONE

`heap_allocated` / `stack_allocatable` / `embeddable` removed from
`templates/types.json`; `dtor` became a dict. Routing keys on which destructor(s)
a type has, not on separate placement flags. Since evolved further into the
dual-ownership schema below.

## 2026-06-20 - Expand dtor to a list for dual-ownership (shared-xor-exclusive) -- LANDED

Shipped in commit `c3cf47c` ("dual-ownership dtor + per-pointer elems/shared
schema"): `templates/types.json` `dtor` is now `{shared, exclusive, fields}`, so a
type whose handle is either refcount-shared (`CArc`) or exclusively freed (`CBox`)
records both storage destructors. On-disk manifest re-fill was still in flight at
audit time (~38/53 `types.json` on the new schema). If revisiting, verify the
provenance-disjointness soundness gate (distinct free symbols + non-overlapping
ctors) and the scaffolder's two-`Drop` emit.

## 2026-06-12 - Kill *Stack/*Embed companions + hand-written impl Drop -- DONE

`crustify-prim`: `define_type!` forwards `uninit()`/`zeroed()` to `CType`;
`CValued` trait + `CVal<T>` + `impl_cvalued!` added (`smart_pointers.rs`,
`macros.rs`). All 7 target subjects migrated to `CVal` in the libgit2 port
(`GitPool`/`GitMap`/`GitHashCtx`/`GitOidarray`/`GitCache`/... via `impl_cvalued!`);
zero `*Stack` companions remain (the surviving hand-written `impl Drop`s are
legitimate RAII lock/scope guards -- `OdbLock`, `GitPackFileGuard`, ...).
`prompts/wrapper/type_wrapper.md` no longer instructs companions; it routes on the
dtor storage/fields split.

## 2026-06-13 - crate-per-`linked_in` + per-file `mod ffi_export` -- MOSTLY LANDED (mechanism superseded)

The central `rust/ffi-exports/` crate is gone (no `_ensure_ffi_exports`, no
FFI_EXPORTS template); each ported file carries its own
`mod ffi_export { use super::*; ... }`, live in `port.md`/`merge.md` and across the
libgit2 port tree; Phase 5 (regenerate the libgit2 odb cluster on the new layout)
is populated on disk. NOTE: the crate = `linked_in` *field* keying has since been
superseded by the `crates.json` model (`src/crustify/crates.py`; scaffolder/bindgen
reason "crate == link unit" from `build.json`, explicitly never `linked_in`). The
concept (crate = link artifact) survives; the `linked_in` keying does not.
Residual open: `ssl_evp_md_free` cross-library attribution (the 1/786 op that
references the other library's type) -- left `pub` + audit-allowlisted, decision
deferred until the openssl target.

## 2026-06-13 - Type analyzer fills `linked_in` -- SUPERSEDED (obsolete)

Shipped originally, but the whole mechanism is gone: `type_analyzer.md` no longer
has the `linked_in` fill / sec 8.5, `templates/types.json` no longer carries
`linked_in`, and crate attribution moved to `crates.json` (which pointedly does not
use `linked_in`). The listed follow-up (a safety-net that logs any in-tree manifest
dir dropped for null `linked_in`) was never implemented and is now moot. Recorded
here only so the history is legible.

## 2026-06-13 - linked_in sec 8.5: disambiguate overlapping source_dirs -- SUPERSEDED

Predicated on the type-analyzer `linked_in` fill above, which no longer exists
(openssl `providers/` shared by libcrypto/fips/legacy). Re-evaluate under the
`crates.json` model only if directory-shared crate attribution becomes a live
failure; the original rule-1/rule-2 precedence note does not apply to the current
mechanism.
