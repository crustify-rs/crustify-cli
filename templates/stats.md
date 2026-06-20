# crustify libgit2 port — progress

Port scope: `<scope>` (N files / N fn / N ty). Waves done: **—**.

## Scope breakdown

| item      | port | wrap | total |
|---|--:|--:|--:|
| files     |  |  |  |
| functions |  |  |  |
| globals   |  |  |  |
| macros    |  |  |  |
| types     |  |  |  |
| LoC       |  |  |  |

## Wave plan

One wave per dag layer: `Wx` = `--dag-layer x`. Macros are bindgen-owned and
excluded. Counts from `--dry-run` (scheduler-resolved). One row per wave; mark
`done` with ✓.

| wave | wrap | port | total | done |
|---|--:|--:|--:|:-:|
| W0  |  |  |  |   |
| …   |  |  |  |   |
| **Σ** |  |  |  |   |

## Items per wave

One column per completed wave.

| item | … | total |
|---|--:|--:|
| type wrappers (struct/union/enum) |  |  |
| callbacks wrapped |  |  |
| functions wrapped (safe FFI views) |  |  |
| functions ported (native Rust) |  |  |
| globals ported |  |  |
| lifecycle ops folded into types |  |  |
| C LoC translated (fn body spans) |  |  |

macros are bindgen-owned — not wave-wrapped.

## Cost & time per wave

Measured from per-wave commit-time windows.

| wave | wrap $ | port $ | total $ | wall-clock |
|---|--:|--:|--:|--:|
| W0 |  |  |  |  |
| … |  |  |  |  |
| **total** |  |  |  | |

One-time pre-wave analyze + scaffold + bindgen cost is tracked separately (not
per-wave).

## Cumulative output

| metric | value |
|---|--:|
| C files translated |  |
| C LoC translated (fn body spans) |  |
| Rust files |  |
| Rust LoC — code |  |
| Rust LoC — doc/comment |  |
| field accessors |  |
| field setters |  |
