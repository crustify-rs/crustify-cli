# libgit2 and OpenSSL campaign results

Historical results for Crustify campaigns over
[libgit2](https://github.com/crustify-rs/crustify-libgit2) (`src/`) and
[OpenSSL](https://github.com/crustify-rs/crustify-openssl) (`ssl/`). Both
targets exceed 100K lines of C. The campaigns used `claude-opus-5`.

## Wrap the FFI closure

The first experiment wrapped the types and symbols needed before target code
could be ported.

### libgit2

Types:

| layer | types | cost | cost/type | cost/line | wall | lines |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 67 | $453.23 | $6.76 | $0.016 | 1h12m | 29,060 |
| 1 | 8 | $61.61 | $7.70 | $0.009 | 29m11s | 7,037 |
| 2 | 1 | $8.95 | $8.95 | $0.007 | 19m22s | 1,365 |
| **Total** | **76** | **$523.79** | **$6.89** | **$0.014** | **2h01m** | **37,462** |

Symbols—the wrap closure of port layers 0–2:

| layer | symbols | cost | cost/symbol | cost/line | wall | lines |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 23 | $15.46 | $0.67 | $0.006 | 29m45s | 2,613 |
| 1 | 32 | $41.30 | $1.29 | $0.010 | 53m05s | 4,053 |
| **Total** | **55** | **$56.76** | **$1.03** | **$0.009** | **1h23m** | **6,666** |

The run completed 55 of the closure's 97 symbols. Another eight lifecycle
primitives landed with their owning types and are included in the type cost.

### OpenSSL

Types:

| layer | types | cost | cost/type | cost/line | wall | lines |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 40 | $283.62 | $7.09 | $0.017 | 26m42s | 16,634 |
| 1 | 6 | $50.82 | $8.47 | $0.017 | 24m41s | 3,049 |
| **Total** | **46** | **$341.80** | **$7.43** | **$0.017** | **1h04m** | **20,354** |

Symbols—the wrap closure of port layers 0–2:

| layer | symbols | cost | cost/symbol | cost/line | wall | lines |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 28 | $30.02 | $1.07 | $0.008 | 33m51s | 3,793 |
| 1 | 32 | $30.60 | $0.96 | $0.009 | 49m44s | 3,566 |
| **Total** | **60** | **$60.62** | **$1.01** | **$0.008** | **1h23m** | **7,359** |

## Transitively wrap god objects

The second experiment selected three target types with at least 25 declared
fields and wrapped their complete dependency closures, bottom layer first.

| | libgit2 `src/` | OpenSSL `ssl/` |
|---|---:|---:|
| seeds | `git_indexer`, `git_packbuilder`, `git_repository` | `record_layer_st`, `quic_stream_st`, `ssl_session_st` |
| seed fields | 30, 29, 29 | 25, 35, 41 |
| units including seeds | 75 | 65 |
| port / wrap | 66 / 9 | 39 / 26 |
| new units in this experiment | 68 | 47 |
| depth | 8 layers | 12 layers |
| share of port scope | 10.2% of 646 types | 9.0% of 399 types |

The tables below exclude units already priced in the first experiment.

### libgit2

| layer | types | cost | cost/type | cost/line | wall | lines |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 18 | $156.49 | $8.69 | $0.011 | 28m47s | 13,971 |
| 1 | 22 | $265.39 | $12.06 | $0.010 | 42m44s | 25,918 |
| 2 | 12 | $150.21 | $12.52 | $0.013 | 38m31s | 11,585 |
| 3 | 7 | $91.31 | $13.04 | $0.009 | 31m44s | 10,065 |
| 4 | 4 | $54.23 | $13.56 | $0.013 | 33m11s | 4,139 |
| 5 | 2 | $40.27 | $20.14 | $0.006 | 38m06s | 6,286 |
| 6 | 2 | $34.24 | $17.12 | $0.009 | 40m53s | 3,835 |
| 7 | 1 | $24.08 | $24.08 | $0.008 | 38m28s | 3,167 |
| **Total** | **68** | **$816.22** | **$12.00** | **$0.010** | **4h52m** | **78,966** |

### OpenSSL

| layer | types | cost | cost/type | cost/line | wall | lines |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 17 | $159.81 | $9.40 | $0.011 | 42m44s | 14,908 |
| 1 | 11 | $108.56 | $9.87 | $0.009 | 33m37s | 12,545 |
| 2 | 6 | $86.96 | $14.49 | $0.007 | 49m08s | 12,495 |
| 3 | 4 | $41.50 | $10.38 | $0.008 | 30m08s | 5,308 |
| 4 | 2 | $26.65 | $13.32 | $0.007 | 37m38s | 3,835 |
| 5 | 1 | $11.51 | $11.51 | $0.006 | 24m25s | 1,860 |
| 6 | 1 | $13.71 | $13.71 | $0.005 | 28m40s | 2,709 |
| 7 | 1 | $12.34 | $12.34 | $0.008 | 27m33s | 1,498 |
| 8 | 1 | $9.98 | $9.98 | $0.006 | 24m15s | 1,740 |
| 9 | 1 | $10.04 | $10.04 | $0.007 | 23m13s | 1,509 |
| 10 | 1 | $8.52 | $8.52 | $0.007 | 20m35s | 1,252 |
| 11 | 1 | $13.90 | $13.90 | $0.006 | 26m50s | 2,337 |
| **Total** | **47** | **$503.48** | **$10.71** | **$0.008** | **6h08m** | **61,996** |

The two unrelated codebases priced generated wrappers at $0.008–$0.010 per
line in this experiment.

## Unsafe surface

The deterministic safety scan after the second experiment reported:

| target | unsafe lines | tree share | unsafe blocks | inside `impl T` |
|---|---:|---:|---:|---:|
| libgit2 `src/` | 2,680 | 5.55% of 48,309 | 1,695 | 1,314 (77.5%) |
| OpenSSL `ssl/` | 2,531 | 7.39% of 34,235 | 1,859 | 1,739 (93.5%) |

An `impl T` reaches wrapped state through the type's accessors, so this is the
portion of the unsafe surface confined to the intended boundary.

## Tests

| target | `#[test]` functions | test lines | files with tests |
|---|---:|---:|---:|
| libgit2 `src/` | 1,917 | 32,961 | 95 |
| OpenSSL `ssl/` | 1,368 | 22,446 | 84 |

Every unit landed only after `cargo check`, `cargo clippy`, and
`cargo test --workspace` passed over the whole tree.
