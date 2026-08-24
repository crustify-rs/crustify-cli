# curl — wrap campaign surface findings

Pre-campaign survey of libcurl's public API and of the safe-wrapper work that
already exists, to decide whether a `wrap` campaign is worth running and what
shape it needs.

Counted, not recalled. Sources measured:

- `curl` master @ `8770c49de4d992aa901157bc0d78de88eb12880e` (2026-08-23),
  `LIBCURL_VERSION 8.22.0-DEV`
- `curl-rust` @ `bae5dc561e0711b4982eeddda8b5dbac45d531f1` (2026-06-29),
  crate `curl` 0.4.50, bundling curl 8.21.0

## Libraries shipped

**One installed library: `libcurl`** (`lib_LTLIBRARIES = libcurl.la`).

Two internal convenience libraries are built but never installed, and are not
API:

| target | what it is |
|---|---|
| `libcurltool.la` / `curltool` | the `curl` command-line tool's own code |
| `libcurlu.la` / `curlu` | a build of `lib/` with internals exposed, for unit tests |

CMake additionally emits static and shared variants of the same `libcurl`.

A campaign therefore has a single target library. `lib/` holds 197 `.c` files
and ~177k lines of `.c` + `.h`:

| dir | `.c` files |
|---|---|
| `lib/` | 128 |
| `lib/curlx/` | 19 |
| `lib/vtls/` | 16 |
| `lib/vauth/` | 13 |
| `lib/vdns/` | 10 |
| `lib/vquic/` | 8 |
| `lib/vssh/` | 3 |

## Public API surface

12 installed headers; 9 substantive, with `system.h`, `stdcheaders.h` and
`typecheck-gcc.h` as scaffolding.

| surface | count |
|---|---|
| exported functions (`CURL_EXTERN`) | **92** |
| `CURLOPT_` enum entries | **287** |
| settable option names (`Curl_easyopts` rows) | **327**, of which 13 aliases → ~314 |
| `CURLINFO_` | 98 |
| `CURLE_` | 142 |
| `CURLMOPT_` | 22 |
| `CURLM_` | 16 |
| `CURLSHOPT_` | 7 |
| URL-API constants | 61 |
| public structs | 18 |
| public enums | 35 |
| callback typedefs | 30 |
| opaque handles | 7 |
| documented names (`symbols-in-versions`) | **1184 total, 1030 still present** |
| man pages | 104 top-level + 422 under `docs/libcurl/opts/` |

The 92 exported functions by header:

| header | fns |
|---|---|
| `curl.h` | 38 |
| `multi.h` | 22 |
| `easy.h` | 10 |
| `mprintf.h` | 10 |
| `urlapi.h` | 6 |
| `websockets.h` | 4 |
| `header.h` | 2 |

The 7 opaque handles: `CURL`, `CURLM`, `CURLSH`, `CURLU`, `curl_mime`,
`curl_mimepart`, `curl_slist`.

### The structural fact that decides the campaign shape

The surface is not 92 functions. It is ~314 options behind **four variadic
entry points** — `curl_easy_setopt`, `curl_multi_setopt`, `curl_share_setopt`,
`curl_easy_getinfo`. None of that type discipline is in the C type system.

It lives in two machine-readable places in-tree, plus a runtime API:

- `lib/easyoptions.c` — a `name → CURLot_*` table
- `include/curl/typecheck-gcc.h` — the compile-time checking macros
- `curl_easy_option_by_name` / `by_id` / `next` (`include/curl/options.h`)

Option type histogram from `lib/easyoptions.c`:

| `CURLot_*` | rows |
|---|---|
| `LONG` | 110 |
| `STRING` | 90 |
| `CBPTR` | 25 |
| `FUNCTION` | 22 |
| `VALUES` | 21 |
| `SLIST` | 11 |
| `OBJECT` | 11 |
| `BLOB` | 8 |
| `OFF_T` | 7 |
| `FLAG_ALIAS` | 13 |

`curl_easy_getinfo` return types, from the `CURLINFO_*` enum:

| type | count |
|---|---|
| `LONG` | 23 |
| `OFF` | 21 |
| `STRING` | 13 |
| `DOUBLE` | 7 |
| `SLIST` | 2 |
| `PTR` | 2 |
| `SOCKET` | 1 |

This table is an unusually good oracle for generating typed setters, and is the
main reason curl is a better wrap target than its size suggests.

## Existing work

### `curl-rust` — the only real safe wrapper

Actively maintained. ~9k lines of safe layer over 1.2k lines of `curl-sys`,
~118 `unsafe` occurrences.

Adoption, from the crates.io API on 2026-08-23:

| crate | version | all-time downloads | last 90 days | direct dependents | first published | last release |
|---|---|---|---|---|---|---|
| `curl` | `0.4.50` | **43,762,358** | **4,059,782** | **295** | 2014-12-16 | 2026-06-11 |
| `curl-sys` | `0.4.90+curl-8.21.0` | 48,726,497 | 5,023,302 | 31 | 2014-12-12 | 2026-06-29 |
| `isahc` | `2.0.1` | 17,082,211 | 1,248,860 | 145 | 2019-08-03 | 2026-07-05 |

lib.rs reports the same crate as ~1.24M downloads/month, 306 direct and **1004
transitive** dependents — cargo itself among them. `curl-sys` carries more
downloads than `curl` because `isahc` and a few others bind the raw layer
directly, bypassing the safe wrapper entirely.

Measured coverage against the C surface:

| | C | `curl-sys` | safe layer |
|---|---|---|---|
| exported functions | 92 | 42 | **37** |
| `CURLOPT_` | 287 | 222 | **156** |
| `CURLINFO_` | 98 | — | **35** |

Whole interfaces with **no** safe Rust form:

- URL API (`curl_url_*`)
- header API (`curl_easy_header`, `curl_easy_nextheader`)
- WebSockets (`curl_ws_*`)
- share (`curl_share_*`)
- the entire mime API (`curl_mime_*`) — the crate still drives the deprecated
  `curl_formadd`
- option introspection (`curl_easy_option_*`)
- HTTP/2 push (`curl_pushheader_byname`, `curl_pushheader_bynum`)
- `curl_global_sslset`, `curl_global_trace`, `curl_global_init_mem`
- SSL session import/export (`curl_easy_ssls_import`, `curl_easy_ssls_export`)

Two soundness gaps the crate documents against itself:

- `src/easy/handler.rs:3315` — `try_clone` / `curl_easy_duphandle` is commented
  out: *"I don't think this is safe, you can drop this which has all the
  callback data and then the next is use-after-free"*
- `src/multi.rs:178,277` — `// TODO: figure out how to expose _easy` /
  `_multi`; the easy↔multi borrow relationship is unmodeled and worked around
  by not exposing it

### `isahc`

Wraps `curl-sys`, but is an opinionated async HTTP client rather than an
API-surface wrapper. Re-exposes a subset for HTTP only; no rustls path.

### Rust inside curl — retreated

- the ISRG/Prossimo-funded `hyper` HTTP backend was **removed in curl 8.12.0**
  (Feb 2025) for lack of uptake; `docs/DEPRECATE.md` lists it under past
  removals
- `docs/EXPERIMENTAL.md` still marks the rustls TLS backend and quiche
  experimental; rustls' graduation is blocked on "a reasonable expectation of a
  stable API going forward"

Memory-safety work on curl is now confined to TLS and QUIC internals. Nobody is
producing a safe Rust surface over libcurl.

### Automated translation research

DARPA TRACTOR and its 150-program MIT LL benchmark; `&inator` for
constraint-based C-to-Rust *interface* translation; ORBIT for agentic
transpilation; c2rust. All target whole-program transpilation or small-scale
interface translation. `&inator` states that scaling to large programs is
unresolved. None has produced a safe wrapper over a shipping C library at
libcurl's option-surface scale.

## Gaps

1. **~46% of options and ~60% of the functions have no safe Rust form
   anywhere** — not in curl-rust, not in isahc, not elsewhere.
2. **Variadic type discipline is enforced nowhere in Rust.** `curl-sys`
   re-declares `curl_easy_setopt` as variadic; safe callers hand-pass `c_long`
   and raw pointers. The `easyoptions.c` table that would make this typed is
   used by no binding.
3. **Handle-relationship lifetimes are unmodeled** — easy↔multi↔share↔mime↔
   slist. This is exactly what a crustify wrap encodes (DAG layers, the raw
   lifetime tiers, `CDropped` / `CCloned` strategies), and it is the specific
   thing curl-rust gave up on.
4. **`duphandle` is unsound-by-omission** — the operation exists in C, is
   unwrappable by hand, and is simply absent.
5. **Newer, security-relevant surfaces are entirely unwrapped** — the URL
   parser in particular, which Rust callers today reimplement or bypass.
6. **Callback safety** — 30 callback typedefs; curl-rust wraps roughly 8 with
   panic-catching shims.

## Sizing

Type and callback units ≈ 18 structs + 35 enums + 30 callbacks + 7 handles ≈
**90**. Symbols **92**.

At the libgit2 rates in `examples/libgit2/wrappers-results-opus5.md` — `$8.16` per
type, `$0.56` per symbol — that is roughly **`$750` + `$55`**. It should run
cheaper per unit than libgit2: the wrap DAG is shallow because almost
everything behind the API is opaque, so there are no god-object waves.

### The caveat to settle before authoring `oracle-config.json`

**The ~314 options are not C symbols.** `query symbols` will not see them; they
are rows in a table reached through one variadic function. The standard
type and symbol waves will produce a correct wrapper of 92 functions and then
leave the actual surface untouched. curl needs a table-driven generation step
that the current wave taxonomy does not have.

## Local checkouts

- system libcurl is **8.5.0** (`libcurl4-openssl-dev`, headers under
  `/usr/include/x86_64-linux-gnu/curl/`) — 17 minor versions behind the tree
  surveyed here; pin the campaign to a fetched revision, not the system one
- existing checkouts on this host: `/data/marius.momeu/git/ffi-corpus/curl/`,
  plus `curl` and `curl-vulns` trees under the dev-workspace docker volumes. If
  `ffi-corpus/curl` is at a known revision it is the natural
  `campaign-revision` and saves a clone

## References

- <https://github.com/alexcrichton/curl-rust>
- <https://lib.rs/crates/curl>
- <https://github.com/sagebind/isahc>
- <https://daniel.haxx.se/blog/2024/12/21/dropping-hyper/>
- <https://www.phoronix.com/news/cURL-8.12-Released>
- <https://www.abetterinternet.org/post/memory-safe-curl/>
- <https://www.darpa.mil/research/programs/translating-all-c-to-rust>
- <https://www.ll.mit.edu/r-d/projects/translating-all-c-rust-tractor-benchmarks>
- <https://arxiv.org/pdf/2604.17261>
- <https://arxiv.org/pdf/2604.12048>
