
## Install dependencies

### crustify

- python
- agent backends: claude and codex

### rust toolchain

- bindgen, clippy, cargo, etc.
- nightly

### CodeQL

- codeql toolchain
- if macos arm64: rosetta

## `<target>/crustify` initial scaffolding

- scaffold the dir
- hand-author `crustify/config.json` for listing where crustify agents can find deps
- copy `templates/gitignore` in `crustify/.gitignore`
- create and checkout branch `crustify/<target>`

## Build target

- hand-author `build.json`
    - prefer multi-threaded build commands (use performance cores, leave efficiency cores out)
    - prefer a configure command that disables deprecated features
    - enable sanitizers

- build the target

- run tests to collect baseline and disable failing tests

- generate the CodeQL database

- emit the T1/T2 tables with `analyze extract-ql`

## Setting up scope targets

When the user asks you to setup a `crustify/targets/<target>/config.json` for a `<target>`
identify any headers that live outside of the `<target>` tree that may export structs, enums, or unions
that are implemented by the code in the `<target>` tree and add them in `config.json.port_files`.
Distinguish implementors from consumers / referencers and only add a header in port-scope if its
implementors are port-scope. Prefer using directory paths in `config.json` when all files are port-scope
instead of listing every file.

## Generate the analysis oracle

### scope.json
Run the oracle to generate `scope.json`

### types.json and syms.json

Run the deterministic-half of the oracle to generate the on-disk `types.json`/`syms.json`
analysis tree for all code items.

### crates.json

Home each item in its suitable crate and module using the guidelines in `docs/crates.json`.
Once homed, run the scaffolder to emit the rust tree.

### bindgen

Run the bindgen stage and complete the emitted sources to build bindgen and generate the
bindings for the wrap-closure items.