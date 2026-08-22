# Playbook

Driving crustify, in two phases. Setup: toolchain install through the first
commit of the initial Rust tree — authoring `build.json`, `cli-config.json` and
a target's `scope-config.json`, building the CodeQL database, extracting the
T1/T2 tables, and seeding crate shells. Translation: preparing, running,
landing and scanning waves with `crustify-audit`. Read Setup
before any wave; every later stage reads what it produces.

Paths below are relative to the crustify checkout (`deps.crustify` in
`cli-config.json`). Run any command's `--help` for exact flags — argparse is the
source of truth.

## The artifact tiers

Two tiers, and the distinction decides where a file goes.

| tier | path | authored | derived |
|---|---|---|---|
| repo | `<repo>/crustify/` | `build.json`, `crates.json`, `cli-config.json` | `codeql/{db,t1,t2}/`, `rust/` |
| target | `<repo>/crustify/targets/<target>/` | `scope-config.json` | `scope.json`, `deps-dag.json`, `logs/<session>/` |

Repo-tier describes the whole repository and is target-agnostic. Target-tier is
scoped to one `crustify-cli <repo_root> <target> …` invocation. A repo can carry
several targets over one repo-tier set.

Every authored file has a commented example under `specs/`. Read the
template before authoring — the `_comment_*` keys carry the field semantics and
are the only complete spec.

## Phase 1 — Setup

From an untouched checkout to the first commit of the initial Rust tree.

### 1. Toolchains and checkouts

| need | install |
|---|---|
| Python ≥ 3.13 | system or `uv` |
| Claude Code CLI | `curl -fsSL https://claude.ai/install.sh \| bash` |
| OpenAI Codex CLI | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` |
| Rust | rustup: `cargo`, `clippy`; nightly with `rustc-dev` and `llvm-tools` |
| `bindgen-cli` | `cargo install bindgen-cli` |
| CodeQL | the CodeQL CLI bundle, on `PATH` |
| `ffibox` | `git clone https://github.com/crustify-rs/ffibox.git` |
| `crustify-audit` | `git clone git@github.com:crustify-rs/crustify-audit.git` beside `ffibox`; `python -m pip install -e <checkout>` |

On macOS arm64 the CodeQL bundle needs Rosetta.

### 2. Bootstrap `crustify/`

```bash
mkdir -p <repo>/crustify
cp specs/gitignore <repo>/crustify/.gitignore
```

Author `<repo>/crustify/cli-config.json` from `specs/cli-config.json`:

| block | holds |
|---|---|
| `deps` | absolute path to the crustify checkout and to `ffibox`|
| `bins` | absolute path to `crustify-cli` and `crustify-oracle` |

**Absolute paths only.** An agent runs inside a git worktree, so nothing
relative to a cwd resolves the same way twice. The file is machine-local and
gitignored — it reaches a worktree through `worktree.link_shared`, not git.

### 3. `build.json`

Author from `specs/build.json`. Downstream stages treat it as authoritative
for library partitioning, link topology and build invocation, so it must be
accurate before anything else runs. All paths repo-root-relative.

**The naming rule.** Each `libraries` key MUST be the stem of its `target` —
the filename without path or extension. `libssl.so` → `libssl`;
`providers/fips.so` → `fips`. System libraries (no `target`, `kind: system`)
take their conventional short name: `libc`, `libm`. This is what later
link-time attribution matches against, and `crates.json` keys off it.

`include_dirs` MAY overlap between libraries — a header's owning crate is
resolved at placement time, not here. `link_dependencies` entries must each name
a library defined in the same file.

**`build_commands`** are shell strings run from the repo root, by hand,
exposing  four generic stages: `configure`, `build`, `test` and `clean`.

- Prefer a `configure` that disables deprecated features.
- Enable sanitizers, so agents catch memory-safety violations when testing their
  Rust against the C.
- Prefer parallel `build` commands; on a hybrid-core host, distribute over
  performance cores.

### 4. Build and baseline

Run `configure`, then `build`. Then run `test` to collect the port-equivalence
baseline, disabling any test that fails on the unported tree.

Record the result in `build.json`'s `test_baseline`: pass/total, plus the name of
every test disabled to reach that state. A post-port run must match it. This is
the only evidence that a translation preserved behaviour, and it cannot be
reconstructed later.

### 5. CodeQL database and the T1/T2 tables

Build the CodeQL database manuallyt, and run `crustify-oracle extract-ql`
to emit the the T1 (entities) and T2 (edges) against that database.

It writes one CSV per query under `crustify/codeql/{t1,t2}/` — T1 entities, T2
edges. Every type/symbol record, the scope sets and the dependency DAG derive
from these on demand, which is why this is the one oracle command with side
effects and the only one that must be run explicitly. It takes minutes; re-run
it only after the C tree or the database changes.

### 6. Scope a target

Author `crustify/targets/<target>/scope-config.json` from
`specs/scope-config.json`.

It names **two file sets** and **one verb**. Entries in either set are a file
(`include/internal/statem.h`) or a directory with a trailing slash (`ssl/`),
which expands to every source and header beneath it. Naming a file the build
never compiled is harmless — T1 anchoring drops uncompiled candidates.

| key | what it names |
|---|---|
| `impl_files` | the sources — and private headers — that **implement** the library |
| `api_headers` | the headers that **publish** its API |
| `campaign_objective` | `port` or `wrap`: what the campaign is aimed at |

Both file sets are authored on every campaign — they describe the *library*,
not the campaign. `campaign_objective` is the only thing that decides how they
are read, and it is required with no default.

**`port` — reimplement the target in Rust.** `impl_files` + `api_headers`
together seed the `targeted` section. Classification is *definition-anchored*:
an entity is targeted iff its **body** lives in a named file (or, having no
body, all its declarations do). Name the implementations **and** the headers
that define the types — headers outside the target tree are never discovered
automatically, and a header-only list drops every function it merely
*declares*, whose body sits in a `.c` you did not name. Put a header in
`api_headers` only if its **implementors** are in `impl_files`; one whose types
are merely *used* reaches the imported section on its own.

**`wrap` — expose the public API.** Scope is composed *identically*: the
library is still `targeted`, because a wrap campaign owns its library too — it
merely intends something different with it. What changes is the dependency
graph. `wrap` walks no bodies at all (every symbol contributes its signature,
exactly as an imported symbol does under `port`), and only a struct **defined**
in `api_headers` keeps its field layout. Everything else orders as an opaque
handle, which is the right shape for wrapping.

Point `api_headers` at published headers (`include/openssl/`,
`include/libxml/`), never at a source tree.

**Three sets, two axes.** `targeted` / `imported` split on **ownership**;
`api` cuts **publication** across both, and is what a wrap campaign schedules:

| set | anchor | what it answers |
|---|---|---|
| `--targeted-only` | definition | the library this campaign owns |
| `--imported-only` | derived closure | its external dependencies |
| `--api-only` | **declaration** | what the headers publish |

They intersect rather than exclude, so `--api-only --imported-only` is the
re-export set. Layout still follows the definition site:

| | struct **defined** in a named file | only **declared** there |
|---|---|---|
| under `port` | full field layout | opaque handle |
| under `wrap` | full layout iff defined in `api_headers` | opaque handle |

So `--transitive` over an opaque-exported type pulls the type and nothing else.

**Three different things are called an objective; keep them apart.**
`campaign_objective` shapes the **dependency graph** — not the scope — and is
authored once per target.
`translate --objective` is the **verb** handed to one agent over one selection,
chosen per wave by the orchestrator. A target type in a port campaign might first
be wrapped and then ported, which is what the flag exists for. 

`out_of_scope.paths` refines what a directory entry expands to;
`out_of_scope.features` is documentation only, for the same reason as
`build.json`'s `features`.

Verify the result before proceeding:

```bash
crustify-oracle <repo_root> <target> query files --targeted-only
crustify-oracle <repo_root> <target> query files --imported-only
```

After the user picked a target, create the campaign's base branch and
logs dir:

```bash
git -C <repo> checkout -b crustify/<target>-<model>
mkdir -p <repo>/crustify/targets/<target>/logs
```

### 7. Seed crate shells

Author `crustify/crates.json`, the placement oracle. Schema:
`docs/schemas/crates.md`; example: `specs/crates.json`.

Seed the campaign's target crate and the top-level crates that own its imported
dependencies. Leave `modules` empty and do not home items yet. Crate names
match `build.json`'s link-unit keys; `depends_on` comes from
`link_dependencies` and is acyclic.

The orchestrator creates minimal compiling wrapper crates. Each starts with a
`Cargo.toml` and empty crate root. Do not create campaign modules yet.

Use Rust edition 2024. Wrapper crates inherit workspace lints that deny
`clippy::undocumented_unsafe_blocks` and allow `clippy::module_inception`.
Do not apply those lints to generated `<lib>-sys` code.

For each target or imported library crate, create its `<lib>-sys` placeholder:
`Cargo.toml`, `src/lib.rs`, `build.rs` and the bindgen input. Its bindgen
pipeline must compile with an empty, no-match agent-owned allowlist. Translator
agents populate that allowlist lazily.

Gate the shells:

```bash
crustify-cli <repo_root> <target> crates validate
cargo build
cargo test
```

### 8. Commit

Commit the initial `rust/` tree on `crustify/<target>-<model>`. Translate waves
branch from this baseline.

### Gates before the first wave

| check | how |
|---|---|
| baseline recorded | `build.json.test_baseline` names pass/total and every disabled test |
| T1/T2 populated | `crustify/codeql/{t1,t2}/` non-empty |
| scope is what you meant | `query files --targeted-only` / `--imported-only` |
| placement consistent | `crates validate` exits clean |
| FFI crates link | `cargo build` + `cargo test` on each `<lib>-sys` |
| DAG resolves | `query dag --layer 0` returns the leaf set |

---

## Phase 2 — Translation

A wave is one `crustify-cli … translate` invocation: the scheduler selects
units, batches them under budget, and spawns one agent per batch in its own git
worktree. Waves repeat bottom-up until the target is closed.

### Prepare each wave

Before `translate`, the orchestrator:

1. chooses the next target wave from the oracle;
2. runs wrap waves for any unsatisfied imported items in its dependency closure;
3. homes each selected wave's items in `crates.json`;
4. creates its `.rs` files and connects them to the crate root;
5. runs `crustify-cli <repo_root> <target> crates validate`;
6. confirms the affected crate shells compile.

Imported waves run lazily and recursively before the target wave that needs
them. They use the imported crate shells seeded during Setup.

The scheduler inserts TODO anchors. Translator agents extend their worklist's
bindgen allowlists and regenerate bindings in their worktrees.

Do not change `crates.json` during a wave. When landing parallel agents, union
their `<lib>-sys` allowlist changes and rerun the affected crate tests.

### Choosing the objective

A **wrap** campaign runs every wave at `--objective wrap`.

A **port** campaign runs `wrap` first: the type stays layout-compatible while C
still reads its fields. It runs `port` for an item once the C-side readers are
gone. `port` re-visits a filled anchor deliberately, so it is how an item is
escalated rather than redone, and it is what starts the opacification burn-down.

An item whose anchor is already filled is dropped from a `wrap` selection with a
warning. `--force` re-runs it anyway; `--skip NAME …` drops items silently.

### Raw lifetime discovery stage

Regardless of the target set, the first translation waves have to be the raw lifetime
discovery set, which will produce release/clone strategies for owned pointers that host
type-erased and NUL-terminated objects. First run `--lifetime-for void` and then
`--lifetime-for string`.

### Land and promote

After agents' changesets land in the session branch, check their logs to make sure the
C and Rust targets build and the tests pass. No need to check the C build/tests for
a wrap wave.

Run the deterministic scan over the merged wave, seeding the exact C type and
symbol names scheduled in it:

```bash
crustify-audit <repo_root> unsafe --name <wave names...> --json
```

Inspect each entry's source-site lists. A site is a lead, not a failure: fix a
wrapper bypass or unsound reference, and leave a necessary FFI seam in place
with its safety justification. The named pass also returns the tree-wide
`counts` block.

`crustify/audit/unsafe.json` is reproducible and gitignored. The
orchestrator's post-merge scan is the wave record.

After verifying everything is green, promote session branches to the cannonical branch.

At the end of the campaign, record one unseeded tree-wide scan:

```bash
crustify-audit <repo_root> unsafe --json
```

### Review objective

Run the `--objective review` stage if the user instructed you to do so, chosing the model
they've selected. This agentic review is independent of the deterministic
`crustify-audit unsafe` gate above. `crustify-audit ub` is outside the campaign
workflow.

### Accounting

Use `utils/log_cost.py` over the per-agent `<stage>.usage.json` to compute cost
and fetch token usage, and never from provider-reported dollars.
Fetch the session wall from `session.log`, agent wall from `<stage>.usage.json`.

Fill whatever evaluation table the user provides.

---

## Self-repair

If throughout driving campaigns you discover any bugs or flaws in `crustify-cli`,
`crustify-oracle`, or `ffibox`, including new generic primitives that can be used
for C/Rust interop in `ffibox`, then create a new branch on the respective repository,
naming it accordingly, and develop a patch for the fix / enhancement.
