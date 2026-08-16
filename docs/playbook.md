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
| `crustify-prim` | `git clone https://github.com/crustify-rs/crustify-prim.git` |

On macOS arm64 the CodeQL bundle needs Rosetta.

### 2. Bootstrap `crustify/`

```bash
mkdir -p <repo>/crustify
cp specs/gitignore <repo>/crustify/.gitignore
git -C <repo> checkout -b crustify/<target>
```

Author `<repo>/crustify/cli-config.json` from `specs/cli-config.json`:

| block | holds |
|---|---|
| `deps` | absolute path to the crustify checkout and to `crustify-prim`|
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

`files` names everything the target covers.

Entries are a file (`include/internal/statem.h`) or a directory with a trailing
slash (`ssl/`), which expands to every source and header beneath it.

**Scope says what the target contains, not what will be done with it.** Port or
wrap is `translate --objective`, chosen per unit by the orchestrator — an item
can be wrapped now and ported later, and encoding a verb here would freeze that.

**Name every file that DEFINES something you want in scope.** Classification is
*definition-anchored*: an entity is in the target iff its **body** lives in a
named file (or, for a declaration-only entity, all its declarations do). That
one rule cuts both ways, and missing either half silently drops work:

- **The sources.** A function's body is in a `.c`. Listing only public headers
  admits just what they *define* — inline functions, macros, value structs —
  while every function they merely *declare* falls out of scope entirely, its
  body sitting in a `.c` you did not name. To wrap a library's public API, name
  the library.
- **The headers.** A struct, enum or union is *defined* in a header, and headers
  outside the target tree are **never discovered automatically**. A public value
  type whose header you did not name is not in your target, however much of its
  implementation is.

Add a header only if its **implementors** are in the target — distinguish
implementors from consumers and referencers. A header whose types are merely
*used* by the target belongs in the import section, and gets there on its own
through the closure.

Narrowing to an API *surface* is a **selection** concern, not a scope one:
`translate --file include/openssl/ssl.h`. Keeping the implementations in scope
is what lets the ownership analysis read free/store/return behaviour off real
bodies instead of guessing from a signature.

Everything an in-scope entity **reaches** that `files` does not name is an
*import* — the FFI frontier — derived automatically into `scope.json`'s sibling
`import` section. Nothing needs to name it.

`out_of_scope.paths` refines what a directory entry expands to;
`out_of_scope.features` is documentation only, for the same reason as
`build.json`'s `features`.

Verify the result before proceeding:

```bash
crustify-oracle <repo_root> <target> query files --target-only
crustify-oracle <repo_root> <target> query files --import-only
```

The import section pools two populations that the section alone cannot
separate. `--out-of-tree` / `--in-tree` cut the independent ORIGIN axis —
whether the entity's home lies outside this repository:

```bash
# the permanent FFI floor
crustify-oracle <repo_root> <target> query types --import-only --out-of-tree
# first-party code this target reaches but does not cover
crustify-oracle <repo_root> <target> query types --import-only --in-tree
```

The first can never move into the target; the second could, by naming its
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
crustify-oracle <repo_root> <target> query types  --target-only
crustify-oracle <repo_root> <target> query symbols --import-only
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

Partitions the wrap-scope surface by owning crate into `<lib>-sys` crates.
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
| scope is what you meant | `query files --target-only` / `--import-only` |
| placement consistent | `scaffold --validate` exits clean |
| FFI crates link | `cargo build` + `cargo test` on each `<lib>-sys` |
| DAG resolves | `query dag --layer 0` returns the leaf set |

---

## Phase 2 — Translation

A wave is one `crustify-cli … translate` invocation: the scheduler selects
units, batches them under budget, and spawns one agent per batch in its own git
worktree. Waves repeat bottom-up until the target is closed.

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


