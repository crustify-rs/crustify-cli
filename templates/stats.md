# crustify libgit2 port — progress

Port scope: whole `src/libgit2` (221 files / 3588 fn / 613 ty). Waves done: **L0–L2**.

## Items per wave

| item | L0 | L1 | L2 | total |
|---|--:|--:|--:|--:|
| type wrappers (struct/union/enum) | 35 | 24 | 19 | 78 |
| callbacks wrapped | 7 | 1 | 3 | 11 |
| functions wrapped (safe FFI views) | 50 | 61 | 17 | 128 |
| functions ported (native Rust) | 1 | 20 | 37 | 58 |
| globals ported | 21 | 0 | 0 | 21 |
| lifecycle ops folded into types | 4 | 15 | 17 | 36 |
| C LoC translated (fn body spans) | 9 | 178 | 577 | 764 |

macros (233) are bindgen-owned — not wave-wrapped.

## Cost & time per wave

Measured from per-wave commit-time windows.

| wave | wrap $ | port $ | total $ | wall-clock |
|---|--:|--:|--:|--:|
| L0 | 96 | 36 | 132 | ~3.5h |
| L1 | 41 | 26 | 67 | ~2h |
| L2 | 38 | 65 | 103 | 2h15m |
| **total** | **175** | **127** | **302** | |

L0 wrap may carry ~$43 more June-18 spillover. One-time pre-wave analyze + scaffold + bindgen ≈ $140 (not per-wave).

## Cumulative output

| metric | value |
|---|--:|
| C files translated | 100 (41 `.c` + 59 `.h`) |
| C LoC translated (fn body spans) | 764 |
| Rust files | 109 (100 modules + 9 `mod`/`lib`) |
| Rust LoC — code | 6,965 |
| Rust LoC — doc/comment | 12,407 |
| field accessors | ~300 methods (425 projections: 263 read / 162 write) |
| field setters | 147 |
