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
mkdir -p <repo>/crustify/targets/<target>/logs
cp specs/gitignore <repo>/crustify/.gitignore
git -C <repo> checkout -b crustify/<target>
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

Crustify does **not** create the database. Build it yourself, wrapping
`build_commands.build`.

This writes one CSV per query under `crustify/codeql/{t1,t2}/` — T1 entities,
T2 edges. Every type/symbol record, the scope sets and the dependency DAG derive
from these on demand. It takes minutes; re-run only after the C tree or the
database changes.

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

**`wrap` — expose the public API, owning nothing.** The `targeted` section
composes empty and the `imported` section is seeded off `api_headers` alone, on
**declaration-site** membership; `impl_files` is not read. That test is the
right one for a public header, whose declared bodies live in files the campaign
does not own — and it is what makes "wrap this library" expressible without
scoping its whole implementation. Point `api_headers` at published headers
(`include/openssl/`, `include/libxml/`), never at a source tree, or you seed on
declarations inside `.c` files.

Everything else follows from that one word, with no per-item flag:

| | named by the seeding key | not named |
|---|---|---|
| struct **defined** there | full field layout — its fields are the API | — |
| struct only **declared** there | opaque handle | opaque handle |
| symbol | orders on its body's callees | orders on its signature |

So `--transitive` over an opaque-exported type pulls the type and nothing else.

**Three different things are called an objective; keep them apart.**
`campaign_objective` shapes the **scope** and is authored once per target.
`translate --objective` is the **verb** handed to one agent over one selection,
chosen per wave by the orchestrator. Narrowing to an API *surface* is neither —
that is **selection**: `translate --file include/openssl/ssl.h`.

Ownership analysis is unaffected by any of it — `ptr_args`/`ptr_ret` come from
the T2 tables, which cover the whole database regardless of scope.

`out_of_scope.paths` refines what a directory entry expands to;
`out_of_scope.features` is documentation only, for the same reason as
`build.json`'s `features`.

To read a `wrap` campaign's graph as if it were a port — bodies walked, every
field kept, only genuinely imported items narrowed — pass `--full` to
`query dag`. It recomposes scope with `campaign_objective` forced to `port` and
caches beside the default graph; nothing in the config changes.

Verify the result before proceeding:

```bash
crustify-oracle <repo_root> <target> query files --targeted-only
crustify-oracle <repo_root> <target> query files --imported-only
```

The imported section pools two populations that the section alone cannot
separate. `--out-of-tree` / `--in-tree` cut the independent ORIGIN axis —
whether the entity's home lies outside this repository:

```bash
# the permanent FFI floor
crustify-oracle <repo_root> <target> query types --imported-only --out-of-tree
# first-party code this target reaches but does not cover
crustify-oracle <repo_root> <target> query types --imported-only --in-tree
```

The first can never move into the targeted set; the second could, by naming its
files.

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

Commit the scaffolded `rust/` tree on `crustify/<target>`. Translate waves land
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

A session branch is never auto-merged; landing it is a deliberate
act. Reviewing it, then promoting anchors to the canonical branch
is the orchestrator's job.

### Verify before proceeding

Check the agent's logs to make sure the C/Rust targets build and the tests pass.

### Accounting

Use `utils/log_cost.py` over the per-agent `<stage>.usage.json` to compute cost
and fetch token usage, and never from provider-reported dollars.
Session wall from `session.log`, agent wall from `<stage>.usage.json`.

Fill whatever evaluation table the user provides.


