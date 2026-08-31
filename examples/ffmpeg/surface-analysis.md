# ffmpeg — wrap surface analysis

Pre-campaign scoping for a `wrap` objective over ffmpeg. Everything below is
measured against a checkout, not quoted: what ffmpeg publishes, what the Rust
ecosystem already reaches, and what a campaign over the remainder costs.

- **measured** — 2026-08-23
- **tree** — `git.ffmpeg.org/ffmpeg` master, `dd38c57676a25daadc44109555cf1bfb27caab97`
- **version** — `RELEASE` = `8.0.git`; libavutil 61, libavcodec 63
- **crates** — rsmpeg `0.18.0+ffmpeg.8.0`, ffmpeg-next `9.0.0`,
  ffmpeg-the-third `6.0.0+ffmpeg-9.0`, ac-ffmpeg `0.19.0`, from crates.io

## Method

The public surface is the union of each library's `HEADERS` in its `Makefile`
(the set `make install` ships), parsed with backslash-continuations resolved.
Within those headers, comments and preprocessor lines are stripped, then:
functions are declarations matching the library's own symbol prefixes; types
are `struct`/`union` bodies plus `typedef struct X X;` opaque handles; fields
are top-level `;` inside a body; callbacks are `(*name)(` declarations, both
typedefs and struct fields.

Ecosystem coverage is measured by downloading each crate's `.crate` from
crates.io and testing whether its sources name a given C function anywhere —
`.rs` for the three FFI crates, `.rs` **and** `.c` for ac-ffmpeg, which reaches
the C ABI through hand-written shims and so names nothing from Rust.

**Caveats.** Naming a function is an upper bound on wrapping it — a crate that
mentions `av_opt_set` once in a helper counts as reaching it. Counts are
regex-derived from headers, not from a compiled tree or a CodeQL extraction, so
treat them as ±5% and re-derive the authoritative numbers from T1/T2 once the
oracle target exists. "Used in Rust" is measured through the four wrapper
crates as a proxy for demand, not through downstream applications.

## What ffmpeg ships

**7 libraries**, per `configure`'s `LIBRARY_LIST`. libpostproc is gone — it was
an 8th, GPL-only library through 7.1 and was removed for 8.0 (upstream patch,
2025-05); it is absent from this tree and `grep -c postproc configure` is 0.

Link topology, from `configure` (4385-4397):

| library | depends on |
|---|---|
| `avutil` | — |
| `avcodec`, `avfilter`, `swscale`, `swresample` | `avutil` |
| `avformat` | `avcodec avutil` |
| `avdevice` | `avformat avcodec avutil` |

| library | public hdrs | hdr LOC | public fns | types | enums | impl LOC |
|---|---|---|---|---|---|---|
| libavutil | 100 | 21,688 | 599 | 158 | 49 | 107k |
| libavcodec | 25 | 8,232 | 181 | 44 | 14 | 1,000k |
| libavformat | 4 | 4,167 | 144 | 18 | 6 | 272k |
| libavfilter | 5 | 1,683 | 66 | 14 | 0 | 278k |
| libswscale | 3 | 850 | 40 | 9 | 6 | 61k |
| libswresample | 3 | 667 | 22 | 1 | 3 | 7k |
| libavdevice | 3 | 478 | 14 | 3 | 2 | 17k |
| **Σ** | **143** | **37,765** | **1,065** | **247** | **80** | **1.74M** |

203 of those types carry a field body in a public header: **1,499 declared
fields, 78 function-pointer declarations**. The cost tail is
`AVCodecContext` (149 fields, 8 fn-ptr), `AVFormatContext` (76),
`AVFrame` (39), `AVCodecParserContext` (35), `AVCodecParameters` (32),
`AVIOContext` (29 fields, 7 fn-ptr).

Whole tree: 1.74M LOC of C across the 7 libraries, 404 asm files, 398
configure options, 3,253 FATE test names.

## What the ecosystem already has

Of the 1,065 public functions, how many each crate's sources name:

| crate | ffmpeg | rs LOC | pub fns | C fns reached | raw ptr in pub sig | `pub unsafe fn` |
|---|---|---|---|---|---|---|
| rsmpeg | 8.0 | 6.4k | 304 | **220 (20.7%)** | 22 (7.2%) | 26 |
| ffmpeg-the-third | 9.0 | 18.8k | 783 | **201 (18.9%)** | 108 (13.8%) | 85 |
| ffmpeg-next | 4-compat fork | 18.1k | 762 | **187 (17.6%)** | 111 (14.6%) | 113 |
| ac-ffmpeg | — | 7.6k + C shims | 355 | **103 (9.7%)** | 19 (5.4%) | 9 |

**Union of all four: 331 / 1,065 = 31.1%.** Intersection of the three FFI
crates: 96 (9.0%). Intersection of all four: 60 (5.6%).

video-rs and ez-ffmpeg sit on top of ffmpeg-next/rsmpeg and add reach of zero;
ffmpeg-sidecar shells out to the binary. Downloads point at ffmpeg-next as the
dominant consumer path (6.5M all-time), then video-rs 347k, ez-ffmpeg 218k,
rsmpeg 164k, ffmpeg-the-third 106k, ac-ffmpeg 55k.

None of the four would pass a `crustify-audit unsafe` gate. Beyond the raw-ptr
and `unsafe fn` columns above: rsmpeg ships an `UnsafeDerefMut` trait as the
sanctioned way to touch struct fields — the field surface is explicitly not
wrapped — and ac-ffmpeg's safety story is a second, unaudited C layer.

## Which libraries Rust actually uses

Three signals: how much wrapper code each library gets (attributing every
crate source file to the library whose functions it names most), how much of it
the crates reached, and how many functions every crate independently needed.

| # | library | C fns | reached (union) | wrapper LOC share | in all 4 |
|---|---|---|---|---|---|
| 1 | **libavutil** | 599 | 151 (25.2%) | **29-43%** (highest everywhere) | 14 |
| 2 | **libavcodec** | 181 | 59 (32.6%) | 15-27% | 17 |
| 3 | **libavformat** | 144 | 41 (28.5%) | 6-16% | 11 |
| 4 | **libswresample** | 22 | 14 (**63.6%**) | 1.5-2.9% | 5 |
| 5 | **libswscale** | 40 | 17 (42.5%) | 2.1-5.4% | 3 |
| 6 | **libavfilter** | 66 | 39 (59.1%) | 2.8-7.1% | 10 |
| 7 | **libavdevice** | 14 | 10 (71.4%) | 1.3-1.4% | 0 |

Per-crate wrapper LOC share:

| | avutil | avcodec | avformat | avfilter | swscale | swresample | avdevice |
|---|---|---|---|---|---|---|---|
| rsmpeg (6.4k) | 42.9% | 14.6% | 16.0% | 7.1% | 5.4% | 2.9% | — |
| ffmpeg-next (18.1k) | 28.9% | 26.5% | 11.9% | 3.5% | 2.5% | 1.5% | 1.4% |
| ffmpeg-the-third (18.8k) | 30.9% | 27.2% | 6.1% | 2.8% | 2.1% | 1.8% | 1.3% |

**The ratios invert the usage story.** libavdevice has the best coverage ratio
(71.4%) and is the least used — 14 functions total, and rsmpeg and ac-ffmpeg
do not wrap it at all. libavutil has the worst ratio (25.2%) and is the most
used: largest share of wrapper code in every crate, because that is where the
shared types live. Its low ratio is 448 unwrapped functions, not low demand.

libavfilter is wide but shallow — 59% reached on 2.8-7.1% of the effort,
because every crate wraps the same ten functions and drives the graph with a
filter *string*. Nobody types the graph description.

**The hot core is 60 functions, 5.6% of the API** — what all four crates
converged on independently:

- `libavcodec` (17) — `avcodec_{alloc_context3,open2,free_context}`,
  `avcodec_{send,receive}_{frame,packet}`, `avcodec_find_{de,en}coder[_by_name]`,
  the `AVCodecParameters` family, `av_packet_rescale_ts`
- `libavutil` (14) — `av_frame_{alloc,free,get_buffer}`, `av_dict_{set,free}`,
  `av_{free,freep}`, `av_opt_set`, `av_strerror`,
  `av_channel_layout_default`, sample/pixel format lookups
- `libavformat` (11) — `avformat_{open_input,close_input,find_stream_info,new_stream,write_header,alloc_context,free_context}`,
  `av_read_frame`, `av_{interleaved_,}write_frame`, `av_write_trailer`
- `libavfilter` (10) — `avfilter_graph_{alloc,parse_ptr,config,create_filter,free}`,
  `avfilter_inout_{alloc,free}`, `avfilter_get_by_name`,
  `av_buffersrc_add_frame`, `av_buffersink_get_frame`
- `libswresample` (5) — `swr_{alloc_set_opts2,init,convert_frame,get_delay,free}`
- `libswscale` (3) — `sws_{getContext,scale,freeContext}`

Feature gates carry no signal: ffmpeg-next and ffmpeg-the-third both default to
`codec, device, filter, format, software-resampling, software-scaling`, so all
seven libraries link out of the box. Demand shows up in effort, not in gates.

## Which surfaces are missing

Per-header, counting functions **no** crate references:

| header | fns | covered | what it is |
|---|---|---|---|
| `libavformat/avformat.h` | 87 | 36.8% | demux/mux long tail |
| `libavformat/avio.h` | 57 | 15.8% | custom I/O, 11 fn-ptr decls |
| `libavutil/opt.h` | 53 | 24.5% | the runtime config surface, string-keyed over `void*` |
| `libavcodec/avcodec.h` | 41 | 51.2% | |
| `libswscale/swscale.h` | 40 | 42.5% | |
| `libavfilter/avfilter.h` | 39 | 53.8% | |
| `libavcodec/bsf.h` | 35 | 25.7% | bitstream filters |
| `libavutil/pixdesc.h` | 31 | 51.6% | |
| `libavcodec/packet.h` | 31 | 45.2% | |
| `libavutil/frame.h` | 27 | 48.1% | |
| `libavutil/avstring.h` | 19 | 5.3% | NUL-terminated string lifecycles |
| `libavutil/mem.h` | 18 | 27.8% | allocator seam |
| `libavutil/imgutils.h` | 18 | 33.3% | plane/stride arithmetic |
| `libavutil/{fifo,hash,bprint,refstruct,timecode,csp,encryption_info,parseutils,stereo3d,threadmessage,container_fifo,eval,aes_ctr,iamf}` | 143 | **0%** | 14 subsystems, entirely unwrapped |
| `libavcodec/{exif,smpte_436m}` | 23 | **0%** | metadata |
| `libavutil/hwcontext_*` (14 vendor hdrs) | ~20 | **0%** | only the `hwcontext.h` base is at 72% |

107 of the 110 headers that publish a function have at least one no crate
touches.

The highest-value targets are the ones that are both unwrapped **and**
memory-unsafe by construction: `avio.h` (Rust closures called from C),
`opt.h` (type-erased `void*` writes into struct fields), `avstring.h` and
`mem.h` (the raw lifetime discovery tiers), and the `AVCodecContext` /
`AVFormatContext` field surfaces rsmpeg explicitly declines to wrap.

## Workload

Calibration is the libgit2 opus5 run (`examples/libgit2/wrappers-results-opus5.md`):
**$8.16 per type-or-callback unit** at `--max-types 1`, **$0.56 per symbol**,
~12m mean agent wall. `--max-types 2` batches types and takes roughly 30-40%
off the type line. Neither figure includes a review pass; budget +25-40% for
one under a second model.

| scope | fns | type units | est. $ (`--max-types 1`) | est. $ (`--max-types 2`) |
|---|---|---|---|---|
| swscale + swresample (pilot) | 62 | ~19 | $150-250 | ~$120-200 |
| **libavutil, all 100 headers** | 598 | 223 | ~$2,350 | ~$1,600 |
| **libavutil, minus SDK-bound hwcontext** | 560 | 182 | ~$1,900 | ~$1,300 |
| **libavutil + libavcodec** | 779 | 292 | **$3,000-3,300** | **$2,100-2,400** |
| ...minus SDK-bound hwcontext | 759 | 251 | $2,600-2,900 | ~$1,900 |
| all 7 libraries | 1,065 | ~400 | $4,000-5,200 | ~$3,000-3,600 |

Type units = types + enums + function-pointer declarations. Each line adds
$20-40 for the two raw-lifetime tiers and $120-250 for the libc imported
closure. Wall at `--parallel-max 32`: ~6-10h of wave time for
libavutil + libavcodec, ~15-25h for the full surface, plus landing and the
per-wave `crustify-audit unsafe` scan.

**libavutil + libavcodec is the natural cut.** `avcodec_deps="avutil"`, so the
two are closed under themselves and nothing but libc enters the imported
closure. Together they are 73% of ffmpeg's public functions and 78% of its
entire public field surface; they cover 210 of the 331 functions the whole
ecosystem reaches (63%) plus **569 functions no Rust crate has ever named**.

| | public hdrs | hdr LOC | fns | types | bodied | enums | fn-ptr | fields |
|---|---|---|---|---|---|---|---|---|
| libavutil | 100 | 21,688 | 598 | 150 | 137 | 49 | 24 | 736 |
| libavcodec | 25 | 8,232 | 181 | 42 | 37 | 14 | 13 | 427 |
| **Σ** | **125** | **29,920** | **779** | **192** | **174** | **63** | **37** | **1,163** |

The cost tail in that scope is `AVCodecContext` alone — 149 fields, 8
function-pointer fields, one unit. libgit2's worst type (`ntlm_client`, 38
fields) cost $16.35; extrapolating puts this one at $40-70 and makes it the
wave most likely to need splitting. `AVFormatContext` is out of this scope.

## The hwcontext decision

The 15 `hwcontext_*.h` headers sit in libavutil's unconditional `HEADERS`
list, so `make install` ships them. 11 of the 15 `#include` a foreign SDK
header:

| header | pulls in |
|---|---|
| `hwcontext_vulkan.h` | `<vulkan/vulkan.h>` |
| `hwcontext_cuda.h` | `<cuda.h>` |
| `hwcontext_vaapi.h` | `<va/va.h>` |
| `hwcontext_d3d11va.h` | `<d3d11.h>` |
| `hwcontext_d3d12va.h` | `<d3d12.h>`, `<d3d12video.h>`, `<d3d12sdklayers.h>` |
| `hwcontext_dxva2.h` | `<d3d9.h>`, `<dxva2api.h>` |
| `hwcontext_qsv.h` | `<mfxvideo.h>` |
| `hwcontext_opencl.h` | `<CL/cl.h>` |
| `hwcontext_amf.h` | 5 `<AMF/...>` headers |
| `hwcontext_vdpau.h` | `<vdpau/vdpau.h>` |
| `hwcontext_videotoolbox.h` | `<VideoToolbox/VideoToolbox.h>` |

Those 11 carry **20 functions but 29 types, 2 enums, 10 fn-ptr decls and 120
fields** — 41 type units, ~$335 at `--max-types 1` — and every SDK type they
name lands in the imported closure as an opaque handle wrapped for no benefit.
Ecosystem coverage of those 20 functions is **0**.

`hwcontext.h` itself (18 fns, 72% covered) and `hwcontext_drm.h`,
`hwcontext_mediacodec.h`, `hwcontext_oh.h` have no foreign include and are
fine to keep.

**Take the drop through the build, not through `out_of_scope`.** Configure
without vulkan/cuda/vaapi/qsv/opencl/amf/vdpau/dxva2/d3d11va/d3d12va and those
headers never compile, never reach the CodeQL database, and T1 anchoring drops
the uncompiled candidates on its own.

## Setup notes

Setup is heavier than any bench target so far — 1.74M LOC of C against
libgit2's ~250k.

- **CodeQL** — hours, not minutes, and a correspondingly large T1/T2
  extraction.
- **Build lever** — libavcodec is 1.0M LOC, nearly all of it codec
  implementations a wrap campaign never touches. A `--disable-everything`
  configure with a small decoder/encoder set keeps the database and the FATE
  baseline tractable without losing any public API: every public entry point
  lives in the always-built core files. The playbook does not re-check the C
  build or tests for a wrap wave, so a minimal build costs nothing here.
- **FATE** — 3,253 test names, and the samples rsync is several GB. Needed for
  the campaign's test baseline even though a wrap campaign will not move it.
- **Sanitizers** — enable them in the `build` command per the playbook, so
  agents catch memory-safety violations when testing Rust against C.

## Suggested campaign order

1. **Pilot** — swscale + swresample. ~$200, small, and the ecosystem has
   already covered them best (42.5% / 63.6%), so there is a dense external
   oracle to check the emitted wrappers against. Exercises opaque handles
   (`SwsContext`), plane-pointer arrays and `AVFrame` borrows.
2. **Core** — libavutil + libavcodec, SDK-bound hwcontext headers dropped at
   configure time. The scope priced above.
3. **Rest** — libavformat (and with it `avio.h`, the highest-value unsafe
   surface in the tree), then libavfilter, libavdevice, and the hwcontext
   vendor headers only if a consumer asks for them.
