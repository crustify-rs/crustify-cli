# Playbook

Driving crustify, in two phases. Setup: toolchain install through the first
commit of the scaffolded Rust tree — authoring `build.json`, `cli-config.json`
and a target's `scope-config.json`, building the CodeQL database and extracting
the T1/T2 tables, crate placement, scaffold and bindgen. Translation: planning,
running, landing and auditing waves of translate agents over that tree. Read
Setup before any wave; every later stage reads what it produces.

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

From an untouched checkout to the first commit of the scaffolded Rust tree.

### 1. Toolchains and checkouts

| need | install |
|---|---|
| Python ≥ 3.13 | system or `uv` |
| Claude Code CLI | `curl -fsSL https://claude.ai/install.sh \| bash` |
| OpenAI Codex CLI | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh` |
| Rust | rustup: `cargo`, `clippy`, nightly toolchain |
| `bindgen-cli` | `cargo install bindgen-cli` |
| CodeQL | the CodeQL CLI bundle, on `PATH` |
| `ffibox` | `git clone https://github.com/crustify-rs/ffibox.git` |

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

### 7. Crate placement and scaffold

Author `crustify/crates.json` — the whole-repo crate/module decomposition and
the placement oracle. Schema: `docs/schemas/crates.md`; layout example:
`specs/crates.json`.

Crate names ARE the link-unit keys: they match `build.json`'s `libraries` and
`executables`, and bindgen uses them as the library identity. `depends_on` comes
from `link_dependencies` and must be acyclic. Every library with bound entities
needs a `sys_crate`.

Use the oracle for the inventory to home, and `build.json` for the artefact
hierarchy:

```bash
crustify-oracle <repo_root> <target> query types  --targeted-only
crustify-oracle <repo_root> <target> query symbols --imported-only
```

Then materialize the tree and gate it:

```bash
crustify-cli <repo_root> <target> scaffold --all
crustify-cli <repo_root> <target> scaffold --validate
```

### 8. `bindgen`

```bash
crustify-cli <repo_root> <target> bindgen [--libs LIB …]
```

Partitions the import surface by owning crate into `<lib>-sys` crates.
**They come out incomplete by design**: `build.rs` carries the per-kind
allowlists but no `fn main`, and `bindgen.h`'s shim block is empty. Finishing
them needs a compiler in the loop, so complete them by hand.

- Write `fn main` and the clang args; generate the bindings.
- Diff the allowlists against the emitted `bindings.rs` to assess completeness,
  and fix what is missing.
- Write thin unit tests proving each `-sys` crate passes `cargo build` and
  `cargo test`.
- **Do not shim a macro that has no bindgen binding.** Worker agents generate
  those on demand during translate; a hand-written shim collides with what they
  emit.

### 9. Commit

Commit the scaffolded `rust/` tree on `crustify/<target>-<model>`. Translate waves land
on branches based off this commit, so it is the baseline every later diff and
promotion is read against.

### Gates before the first wave

| check | how |
|---|---|
| baseline recorded | `build.json.test_baseline` names pass/total and every disabled test |
| T1/T2 populated | `crustify/codeql/{t1,t2}/` non-empty |
| scope is what you meant | `query files --targeted-only` / `--imported-only` |
| placement consistent | `scaffold --validate` exits clean |
| FFI crates link | `cargo build` + `cargo test` on each `<lib>-sys` |
| DAG resolves | `query dag --layer 0` returns the leaf set |

---

## Phase 2 — Translation

A wave is one `crustify-cli … translate` invocation: the scheduler selects
units, batches them under budget, and spawns one agent per batch in its own git
worktree. Waves repeat bottom-up until the target is closed.

### Choosing the objective

`--objective` is taken as given — nothing derives it from an item's section — so
one run carries one verb and picking it is yours.

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

Review and promote session branches to the cannonical branch after agents land their branches.

### Verify before proceeding

Check the agent's logs to make sure the C/Rust targets build and the tests pass.

### Accounting

Use `utils/log_cost.py` over the per-agent `<stage>.usage.json` to compute cost
and fetch token usage, and never from provider-reported dollars.
Fetch the session wall from `session.log`, agent wall from `<stage>.usage.json`.

Fill whatever evaluation table the user provides.


