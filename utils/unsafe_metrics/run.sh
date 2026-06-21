#!/usr/bin/env bash
# Usage: ./run.sh <file.rs> [extra rustc args]
set -e
NS=$(rustc +nightly --print sysroot)
cargo +nightly build -q
LD_LIBRARY_PATH=$NS/lib SYSROOT=$NS \
  ./target/debug/unsafe_metrics "$1" --edition 2021 --crate-type lib "${@:2}"
