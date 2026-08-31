# crustify — `libgit2 / src`

Campaign: libgit2 @ `ddf3b5c85`, target section `src/` + `include/` (409 files),
import section 102. Agent backend `claude_cli`, model `anthropic/claude-opus-5`,
`--billing api`, `--max-types 1`, `--parallel-max 32`, policy `per-agent`.
Deps: crustify `05b76b2` (**ref**), ffibox `7282e05` (**ref**).
Campaign branch `crustify/src-opus5`; `crustify/src` is the untouched scaffold
baseline. Cost is priced from token counts by `log_cost.py`'s own
`parse_usage`, never from provider-reported dollars.

**Legend** — as the task template defines it. `wrapped` counts DISTINCT
`type.field` paths that reached a promoted `/// Field:` anchor; `—` means
wrapped with no field accessor (opaque). `newtypes` is the number of distinct
Rust types carrying a `/// Wraps: <tag>` anchor; `>1` is one C type forked into
several representations. `wall` is `ended_at − started_at` from that agent's
own `usage.json`, so it INCLUDES its per-worktree C rebuild. In the batches
table `wall` is the layer's LONGEST agent — what the layer would cost with
every batch spawned at once — and the parenthetical is the serial-sum multiple.

## Overview

Every unit the campaign emitted, across all four waves. `units` counts agents'
worklist entries.

| wave | kind | units | loc | $ | $/type | $/symbol | wall |
|---|---|---|---|---|---|---|---|
| raw lifetime | symbols | `2` tiers | — | `$16.63` | — | `$0.49` | `37m45s` |
| import closure | types + callbacks | `77` | `41,946` | `$549.65` | `$7.14` | — | `1h19m29s` |
| import closure | symbols | `86` | `6,135` | `$47.88` | — | `$0.56` | `1h09m10s` |
| god objects | types + callbacks | `72` | `61,570` | `$666.47` | `$9.26` | — | `3h17m41s` |
| **Σ** | | **`237`** | **`109,651`** | **`$1280.63`** | **`$8.16`** | **`$0.56`** | **`6h24m05s`** |

Types and callbacks across both waves: `149` units, `780` declared fields of
which `549` reached an accessor, `136` of the `483` target-touched fields
pointers, forked into `198` newtypes over `103,516` loc for `$1216.12` —
`$8.16` per unit, `$0.012` per loc, `$1.56` per declared field.

Symbols: `499` target-section functions needed the `71` wrapped items, which
took `99` distinct safe fns over `6,135` loc for `$47.88` — `$0.56` per unit,
`$0.008` per loc, two orders of magnitude under a type.

Tree-wide: `$0.012` per loc. Wall is the sum of the four waves' own elapsed
time; the import type wave alone would have run `17h43m` serially.

## Raw lifetime discovery

Goal: turn the untyped lifecycle primitives into Rust lifetime contracts before
any wrapper needs one. `--lifetime-for void` then `--lifetime-for string`, one
agent each, objective `raw` (set by the tier, not `--objective`). `strategies`
counts the deleter/cloner ZSTs emitted; the four trait columns count the
`unsafe impl`s that bind them.

| tier | symbols submitted | strategies | CDropped | CCloned | CLenDropped | CLenCloned | $ | wall |
|---|---|---|---|---|---|---|---|---|
| void | `16` | `2` | `2` | `0` | `2` | `0` | `$7.28` | `17m27s` |
| string | `18` | `5` | `4` | `4` | `5` | `0` | `$9.35` | `20m18s` |
| **Σ** | **`34`** | **`7`** | **`6`** | **`4`** | **`7`** | **`0`** | **`$16.63`** | **`37m45s`** |


## Import closure

Goal: wrap everything the target section reaches but does not own — first every
import-section type and callback, then the import-section symbols the target
depends on. Two waves, both `--objective wrap`.

### Types and callbacks

The import type closure: every import-section type and callback, bottom-up by
its own DAG layer, `--max-types 1` so each type is its own agent. 77 of 79
units; `struct stat` is gate-missed (see Notes).

| layer | id | unit | kind | fields | port | target ptr | wrapped | newtypes | $ | wall | loc |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | WT1 | `GENERAL_NAME_st` | struct | 17 | 3 | 1 | 2 | 1 | $10.37 | 19m18s | 1112 |
| 0 | WT2 | `SHA256state_st` | struct | 6 | — | — | — | 2 | $3.36 | 8m24s | 266 |
| 1 | WC1 | `SSL_verify_cb` | callback | — | — | — | — | 1 | $4.57 | 10m29s | 692 |
| 0 | WT3 | `X509_name_entry_st` | struct | — | — | — | — | 1 | $5.20 | 10m54s | 279 |
| 0 | WT4 | `X509_name_st` | struct | — | — | — | — | 1 | $7.71 | 14m09s | 306 |
| 0 | WT5 | `_IO_FILE` | struct | 29 | — | — | — | 2 | $4.66 | 11m11s | 203 |
| 0 | WT6 | `__CONST_SOCKADDR_ARG` | struct | 13 | — | — | 1 | 2 | $4.54 | 12m00s | 454 |
| 0 | WC2 | `__compar_d_fn_t` | callback | — | — | — | — | 1 | $11.17 | 19m15s | 1045 |
| 0 | WT7 | `__dirstream` | struct | — | — | — | — | 1 | $3.34 | 7m34s | 212 |
| 0 | WT8 | `__sigset_t` | struct | 1 | — | — | — | 1 | $5.52 | 10m45s | 316 |
| 1 | WT9 | `addrinfo` | struct | 8 | 6 | 2 | 6 | 1 | $6.51 | 14m28s | 927 |
| 0 | WT10 | `asn1_string_st` | struct | 4 | — | — | — | 1 | $5.62 | 12m39s | 483 |
| 0 | WT11 | `bio_method_st` | struct | — | — | — | — | 1 | $4.30 | 9m56s | 252 |
| 0 | WT12 | `bio_st` | struct | — | — | — | — | 5 | $9.35 | 16m42s | 465 |
| 0 | WT13 | `dirent` | struct | 5 | 1 | — | 1 | 1 | $4.90 | 10m21s | 276 |
| 0 | WT14 | `in6_addr` | struct | 4 | — | — | — | 1 | $6.21 | 12m28s | 428 |
| 0 | WT15 | `in_addr` | struct | 1 | — | — | 1 | 1 | $4.23 | 8m48s | 271 |
| 0 | WT16 | `llhttp__internal_s` | struct | 21 | 3 | 1 | 3 | 1 | $7.20 | 14m31s | 496 |
| 0 | WT17 | `llhttp_errno` | enum | — | — | — | — | 1 | $2.92 | 6m45s | 379 |
| 2 | WT18 | `llhttp_settings_s` | struct | 23 | 5 | 5 | 5 | 2 | $8.53 | 16m38s | 1256 |
| 0 | WT19 | `llhttp_type` | enum | — | — | — | — | 1 | $1.74 | 4m40s | 181 |
| 0 | WT20 | `ntlm_buf` | struct | 3 | — | — | 3 | 3 | $7.83 | 18m13s | 986 |
| 0 | WT21 | `ntlm_challenge` | struct | 10 | — | — | 10 | 1 | $7.80 | 14m28s | 730 |
| 0 | WT22 | `ntlm_client` | struct | 38 | — | — | 36 | 1 | $16.35 | 24m00s | 516 |
| 0 | WT23 | `ntlm_client_flags` | enum | — | — | — | — | 1 | $3.38 | 7m54s | 516 |
| 0 | WT24 | `ntlm_state` | enum | — | — | — | — | 1 | $2.31 | 5m50s | 235 |
| 0 | WT25 | `ntlm_version` | struct | 4 | — | — | 4 | 1 | $4.04 | 8m19s | 332 |
| 0 | WT26 | `ossl_init_settings_st` | struct | — | — | — | — | 1 | $3.99 | 9m39s | 181 |
| 0 | WT27 | `passwd` | struct | 7 | 1 | 1 | 1 | 4 | $6.27 | 14m23s | 482 |
| 0 | WT28 | `pcre2_real_code_8` | struct | — | — | — | — | 3 | $6.05 | 11m31s | 276 |
| 0 | WT29 | `pcre2_real_compile_context_8` | struct | — | — | — | — | 2 | $6.09 | 13m08s | 289 |
| 0 | WT30 | `pcre2_real_general_context_8` | struct | — | — | — | — | 1 | $8.70 | 16m58s | 297 |
| 0 | WT31 | `pcre2_real_match_context_8` | struct | — | — | — | — | 1 | $6.50 | 14m19s | 260 |
| 0 | WT32 | `pcre2_real_match_data_8` | struct | — | — | — | — | 1 | $3.93 | 8m38s | 278 |
| 0 | WT33 | `pollfd` | struct | 3 | 3 | — | 3 | 1 | $3.05 | 7m04s | 301 |
| 0 | WT34 | `pthread_attr_t` | struct | 2 | — | — | — | 1 | $5.33 | 11m11s | 348 |
| 0 | WT35 | `pthread_cond_t` | struct | 3 | — | — | — | 1 | $6.28 | 13m27s | 445 |
| 0 | WT36 | `pthread_condattr_t` | struct | 2 | — | — | — | 1 | $9.08 | 15m47s | 388 |
| 0 | WT37 | `pthread_mutex_t` | struct | 3 | — | — | — | 1 | $15.63 | 25m34s | 578 |
| 0 | WT38 | `pthread_mutexattr_t` | struct | 2 | — | — | — | 1 | $8.90 | 16m30s | 452 |
| 0 | WT39 | `pthread_rwlock_t` | struct | 3 | — | — | — | 1 | $5.80 | 11m46s | 395 |
| 0 | WT40 | `pthread_rwlockattr_t` | struct | 2 | — | — | — | 1 | $9.92 | 17m09s | 374 |
| 0 | WT41 | `reftable_buf` | struct | 3 | — | — | 3 | 1 | $10.10 | 20m11s | 621 |
| 0 | WT42 | `reftable_error` | enum | — | — | — | — | 1 | $4.33 | 9m09s | 558 |
| 0 | WT43 | `reftable_hash` | enum | — | — | — | — | 1 | $2.61 | 6m58s | 363 |
| 0 | WT44 | `reftable_iterator` | struct | 2 | — | — | 2 | 1 | $12.00 | 20m12s | 717 |
| 0 | WT45 | `reftable_iterator_vtable` | struct | 3 | — | — | 3 | 1 | $4.97 | 12m24s | 717 |
| 0 | WT46 | `reftable_log_expiry_config` | struct | 2 | — | — | 2 | 1 | $4.44 | 9m29s | 349 |
| 0 | WT47 | `reftable_log_record` | struct | 14 | 12 | 4 | 4 | 1 | $13.21 | 21m50s | 1344 |
| 0 | WT48 | `reftable_merged_table` | struct | 6 | — | — | 1 | 1 | $5.51 | 10m35s | 309 |
| 0 | WT49 | `reftable_record` | struct | 6 | — | — | 2 | 1 | $18.51 | 28m52s | 1221 |
| 0 | WT50 | `reftable_ref_record` | struct | 10 | 9 | 2 | 5 | 1 | $9.38 | 17m25s | 1201 |
| 0 | WT51 | `reftable_stack` | struct | 9 | — | — | 7 | 1 | $13.56 | 21m39s | 1065 |
| 1 | WT52 | `reftable_stack_options` | struct | 4 | 2 | — | 2 | 1 | $6.50 | 12m24s | 538 |
| 0 | WT53 | `reftable_table` | struct | 13 | — | — | — | 2 | $5.26 | 10m28s | 358 |
| 0 | WT54 | `reftable_write_options` | struct | 9 | 1 | — | 1 | 1 | $3.32 | 7m41s | 331 |
| 0 | WT55 | `reftable_writer` | struct | 19 | — | — | 4 | 2 | $13.09 | 21m21s | 561 |
| 0 | WT56 | `s_mmbuffer` | struct | 2 | 2 | 1 | 2 | 2 | $13.46 | 22m34s | 773 |
| 0 | WT57 | `s_mmfile` | struct | 2 | 2 | 1 | 2 | 2 | $8.54 | 16m39s | 666 |
| 0 | WT58 | `s_xdchange` | struct | 6 | — | — | 6 | 1 | $10.21 | 17m01s | 583 |
| 1 | WT59 | `s_xdemitcb` | struct | 3 | 2 | 2 | 3 | 2 | $9.35 | 16m39s | 1346 |
| 1 | WT60 | `s_xdemitconf` | struct | 6 | 6 | 3 | 6 | 1 | $16.03 | 23m06s | 1351 |
| 0 | WT61 | `s_xdfenv` | struct | 2 | — | — | 2 | 1 | $9.18 | 17m38s | 693 |
| 1 | WT62 | `s_xmparam` | struct | 8 | 8 | 3 | 8 | 2 | $6.89 | 15m07s | 1320 |
| 0 | WT63 | `s_xpparam` | struct | 5 | 1 | — | 2 | 1 | $10.26 | 18m11s | 517 |
| 0 | WT64 | `sockaddr` | struct | 2 | — | — | — | 1 | $6.47 | 13m06s | 334 |
| 0 | WT65 | `ssl_ctx_st` | struct | — | — | — | — | 1 | $7.40 | 15m07s | 369 |
| 0 | WT66 | `ssl_method_st` | struct | — | — | — | — | 1 | $3.01 | 7m20s | 285 |
| 0 | WT67 | `ssl_st` | struct | — | — | — | — | 1 | $4.65 | 10m12s | 330 |
| 0 | WT68 | `stack_st` | struct | — | — | — | — | 1 | $9.49 | 18m47s | 808 |
| 1 | WT69 | `stack_st_GENERAL_NAME` | struct | — | — | — | — | 4 | $6.68 | 14m44s | 493 |
| 0 | WT70 | `timespec` | struct | 2 | 2 | — | 2 | 1 | $6.01 | 12m50s | 533 |
| 0 | WT71 | `timeval` | struct | 2 | 2 | — | 2 | 1 | $4.35 | 9m30s | 371 |
| 0 | WT72 | `tm` | struct | 11 | 8 | — | 8 | 4 | $5.44 | 11m45s | 585 |
| 0 | WT73 | `x509_st` | struct | — | — | — | — | 5 | $7.88 | 13m34s | 278 |
| 0 | WT74 | `x509_store_st` | struct | — | — | — | — | 4 | $2.70 | 7m02s | 278 |
| 0 | WT75 | `z_stream_s` | struct | 14 | 5 | 3 | 5 | 2 | $5.68 | 14m46s | 822 |
| **Σ 77** | | | | **379** | **84** | **29** | **160** | **113** | **$549.70** | | **41946** |

### Batches — types

| layer | units | loc | $ | wall (longest) | wall (actual) | serial Σ | $/unit | $/loc |
|---|---|---|---|---|---|---|---|---|
| `0` | `68` | `32,911` | `$474.22` | `28m52s` | **`39m41s`** (queued) | `15h21m` (`31.9`x) | `$6.97` | `$0.014` |
| `1` | `8` | `7,779` | `$66.90` | `23m06s` | `23m09s` | `2h06m` (`5.5`x) | `$8.36` | `$0.009` |
| `2` | `1` | `1,256` | `$8.53` | `16m38s` | `16m39s` | `0h16m` (`1.0`x) | `$8.53` | `$0.007` |
| **Σ** | **`77`** | **`41,946`** | **`$549.65`** | — | **`1h19m29s`** | **`17h43m`** (`36.9`x) | **`$7.14`** | **`$0.013`** |

### Symbols

The import symbol closure: the functions and globals target-section code at
layers 0–2 calls but does not own — the direct symbol deps of all 933
target-section symbols at those layers, intersected with the import closure.
86 units, 72 distinct C items.

| layer | symbol | kind | target fns | deps | wrappers | batch | canon |
|---|---|---|---|---|---|---|---|
| `0` | `ERR_error_string_n` | function_exported | `2` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `ERR_get_error` | function_exported | `3` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `__ctype_b_loc` | function_exported | `57` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `__ctype_tolower_loc` | function_exported | `17` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `__ctype_toupper_loc` | function_exported | `4` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `__errno_location` | function_exported | `57` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `access` | function_exported | `2` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `close` | function_exported | `55` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `difftime` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `fsync` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `getcwd` | function_exported | `1` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `getsockopt` | function_exported | `1` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `link` | function_exported | `2` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `malloc` | function_exported | `10` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `mmap` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `munmap` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `open` | function_exported | `6` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `pthread_getspecific` | function_exported | `1` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `pthread_key_create` | function_exported | `1` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `pthread_key_delete` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `pthread_setspecific` | function_exported | `1` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `read` | function_exported | `4` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `realloc` | function_exported | `3` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `reftable_error_str` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `rename` | function_exported | `1` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `strcasecmp` | function_exported | `27` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `strerror` | function_exported | `2` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `strncasecmp` | function_exported | `7` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `strtol` | function_exported | `4` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `strtoul` | function_exported | `5` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `sysconf` | function_exported | `3` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `time` | function_exported | `6` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `unlink` | function_exported | `22` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `waitpid` | function_exported | `2` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `0` | `write` | function_exported | `4` | — | `1` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| `1` | `BIO_set_data` | function_exported | `3` | `bio_st` | `3` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `BIO_set_init` | function_exported | `1` | `bio_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `SHA256_Final` | function_exported | `1` | `SHA256state_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `SHA256_Init` | function_exported | `1` | `SHA256state_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `SHA256_Update` | function_exported | `1` | `SHA256state_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `SSL_CTX_get_cert_store` | function_exported | `1` | `ssl_ctx_st`, `x509_store_st` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `SSL_CTX_load_verify_locations` | function_exported | `1` | `ssl_ctx_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `SSL_get_error` | function_exported | `1` | `ssl_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `X509_STORE_add_cert` | function_exported | `1` | `x509_store_st`, `x509_st` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `clock_gettime` | function_exported | `1` | `timespec` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `deflateReset` | function_exported | `2` | `z_stream_s` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `fprintf` | function_exported | `16` | `_IO_FILE` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `futimens` | function_exported | `1` | `timespec` | `3` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `gettimeofday` | function_exported | `2` | `timeval` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `gmtime_r` | function_exported | `5` | `tm` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `inflateReset` | function_exported | `2` | `z_stream_s` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `llhttp_execute` | function_exported | `1` | `llhttp__internal_s`, `llhttp_errno` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `llhttp_finish` | function_exported | `1` | `llhttp__internal_s`, `llhttp_errno` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `llhttp_get_error_pos` | function_exported | `1` | `llhttp__internal_s` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `llhttp_resume_after_upgrade` | function_exported | `1` | `llhttp__internal_s` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `localtime_r` | function_exported | `3` | `tm` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `mktime` | function_exported | `3` | `tm` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `pcre2_match_8` | function_exported | `2` | `pcre2_real_match_data_8`, `pcre2_real_code_8`, `pcre2_real_match_context_8` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `pcre2_match_data_create_8` | function_exported | `2` | `pcre2_real_match_data_8`, `pcre2_real_general_context_8` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `pthread_mutex_lock` | function_exported | `55` | `pthread_mutex_t` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `pthread_mutex_unlock` | function_exported | `55` | `pthread_mutex_t` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `pthread_rwlock_init` | function_exported | `5` | `pthread_rwlockattr_t`, `pthread_rwlock_t` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `pthread_sigmask` | function_exported | `2` | `__sigset_t` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `qsort_r` | function_exported | `1` | — | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `sigaddset` | function_exported | `2` | `sigset_t` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `sigemptyset` | function_exported | `2` | `sigset_t` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `sigismember` | function_exported | `1` | `sigset_t` | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `signal` | function_exported | `1` | — | `1` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `sigpending` | function_exported | `1` | `sigset_t` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `1` | `sigwait` | function_exported | `1` | `sigset_t` | `2` | `wrap-symbol_BIO_set_data` | `crustify/src-opus5` |
| `—` | `strdup` | function_exported | `3` | — | `2` | `wrap-symbol_ERR_error_string_n` | `crustify/src-opus5` |
| **Σ `71`** | | | **`499`** | | **`99`** | **`2` batches** | **`71`/`71`** |

### Batches — symbols

| layer | units | loc | $ | wall | $/unit | $/loc |
|---|---|---|---|---|---|---|
| `0` | `49` | `3350` | `$15.81` | `30m59s` (`1.0`x) | `$0.32` | `$0.005` |
| `1` | `37` | `2785` | `$32.07` | `38m11s` (`1.0`x) | `$0.87` | `$0.012` |
| **Σ** | **`86`** | **`6135`** | **`$47.88`** | **`1h09m10s`** (`1.0`x, sum = session wall) | **`$0.56`** | **`$0.008`** |

## God objects

Goal: the three target-section types with more than 25 declared fields —
`git_indexer`, `git_packbuilder`, `git_repository` — and their transitive
closure, which `--transitive` expands across symbols so a type reachable only
through a function comes along. 72 units over 8 dependency layers.

### Types and callbacks

| layer | id | unit | kind | fields | port | target ptr | wrapped | newtypes | $ | wall | loc | canon |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `0` | WC1 | `GIT_HASHMAP_STRUCT` | callback | `—` | `—` | `—` | `—` | `1` | $9.25 | 17m30s | `1208` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WC2 | `collision_block_callback` | callback | `—` | `—` | `—` | `—` | `1` | $5.23 | 11m18s | `917` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WC3 | `git_array_t` | callback | `—` | `—` | `—` | `3` | `2` | $8.89 | 17m43s | `1065` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT1 | `git_atomic32` | struct | `1` | `1` | `—` | `1` | `1` | $6.51 | 11m57s | `360` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT2 | `git_attr_file_entry` | struct | `3` | `3` | `2` | `3` | `1` | $9.88 | 15m47s | `837` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT3 | `git_attr_fnmatch` | struct | `5` | `5` | `2` | `5` | `1` | $12.44 | 19m22s | `1160` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT4 | `git_diff_driver_t` | enum | `—` | `—` | `—` | `—` | `1` | $3.07 | 6m53s | `297` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT5 | `git_hash_algorithm_t` | enum | `—` | `—` | `—` | `—` | `1` | $6.06 | 11m24s | `483` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT6 | `git_index_time` | struct | `2` | `2` | `—` | `2` | `2` | $5.39 | 10m22s | `462` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT7 | `git_indexer_progress` | struct | `7` | `7` | `—` | `7` | `1` | $4.09 | 9m22s | `447` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT8 | `git_map` | struct | `2` | `2` | `1` | `2` | `1` | $8.81 | 18m05s | `841` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT9 | `git_object_t` | enum | `—` | `—` | `—` | `—` | `1` | $8.83 | 14m29s | `481` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT10 | `git_oid` | struct | `1` | `1` | `—` | `1` | `1` | $9.68 | 15m12s | `386` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT11 | `git_oid_t` | enum | `—` | `—` | `—` | `—` | `2` | $6.65 | 12m09s | `386` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT12 | `git_pack_header` | struct | `3` | `3` | `—` | `3` | `1` | $7.76 | 12m43s | `544` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WC4 | `git_packbuilder_progress` | callback | `—` | `—` | `—` | `—` | `2` | ↖ batched | — | `917` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT13 | `git_pool_page` | struct | `4` | `4` | `1` | `4` | `1` | $7.37 | 16m21s | `1011` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT14 | `git_refdb_t` | enum | `—` | `—` | `—` | `—` | `1` | $6.79 | 13m10s | `434` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT15 | `git_str` | struct | `3` | `3` | `1` | `3` | `1` | $11.24 | 21m16s | `1203` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WC5 | `git_vector_cmp` | callback | `—` | `—` | `—` | `—` | `3` | ↖ batched | — | `917` | `wrap-2026-08-17_00-00-50_fd16` |
| `0` | WT16 | `git_zstream_t` | enum | `—` | `—` | `—` | `—` | `1` | $5.12 | 10m47s | `340` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT17 | `SHA1_CTX` | struct | `14` | `14` | `1` | `14` | `2` | $8.52 | 16m45s | `1370` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT18 | `git_array_oid_t` | struct | `3` | `3` | `1` | `3` | `3` | $5.99 | 10m26s | `410` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT19 | `git_attr_cache_filemap` | struct | `7` | `7` | `3` | `7` | `1` | $6.10 | 12m30s | `605` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT20 | `git_cache` | struct | `3` | `3` | `—` | `3` | `1` | $11.57 | 20m53s | `999` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT21 | `git_commit_graph_file` | struct | `9` | `9` | `4` | `9` | `1` | $9.18 | 18m30s | `1514` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT22 | `git_diff_driver_pattern` | struct | `2` | `2` | `1` | `2` | `1` | $8.25 | 15m13s | `871` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT23 | `git_futils_filestamp` | struct | `3` | `3` | `—` | `3` | `1` | $6.44 | 12m13s | `623` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT24 | `git_hash_sha256_ctx` | struct | `1` | `1` | `—` | `1` | `1` | $5.46 | 11m18s | `524` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT25 | `git_index_entry` | struct | `12` | `12` | `1` | `12` | `1` | $8.00 | 13m55s | `800` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT26 | `git_indexer_oidmap` | struct | `7` | `7` | `3` | `2` | `1` | $11.42 | 18m40s | `627` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WC6 | `git_indexer_progress_cb` | callback | `—` | `—` | `—` | `—` | `2` | $4.20 | 8m12s | `350` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT27 | `git_mwindow` | struct | `5` | `5` | `1` | `5` | `1` | $7.75 | 14m17s | `1020` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT28 | `git_odb_options` | struct | `2` | `2` | `—` | `2` | `1` | $6.30 | 11m44s | `651` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT29 | `git_pack_oidmap` | struct | `7` | `7` | `3` | `7` | `1` | $9.80 | 16m16s | `553` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT30 | `git_pobject` | struct | `15` | `14` | `4` | `14` | `1` | $14.72 | 22m25s | `1617` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT31 | `git_pool` | struct | `3` | `3` | `1` | `3` | `1` | $7.21 | 13m19s | `1011` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT32 | `git_rawobj` | struct | `3` | `3` | `1` | `3` | `2` | $10.09 | 17m02s | `1108` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT33 | `git_refcount` | struct | `2` | `2` | `1` | `2` | `1` | $10.06 | 17m35s | `924` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT34 | `git_submodule_cache` | struct | `7` | `7` | `3` | `7` | `1` | $12.94 | 21m41s | `856` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT35 | `git_tree_cache` | struct | `7` | `7` | `1` | `7` | `1` | $14.47 | 24m16s | `1537` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT36 | `git_vector` | struct | `5` | `5` | `2` | `5` | `1` | $13.25 | 22m59s | `917` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT37 | `git_zstream` | struct | `6` | `6` | `1` | `6` | `1` | $10.47 | 19m31s | `340` | `wrap-2026-08-17_00-00-50_fd16` |
| `1` | WT38 | `walk_object` | struct | `3` | `3` | `—` | `3` | `1` | $5.96 | 11m55s | `616` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT39 | `git_attr_rule` | struct | `2` | `2` | `—` | `2` | `1` | $7.20 | 14m26s | `783` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT40 | `git_commit_graft` | struct | `2` | `2` | `—` | `2` | `1` | $6.41 | 11m36s | `686` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT41 | `git_commit_graph` | struct | `4` | `4` | `1` | `4` | `1` | $8.42 | 13m41s | `1514` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT42 | `git_config` | struct | `3` | `3` | `—` | `3` | `1` | $14.20 | 21m31s | `1003` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT43 | `git_diff_driver` | struct | `9` | `9` | `2` | `6` | `1` | $9.74 | 18m13s | `297` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT44 | `git_hash_sha1_ctx` | struct | `1` | `1` | `—` | `1` | `2` | $5.34 | 10m57s | `409` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT45 | `git_index_entrymap` | struct | `8` | `8` | `3` | `8` | `1` | $9.77 | 18m57s | `958` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT46 | `git_mwindow_file` | struct | `4` | `4` | `1` | `4` | `1` | $10.67 | 19m38s | `1313` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT47 | `git_pack_cache_entry` | struct | `3` | `3` | `—` | `3` | `1` | $8.95 | 17m15s | `1048` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT48 | `git_packbuilder_pobjectmap` | struct | `7` | `7` | `3` | `7` | `1` | $13.91 | 17m43s | `674` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT49 | `git_packbuilder_walk_objectmap` | struct | `7` | `7` | `3` | `7` | `1` | $9.90 | 15m05s | `760` | `wrap-2026-08-17_00-00-50_fd16` |
| `2` | WT50 | `git_refdb` | struct | `3` | `3` | `2` | `3` | `1` | $12.46 | 20m38s | `434` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT51 | `git_attr_cache_macromap` | struct | `7` | `7` | `3` | `7` | `1` | $8.87 | 14m20s | `726` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT52 | `git_diff_driver_map` | struct | `7` | `7` | `3` | `7` | `1` | $15.70 | 23m34s | `998` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT53 | `git_grafts_oidmap` | struct | `7` | `7` | `3` | `7` | `1` | $8.70 | 15m52s | `833` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT54 | `git_hash_ctx` | struct | `4` | `4` | `—` | `2` | `1` | $8.44 | 17m31s | `1000` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT55 | `git_index` | struct | `23` | `23` | `6` | `23` | `1` | $16.16 | 24m16s | `447` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT56 | `git_odb` | struct | `7` | `7` | `1` | `7` | `1` | $13.56 | 19m50s | `651` | `wrap-2026-08-17_00-00-50_fd16` |
| `3` | WT57 | `git_pack_offsetmap` | struct | `7` | `7` | `3` | `7` | `1` | $7.96 | 15m11s | `886` | `wrap-2026-08-17_00-00-50_fd16` |
| `4` | WT58 | `git_attr_cache` | struct | `6` | `6` | `2` | `6` | `1` | $13.43 | 21m00s | `605` | `wrap-2026-08-17_00-00-50_fd16` |
| `4` | WT59 | `git_diff_driver_registry` | struct | `1` | `1` | `—` | `1` | `1` | $7.29 | 13m10s | `570` | `wrap-2026-08-17_00-00-50_fd16` |
| `4` | WT60 | `git_grafts` | struct | `4` | `4` | `1` | `4` | `2` | $10.75 | 16m35s | `833` | `wrap-2026-08-17_00-00-50_fd16` |
| `4` | WT61 | `git_pack_cache` | struct | `5` | `5` | `—` | `5` | `1` | $8.91 | 16m07s | `1048` | `wrap-2026-08-17_00-00-50_fd16` |
| `5` | WT62 | `git_pack_file` | struct | `20` | `20` | `2` | `20` | `1` | $14.93 | 25m07s | `2710` | `wrap-2026-08-17_00-00-50_fd16` |
| `5` | WT63 | `git_repository` | struct | `29` | `28` | `17` | `25` | `1` | $22.97 | 35m13s | `3169` | `wrap-2026-08-17_00-00-50_fd16` |
| `6` | WT64 | `git_packbuilder` | struct | `29` | `29` | `6` | `29` | `1` | $13.59 | 24m24s | `917` | `wrap-2026-08-17_00-00-50_fd16` |
| `6` | WT65 | `git_packfile_stream` | struct | `5` | `5` | `2` | `5` | `1` | $10.63 | 20m42s | `1312` | `wrap-2026-08-17_00-00-50_fd16` |
| `7` | WT66 | `git_indexer` | struct | `30` | `30` | `4` | `30` | `1` | $16.40 | 25m21s | `447` | `wrap-2026-08-17_00-00-50_fd16` |
| **Σ `72`** | | | | **`401`** | **`399`** | **`107`** | **`389`** | **`85`** | **$`666.52`** | | **`61570`** | **`72`/`72`** |

### Cost — types

| units | fields | $ | wall | $/unit | $/field |
|---|---|---|---|---|---|
| **`72`** | **`401`** | **`$666.52`** | **`3h17m41s`** | **`$9.26`** | **`$1.66`** |

### Batches — by layer

| layer | units | loc | $ | wall | $/unit | $/loc |
|---|---|---|---|---|---|---|
| `0` | `21` | `14696` | `$143.06` | `21m16s` (`12.5`x) | `$6.81` | `$0.010` |
| `1` | `23` | `19843` | `$208.15` | `24m16s` (`15.3`x) | `$9.05` | `$0.010` |
| `2` | `12` | `9879` | `$116.97` | `21m31s` (`9.3`x) | `$9.75` | `$0.012` |
| `3` | `7` | `5541` | `$79.39` | `24m16s` (`5.4`x) | `$11.34` | `$0.014` |
| `4` | `4` | `3056` | `$40.38` | `21m00s` (`3.2`x) | `$10.09` | `$0.013` |
| `5` | `2` | `5879` | `$37.90` | `35m13s` (`1.7`x) | `$18.95` | `$0.006` |
| `6` | `2` | `2229` | `$24.22` | `24m24s` (`1.8`x) | `$12.11` | `$0.011` |
| `7` | `1` | `447` | `$16.40` | `25m21s` (`1.0`x) | `$16.40` | `$0.037` |
| **Σ** | **`72`** | **`61570`** | **`$666.47`** | **`3h17m41s`** (sum, = session wall) | **`$9.26`** | **`$0.011`** |

## Safety audit

`crustify-audit <repo> unsafe --json`, `global` section — tree-wide, not
per seed. Two snapshots: at the import-closure promotion and at the final tree.

### Headline — at the import closure (`be5f6d064`)

| unsafe loc | % of loc | blocks | % in `impl T` | naked raw ptrs | `&mut` | field proj |
|---|---|---|---|---|---|---|
| `1946` | `8.6`% | `1240` | `25`% | `180` | `3` | `482` |

### All metrics — at the import closure

| metric | value | reading |
|---|---|---|
| `code_lines` | `22726` | non-blank, non-comment source lines (denominator) |
| `total_stmts` | `1079` | statements |
| `unsafe_blocks` | `1240` | count of `unsafe { }` blocks |
| `unsafe_block_stmts` | `312` | statements inside them |
| `unsafe_block_code_lines` | `1946` | **8.6%** of `code_lines` |
| `unsafe_blocks_wrapper_impl` | `316` | 25% — inside `impl <wrapper T>` |
| `unsafe_blocks_ffi_export` | `0` | inside the C-ABI gateway |
| `wrapper_impl_macro` | `242` | macro-generated accessors |
| `wrapper_impl_handwritten` | `74` | hand-written ones |
| `rp_args` | `252` | raw-ptr positions in arguments |
| `rp_rets` | `314` | raw-ptr positions in returns |
| **total positions** | `566` | args + rets; disjoint, so this is the surface |
| `rp_seam` | `386` | 68% sanctioned: seam fn / `mod ffi_export` / `extern "C"` / ptr-to-own-`Self` |
| **smell (total − seam)** | `180` | 32% — the reportable remainder |
| `rp_wrapped` | `5` | **of the smell**: pointee is a C type that HAS a wrapper — the actionable defect |
| `rp_in_wrapper` | `1` | **of the smell**: inside a wrapper impl — the least excusable placement |
| `ref_to_type_wrapper` | `3` | `&W`/`&mut W` on an inline-`CType` wrapper — **target 0** |
| `field_proj_wrapped` | `482` | projection VOLUME — shares one HIR shape with `addr_of!`, not a violation |
| `field_proj_outside_impl` | `0` | projections outside any accessor — **target 0** |
| `field_ref_wrapped` | `0` | `&(*p).field` — forbidden by the translator playbook — **target 0** |
| `raw_ptr_derefs` | `490` | `*p` on a raw pointer (volume) |
| `void_ptr_sanctioned` | `85` | `*c_void` in a seam / `ffi_export` / `extern "C"` signature |
| `void_ptr_smell` | `154` | `*c_void` elsewhere — dominated by the `from_void_ptr`/`as_mut_void_ptr` pair |

### Headline — final, after the god objects (`33d19674f`)

| unsafe loc | % of loc | blocks | % in `impl T` | naked raw ptrs | `&mut` | field proj |
|---|---|---|---|---|---|---|
| `4238` | `8.4`% | `2606` | `22`% | `313` | `3` | `1270` |

### All metrics — final

| metric | value | reading |
|---|---|---|
| `code_lines` | `50754` | non-blank, non-comment source lines (denominator) |
| `total_stmts` | `2994` | statements |
| `unsafe_blocks` | `2606` | count of `unsafe { }` blocks |
| `unsafe_block_stmts` | `612` | statements inside them |
| `unsafe_block_code_lines` | `4238` | **8.4%** of `code_lines` |
| `unsafe_blocks_wrapper_impl` | `564` | 22% — inside `impl <wrapper T>` |
| `unsafe_blocks_ffi_export` | `0` | inside the C-ABI gateway |
| `wrapper_impl_macro` | `390` | macro-generated accessors |
| `wrapper_impl_handwritten` | `174` | hand-written ones |
| `rp_args` | `425` | raw-ptr positions in arguments |
| `rp_rets` | `529` | raw-ptr positions in returns |
| **total positions** | `954` | args + rets; disjoint, so this is the surface |
| `rp_seam` | `641` | 67% sanctioned: seam fn / `mod ffi_export` / `extern "C"` / ptr-to-own-`Self` |
| **smell (total − seam)** | `313` | 33% — the reportable remainder |
| `rp_wrapped` | `186` | **of the smell**: pointee is a C type that HAS a wrapper — the actionable defect |
| `rp_in_wrapper` | `5` | **of the smell**: inside a wrapper impl — the least excusable placement |
| `ref_to_type_wrapper` | `3` | `&W`/`&mut W` on an inline-`CType` wrapper — **target 0** |
| `field_proj_wrapped` | `1270` | projection VOLUME — shares one HIR shape with `addr_of!`, not a violation |
| `field_proj_outside_impl` | `0` | projections outside any accessor — **target 0** |
| `field_ref_wrapped` | `0` | `&(*p).field` — forbidden by the translator playbook — **target 0** |
| `raw_ptr_derefs` | `1278` | `*p` on a raw pointer (volume) |
| `void_ptr_sanctioned` | `136` | `*c_void` in a seam / `ffi_export` / `extern "C"` signature |
| `void_ptr_smell` | `261` | `*c_void` elsewhere — dominated by the `from_void_ptr`/`as_mut_void_ptr` pair |

### What the god objects moved

| metric | post-W2 | post-W3 | |
|---|---|---|---|
| `code_lines` | `22726` | `50754` | tree more than doubled |
| unsafe % | `8.6`% | `8.4`% | density FELL |
| total rp positions | `566` | `954` | |
| `rp_seam` | `386` | `641` | |
| smell | `180` | `313` | |
| **`rp_wrapped`** | **`5`** | **`186`** | **the campaign's largest single audit movement** |
| `rp_in_wrapper` | `1` | `5` | |
| `field_ref_wrapped` | `0` | `0` | held |
| `ref_to_type_wrapper` | `3` | `3` | held — no new cases |

## Notes

The only prose outside the setup and legend above: pitfalls, findings, and the
context each table cannot carry.

### `struct stat` — gate-missed, not skipped

The oracle lists it in the import section (`query types --imported-only` returns
it; `bits/struct_stat.h` is in the import file set) but `translate` refuses it
— *"out of scope (neither target- nor import-section)"* — while the same
dry-run labels it `(target)` when reached as a dependency of the same-named
`stat` FUNCTION. Three verdicts that cannot all be true; it looks like a
name-collision edge case, and it is unchanged on `ref`. 78 of 79 units ran.

### The wide symbol wave, superseded

A first wave 2 took every import-section symbol whose OWN DAG layer was 0–2 (255
selected → 231 units → 6 batches, `$167.80`, 230 distinct items). That is a
strict superset — it covered this closure entirely and 163 symbols beyond it —
but at 3.5x the cost for work the brief did not ask for, and it MISSED `write`,
a direct dependency of port code at layers 0–2 that sits at DAG layer 8. It was
un-promoted and is kept intact on
`crustify/session/wrap-2026-08-16_16-43-18_0c15` with its 6 agent branches.

### From the import closure

**The audit's headline numbers are not what they look like.** `field proj` 482
and `naked raw ptrs` 557 read as mass violations of the safety discipline; they
are not. `field_proj_wrapped` and `raw_ptr_derefs` are exactly equal in every
scope tested (482/482 tree-wide, 381/381 for the libgit2 crate), because both
count the same construct — `addr_of!((*ptr).field)`, which the principles
MANDATE as the read form. The metrics that actually flag a breach are the
`outside`/`nonseam` twins, and they are `field_proj_outside_impl` 0,
`unsafe_blocks_ffi_export` 0, `rp_wrap_nonseam_args` 0, `rp_wrap_nonseam_rets` 1.
Read the raw counters as violations and you would reject a clean tree.

**`ref_to_wrapper` = 3 is a rule/rationale mismatch, not an indexing bug.**
All three are attributed to `NtlmBufBufBorrowed` (`ntlm_buf`) in
`libgit2/src/ntlmclient/ntlm.rs`, which carries `unsafe impl<'a> CCell for
NtlmBufBufBorrowed<'a> { type C = ffi::ntlm_buf; … }` — so it IS in the wrapper
set by the metric's own definition, and the detection is correct. Notably it is
HAND-WRITTEN rather than a `define_ctype!` triple (the borrow needs a lifetime
on the value, which the macro cannot express), and the resolution-aware pass
found it anyway, because it keys on `impl CCell|CLayout` + `type C` rather than
on the macro spelling.

What does not hold here is the RATIONALE. The rule exists because `&W`/`&mut W`
assert `noalias` / `readonly` / validity over memory C may write through a
pointer it retains. This slot is Rust-owned stack storage built by `new()` and
never handed to C — no C routine takes an `ntlm_buf *` at all — so nothing
outside Rust holds a pointer to it. It is shaped exactly like
`ffibox::CVal<T>`, and the agent verified it under Miri (Stacked
Borrows) and documented the argument in the type's rustdoc.

The metric is deliberately syntactic ("decidable by syntax rather than by a
per-type judgement"), so it cannot see that distinction. Two fixes, either of
which keeps the target at 0: split the counter by PROVENANCE (C-owned
pointer-holding wrappers vs Rust-constructed inline slots — prim already draws
this line with `CVal`), or give the exemption a machine-readable marker such as
`// AUDIT-OK(ref_to_wrapper): Rust-owned slot, never handed to C`, mirroring
the `// SAFETY:` convention. As it stands the justification lives in prose no
tool reads, so this finding will recur every wave.

Separately, the TEXTUAL pass (`audit_manifest.py`) is both over- and
under-inclusive and says so itself: its `wrapper_ref` counts `&self`/`&mut
self` anywhere in an entity's span without resolving what `Self` is — so a
trait DEFINITION like `NtlmBufCursorMut` (3 `&mut self`, implemented only by
the handles) leaks in — while it discovers wrappers only through
`define_*ctype!` / `CType<ffi::X>`, missing hand-written ones entirely. 70 of
314 wrapper impls here are hand-written.

**A systemic pattern worth a decision, not a bug.** `void_ptr_smell` = 154 is
not 154 independent mistakes: it is a `from_void_ptr` / `as_mut_void_ptr` pair
emitted on ~77 types. Either the pair is sanctioned and the audit should
classify it as such, or it should not be emitted by default.

**Naked-ffi surface is small and localised**: 9 sites over 8 entities, worst
`ReftableTable` at 2.

**`struct stat` is gate-missed, not skipped.** The oracle lists it as
import-section (`query types --imported-only` returns it; `bits/struct_stat.h` is in
the wrap file set) but `translate` refuses it — *"out of scope (neither wrap-
nor target-section)"* — while the same dry-run labels it `(port)` when it is
reached as a dependency of the same-named `stat` FUNCTION. Three verdicts that
cannot all be true; it looks like a name-collision edge case. Unchanged on
`ref`. 78 of 79 units ran; this is the one that did not.

**Tooling defects hit during the campaign**, both still present on `ref`:

1. `crustify-log-cost` reports `$0.00` for any translate-stage wave. Its
   `kind()` returns the translate-era buckets (`wrap-type`, `wrap-symbol`,
   `raw-symbol`) but the summary loop iterates a hardcoded list of the
   PRE-translate names, so every row is counted and then never printed.
   Worked around by `crustify/utils/wave_cost.py`, which imports log_cost's own
   `parse_usage`/`wall_seconds` rather than repricing. `kind()` also has no
   `raw-*` case, so the lifetime runs bucket as `other`.
2. On the FIRST wave in a fresh tree, `crustify/targets/<t>/logs/` does not yet
   exist in the main checkout, so `worktree._link_into` has nothing to symlink;
   the agent creates it INSIDE its worktree and the purge destroys it, taking
   the agent's `.log` and `usage.json` with it. This is the failure the
   `.providers` docstring warns about, but `logs/` is not in `_SHARED_LAZY_DIRS`.
   It cost the first void run's entire trace; the cost was recoverable only
   because the claude CLI keeps its transcript in `~/.claude/projects`.
   Pre-creating the shared dir fixes it.

**An agent-reported hazard worth acting on**: agents share `/tmp`, and one
agent's `--update` payload at `/tmp/free.json` was overwritten by another
agent's findings before submission. The oracle's arg-name invariant rejected
it, so nothing corrupt landed — but that was luck. Findings files should be
written to a per-agent path.

**Verification is gated on an artifact that must exist.** The `ref` agents link
`build-static/libgit2.a` when present and gate the C-calling tests behind
`cfg(crustify_c_linked)`. Absent the archive those tests silently compile out,
so a green `cargo test` proves nothing about any release strategy. Every figure
here was taken with the campaign's recorded static-archive build:
**778 tests pass, 106 of them `linked::` round-trips
executing against real libgit2 C**.

**Linkability is a campaign-wide constraint.** `libgit2.so` exports 902
symbols, all the public `git_*` API; every internal symbol (`git__*`,
`reftable_*`, `xdl_*`, `ntlm_*`) is hidden. Nothing in the wrap surface is
callable by linking the shared library — which is why the static archive route
exists and why `libgit2-sys` carries no link directive of its own.


---

### From the god objects

**`field proj` 1270 is volume, not violations** — see the import-closure copy's
note. `field_proj_wrapped` counts `addr_of!((*p).f)` and a bare `(*p).f`
identically, because `addr_of!` takes a place expression and both lower to one
HIR shape. The rule the current translator playbook states — never form
`&(*p).field` — is `field_ref_wrapped`, and it is **0** across the tree.

**The one real regression: `rp_wrap_nonseam_args` 0 → 4.** Raw pointers in
wrapper signatures away from the FFI seam, all four introduced by this wave and
each carrying a written justification in the code:

- `util/pool.rs: contains(&self, ptr: *const c_void)` — an address-range
  predicate over type-erased `git_pool_malloc` results; a wrapper would be
  meaningless, since "an arbitrary foreign address is exactly what the C
  predicate answers for".
- `util/pool.rs: unlink_next(&mut self) -> *mut git_pool_page` and
  `mwindow.rs: unlink_next(...)` — the read-then-NULL pair, deliberately NOT a
  `CBox` because that "would make `c_drop` recurse once per page".
- `indexer.rs: from_void_ptr(ptr: *mut c_void)` — the callback-payload retag.

They read as defensible rather than sloppy, but they are a real move off 0 and
the same class of problem as `ref_to_type_wrapper`: a deliberately syntactic
rule meeting a justified exception with no machine-readable way to declare it.
An `// AUDIT-OK(<metric>): <reason>` marker, checked the way `// SAFETY:` is,
would let the target stay a meaningful 0.

**`ref_to_type_wrapper` stayed at 3** — still only the carried
`NtlmBufBufBorrowed` case from wave 1. This wave, 72 units and 61,570 lines,
added none.

**Cost per layer climbs as the DAG deepens**: `$6.81`/unit at L0 against
`$16.40` at L7, because each layer's agents read and reconcile against
everything the layers below emitted. The tail is also where parallelism dies —
L0 ran 21 units at 12.5x serial-sum compression, L7 ran one agent at 1.0x. Two
thirds of the wave's wall clock bought the last 12 units.

**Two units are labelled `callback` but are macro-generated structs**
(`GIT_HASHMAP_STRUCT`, `git_array_t`): the oracle holds no type record for a
macro-generated type, so the kind column falls back. Their `newtypes` and
`wrapped` columns are still derived from the landed anchors and are correct.

**Verification** (with `build-static/libgit2.a` built, so the gated
`linked::` tests actually execute rather than compiling out): `scaffold
--validate` clean; **1688 tests pass, 400 `linked::` round-trips** against
libgit2's own C; `cargo clippy --workspace --all-targets` clean under
`undocumented_unsafe_blocks = "deny"`.

### The lifetime traits beyond the lifetime stages

Tree-wide after all waves the four traits stand at `CDropped` 40, `CCloned` 15,
`CLenDropped` 10, `CLenCloned` 0 — so the waves added 34 / 11 / 3 beyond what
the lifetime stages emitted, and no `CLenCloned` was ever emitted, meaning no
`CVec` in the tree is cloneable. That is a property of the C: cloning a counted
buffer needs a C routine that duplicates one, and libgit2 exposes none.
The three later `CLenDropped` strategies came from ordinary waves rather than a
lifetime tier — `Munmap`, `GitMapUnmap` and `LogRecordArrayFree` — so the
lifetime stages are not a complete inventory of release strategies.

### The raw-pointer metric family

The audit's raw-pointer counters were rebuilt during this campaign. The earlier
pair split the surface into two regions and counted the sanctioned region in
neither, publishing a numerator with no denominator: `941` read as "the whole
surface is suspect" when two thirds of it was legitimate by construction. One
family now — `rp_args` + `rp_rets` is every position, `rp_seam` the sanctioned
subset, and `rp_wrapped` / `rp_in_wrapper` are subsets of the remainder. The
`rp_wrapped` movement from `5` to `186` across the god objects is the campaign's
largest single audit finding and was unreportable before.
