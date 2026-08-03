
## Install dependencies

### crustify

- python
- agent backends
    - claude: `curl -fsSL https://claude.ai/install.sh | bash`
    - codex: `curl -fsSL https://chatgpt.com/codex/install.sh | sh`

### rust toolchain

- bindgen, clippy, cargo, etc.
- nightly

### CodeQL

- CodeQL toolchain
- if MacOS arm64: rosetta

## `<target>/crustify` initial scaffolding

- scaffold the dir
- hand-author `crustify/config.json` for listing where crustify agents can find deps
- copy `templates/gitignore` in `crustify/.gitignore`
- create and checkout branch `crustify/<target>`

## Build target

- hand-author `build.json`
    - prefer multi-threaded build commands (prefer using performance cores only, 
    leave efficiency cores out)
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

Run the oracle to generate `scope.json`.

### types.json and syms.json

Run the oracle to emit the on-disk analysis schemas `types.json`/`syms.json` for all code items.

### crates.json

Home each item in its suitable crate and module using the guidelines in `docs/crates.md`.
Leverage `crustify query` to obtain wrap- and port-closures, and `build.json` for artefact hierarchy.
Once homed, run the scaffolder to emit the rust tree.
Gate with `scaffold --validate`.

### bindgen

Run the bindgen stage and complete the emitted sources to build bindgen and generate the
bindings for the wrap-closure items.
Diff the allowlists against `bindings.rs` to assess completenes and fix missing items.
Write thin unit tests to prove linking `-sys` crates pass `cargo build` and `cargo test`.

### Initial commit

Commit the scaffolded `rust/` tree on `crustify/<target>`.