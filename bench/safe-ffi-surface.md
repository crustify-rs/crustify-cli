# Safe-FFI surface — what hand-written wrappers actually cover

How much of a C library's public API its established Rust wrapper reaches
*safely*, and how that wrapper represents the C types. Measured, not asserted:
every figure below comes from the shipped `.so` and the crate source, and the
method is stated so it can be re-run when either moves.

The question this answers for target selection is not "does a safe crate
exist" -- for any mature C library it does -- but **what fraction of the API it
covers, and whether the uncovered part matters**.

**Legend**

- `C fns` — exported `T` symbols in the shipped shared object, symbol-version
  suffixes stripped, leading-underscore internals dropped
- `safe fns` — of those, the ones appearing in CALL position in the crate's
  safe sources; generated bindings and doc comments excluded
- `C structs def / opaque` — `struct X { … }` in a public header (layout
  visible to a consumer) versus `typedef struct X X;` with no body
- `safe types` — `pub struct` in the safe crate. An UPPER bound on wrapped C
  types: it also counts iterators, builders and error types
- `getter-shaped` — `pub fn name(&self)` taking no further argument. A proxy
  for a field accessor, not an exact count — it also catches computed
  properties

## Coverage

| library | crate | C fns | safe fns | coverage | C structs def / opaque | safe types |
|---|---|---|---|---|---|---|
| libgit2 1.9.0 | `git2` | 902 | 592 | **65.6%** | 54 / 53 | 154 |
| libssl + libcrypto 3.x | `openssl` 0.10.81 | 6,463 | 804 | **12.4%** | 155 / 211 | 246 |
| libavcodec/format/util 7.1.5 | `ffmpeg-next` 9.0.0 | 916 | 131 | **14.3%** | 171 / 33 | 108 |
| libxml2 2.9.14 | `libxml` 0.3.21 | 1,649 | 113 | **6.9%** | 59 / 34 | 16 |


`libgit2` is the outlier and the control: `git2` is a genuinely comprehensive
binding, which is why the pipeline found no symbol wave worth running there.
The other three leave most of their C API unreachable without `unsafe`.

Note where OpenSSL sat when Crustify started on it: **26.3%** for `libssl`
alone (159 of 604), the figure for the whole of libssl+libcrypto being dragged
down by the much larger crypto surface.

## Representation and borrow discipline


| library | `repr(transparent)` | `repr(C)` | `&self` fns | `&mut self` fns | `-> &mut` | `&mut` params | getter-shaped | `set_*` | pub fns |
|---|---|---|---|---|---|---|---|---|---|
| libgit2 | 0 | 5 | 563 | 376 | 264 | 39 | 368 | 39 | 1141 |
| libssl | 11 | 0 | 379 | 392 | 72 | 178 | 296 | 176 | 1269 |
| libavcodec/format/util | 1 | 0 | 290 | 204 | 33 | 41 | 263 | 106 | 723 |
| libxml2 | 0 | 0 | 102 | 47 | 0 | 14 | 64 | 12 | 249 |

**None of the four is layout-compatible with the C type in any systematic
way.** `openssl`'s 11 `repr(transparent)` and `ffmpeg-next`'s 1 are isolated;
the dominant pattern everywhere is a Rust-native handle owning a `*mut ffi::T`.
A wrapper therefore can never be handed back to C by value — the capability a
`#[repr(transparent)]` newtype over the C type buys.

**All four take `&mut self` freely** (392 / 376 / 204 / 47 methods). None uses
interior mutability. That is sound for them precisely BECAUSE the handle is
Rust-native: the `&mut` is over the pointer-holding struct, never over the C
object, and the write itself is a raw-pointer store.

**None of them forms `&ffi::T`.** Field access is a raw-pointer place
projection — `(*self.as_ptr()).field` — which reads the field without
materialising a reference to the struct. On the projection axis they already do
what `addr_of!` enforces.

## Three field-access strategies

| crate | strategy |
|---|---|
| `ffmpeg-next` | direct projection both ways: `(*self.as_ptr()).width` to read, `(*self.as_mut_ptr()).width = v` behind `&mut self` |
| `openssl` | direct projection, read-only; construction goes through `X_new` + `set0_*` |
| `libxml` | no projection at all — every field read delegates to a C function (`xmlNextSibling(self.node_ptr())`) |

This predicts the coverage. `ffmpeg-next` can mechanically mirror a public
field, so it has 263 getters and 106 setters against only 131 reachable
functions — FFmpeg's API IS field assignment. `libxml` can only expose a field
for which libxml2 exports an accessor, and where none exists the field is
unreachable; hence 6.9%.

## Where libxml2's gap sits

| header | C fns | safe | coverage |
|---|---|---|---|
| `xmlunicode.h` | 166 | 0 | 0% |
| `tree.h` | 163 | 50 | 31% |
| `xpathInternals.h` | 115 | 1 | 1% |
| `xmlreader.h` | 86 | 16 | 19% |
| `parserInternals.h` | 84 | 0 | 0% |
| `xmlwriter.h` | 80 | 0 | 0% |
| `valid.h` | 70 | 0 | 0% |
| `parser.h` | 70 | 5 | 7% |
| `xmlIO.h` | 52 | 3 | 6% |
| `(no header)` | 50 | 0 | 0% |
| `xpath.h` | 40 | 9 | 22% |
| `SAX2.h` | 38 | 0 | 0% |
| `HTMLparser.h` | 37 | 5 | 14% |
| `catalog.h` | 37 | 0 | 0% |

The covered part is the parse -> tree -> xpath path the crate was built around.
Outside it: **no safe XML writer** (`xmlwriter.h` 0/80), **no DTD validation**
(`valid.h` 0/70), **no SAX2**, **no catalog resolution**. Excluding the
deprecated corners (`nanoftp`, `nanohttp`, `DOCBparser`, `SAX`, `globals`,
`threads`, `xmlmemory`) moves the total only from 6.9% to 7.7%.

The maintainers state the cause outright: *"providing a more or less complete
wrapper would be too much work"*, *"only covers a subset of libxml2 at the
moment, contributions are welcome"*. That is the economics of hand-writing
wrappers, and it is the constraint a closure-driven pipeline does not face.

One gap it would NOT close: *"no thread safety — libxml2's global memory
management is a challenge to adapt in a thread-safe way"*. That is a property
of the C library, so a generated wrapper inherits it.

## Runtime ownership flags

`ffmpeg-next` decides ownership per construction site and stores it in a
`_own: bool`, consulted in `Drop`:

| type | flag read in `Drop`? |
|---|---|
| `codec::Picture` | yes (`picture.rs`) |
| `scaling::Vector` | yes (`vector.rs`) |
| `util::frame::Frame` | **no** — `Drop` calls `av_frame_free` unconditionally |

`Frame::wrap(ptr)` sets `_own: false` and `Frame::empty()` sets `true`, but the
`Drop` impl never reads the field, so a borrowed frame is freed anyway. The `_`
prefix suppresses the dead-field warning that would have flagged it. `wrap` is
`pub unsafe fn`, so the obligation is formally the caller's, but nothing in the
signature says the frame will be freed.

Two of three types get it right — a discipline that mostly works and fails
silently when it does not. The contrast with encoding ownership in the TYPE
(an owning handle versus a borrowed view, chosen once at analysis time and
recorded with the C function that justifies it) is that the wrong choice
becomes a compile error rather than a runtime free.

## Method, and two traps

Both traps produced wrong answers before the method settled; either would
silently mis-rank a target.

- **Bare-identifier matching OVER-counts.** `rust-openssl` documents
  *"corresponds to `SSL_CTX_new`"* on nearly every method, inflating its count
  by ~150 phantom calls. Doc comments must be stripped.
- **Path-prefix matching UNDER-counts.** Keying on `ffi::NAME` / `bindings::NAME`
  gave `libxml` 16 instead of 113, because that crate imports symbols rather
  than path-qualifying them.

Call-position matching with doc comments stripped is the only rule that holds
across all four; it agrees with a per-header hand count for libxml2 to within 3.

Sources: `nm -D --defined-only` on the shipped `.so`; public headers from the
distribution `-dev` packages; crate sources at the versions named above.
