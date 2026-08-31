# libgit2 — public API surface analysis

Survey of libgit2's public API and of the safe-wrapper work that already
exists, for deciding whether a public-API `wrap` campaign is worth running and
what its `api_headers` boundary should be.

**This is not the campaign already recorded in this directory.**
`examples/crustify/libgit2/TASK.md` targets the whole `src/` dir — a port objective, whose
wrap waves cover the *import closure* the port needs. libgit2's own public API
has not been wrap-campaigned here.

Counted, not recalled. Sources measured:

- `libgit2` @ `0551dfd4ad989b6a3d5683c0d4cf326c6efef929` (2026-08-15),
  `LIBGIT2_VERSION 1.9.0`
- `git2-rs` @ `da6c126f7733329d82748e710266af1c61591bdd` (2026-07-25),
  crate `git2` 0.21.0

## The public API header

**`include/git2.h`** is the umbrella and the only entry point. 74 lines, no
declarations of its own, 62 `#include "git2/…"` lines. Consumers do
`#include <git2.h>`.

### Header tiers

| tier | path | headers | `GIT_EXTERN` fns |
|---|---|---|---|
| consumer API | `include/git2/*.h` | 68 | **876** (68 of them in `deprecated.h`) |
| extension API | `include/git2/sys/*.h` | 24 | **112** |
| umbrella | `include/git2.h` | 1 | — |
| | **93 total** | | **988 total** |

Six of the 68 top-level headers are not named in `git2.h`:

| header | reached how |
|---|---|
| `strarray.h` | transitively — `refs.h`, `reset.h`, `status.h`, `index.h`, `remote.h`, `tag.h`, `worktree.h`, `pathspec.h` |
| `oidarray.h` | transitively — `odb.h`, `merge.h` |
| `trace.h` | via `deprecated.h` |
| `credential_helpers.h` | via `deprecated.h` and `cred_helpers.h` |
| `cred_helpers.h` | deprecated one-line alias; included by nothing |
| `stdint.h` | compat shim; included by nothing |

`include/git2/sys/` is a genuinely separate surface, unreachable through
`git2.h`: `alloc`, `commit`, `commit_graph`, `config`, `cred`, `credential`,
`diff`, `email`, `errors`, `filter`, `hashsig`, `index`, `mempack`, `merge`,
`midx`, `odb_backend`, `openssl`, `path`, `refdb_backend`, `refs`, `remote`,
`repository`, `stream`, `transport`. It is the "you are extending libgit2" API
— custom ODB, refdb, transport, stream and filter backends — with
correspondingly harsher invariants.

## Public API surface

| surface | count |
|---|---|
| exported functions (`GIT_EXTERN`) | **988** — 876 consumer, 112 extension |
| of those, deprecated | 68 (all in `deprecated.h`) |
| **live consumer functions** | **808** |
| public structs | 90 |
| opaque handles (`typedef struct git_x git_x;`) | 73 |
| public enums | 86 |
| callback typedefs (`GIT_CALLBACK`) | 130 |
| public headers | 93 |
| impl `.c` files / loc | 204 / 144,095 (`.c` + `.h`) |

libgit2 is a wide, honest C API: ~1,000 typed functions over 73 opaque handles,
all of it visible to a symbol query. Contrast libcurl, whose real surface hides
behind four variadic entry points — see `examples/crustify/curl/surface-findings.md`.

| | libgit2 1.9.0 | libcurl 8.22.0-DEV |
|---|---|---|
| public headers | 93 | 12 |
| exported functions | **988** | 92 |
| public structs | 90 | 18 |
| opaque handles | 73 | 7 |
| public enums | 86 | 35 |
| callback typedefs | **130** | 30 |
| impl `.c` files / loc | 204 / 144k | 197 / 177k |
| hidden option surface | none | ~314 options behind 4 variadic fns |

## Existing work

### `git2-rs` — the safe wrapper

29,328 lines of safe layer over 5,177 lines of `libgit2-sys`, with **1,060**
`unsafe` occurrences.

Measured coverage against the C surface:

| | C | `libgit2-sys` | safe layer |
|---|---|---|---|
| exported functions | 988 | 683 | **627** (63%) |

361 exported functions have no safe Rust form. Roughly 3× curl-rust's line
count against 10× the surface, and about 9× its `unsafe` density — a much
larger hand-written safety-obligation surface than curl-rust carries.

### Adoption

crates.io API, 2026-08-23:

| crate | version | all-time downloads | last 90 days | direct dependents | first published | last release |
|---|---|---|---|---|---|---|
| `git2` | `0.21.0` | **109,392,193** | **15,545,752** | **1,969** | 2014-11-14 | 2026-05-18 |
| `libgit2-sys` | `0.18.8+1.9.7` | 109,000,838 | 15,559,505 | 11 | 2014-11-13 | 2026-08-21 |
| `gix` | `0.87.0` | 43,485,525 | 9,388,014 | 386 | 2023-02-10 | 2026-08-22 |

**The `-sys`/safe ratio is inverted relative to curl.** `libgit2-sys` has 11
direct dependents against `git2`'s 1,969 — essentially everyone goes through
the safe wrapper. For curl it is 31 against 295, with `curl-sys` outdrawing
`curl` by ~5M downloads. `git2` *is* the ecosystem's git API; `curl` is one of
several ways to reach libcurl, and a meaningful share of traffic routes around
it.

### `gix` — a credible pure-Rust competitor

9.4M downloads/90d, 386 direct dependents, released within the last two days of
this survey. A native reimplementation eating the same niche. libcurl has no
equivalent at the *API* level — reqwest and hyper replace the use case, not the
interface.

## Gaps

1. **361 of 988 exported functions have no safe Rust form** — 37% of the
   surface.
2. **1,060 `unsafe` occurrences** in the safe layer: a large hand-audited
   obligation surface, none of it machine-checked.
3. **130 callback typedefs** — the extension points where C hands control back
   to the caller, and the hardest part of the surface to wrap soundly.
4. **The `sys/` extension API is essentially unwrapped** and is where custom
   backends live.

Unlike curl, the incumbent here is strong: `git2` covers 63% of a much larger
surface, and `gix` offers a native alternative with real traction.

## Sizing

Type and callback units ≈ 90 structs + 86 enums + 130 callbacks ≈ **306**.
Symbols **988** (or **808** excluding `deprecated.h`).

At the rates this repo measured on the libgit2 `src/` port
(`wrappers-results-opus5.md`: `$8.16` per type, `$0.56` per symbol):

| | units | estimate |
|---|---|---|
| types + callbacks | 306 | ~`$2,500` |
| symbols | 988 | ~`$550` |
| **Σ** | | **~`$3,050`** |

Roughly 3–4× a curl campaign, with no variadic-table problem to solve first.

## For an `oracle-config.json`

`api_headers` should be `include/git2.h` plus its closure — the 876-function
consumer surface. Two boundary decisions to make explicitly:

- **`deprecated.h`** — 68 superseded functions. Put it in `excluded-surface`
  rather than spending a wave on it.
- **`include/git2/sys/`** — exclude by default. 112 functions whose safety
  contract is "you are implementing a backend correctly", a different and much
  harder wrapping problem than the consumer API.

That leaves **808 live consumer functions** as the natural campaign surface,
against the 627 that `git2` currently reaches.

## References

- <https://github.com/libgit2/libgit2>
- <https://github.com/rust-lang/git2-rs>
- <https://crates.io/crates/git2>
- <https://crates.io/crates/gix>
- `examples/crustify/curl/surface-findings.md` — the same survey for libcurl
- `examples/crustify/libgit2/wrappers-results-opus5.md` — the `src/` port campaign whose
  per-unit rates the sizing above borrows
