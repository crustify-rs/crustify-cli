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

Every authored file has a commented example under `templates/`. Read the
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

Ask the user which agent backends it wants to install, as it may not want you to install both.
On macOS arm64 the CodeQL bundle needs Rosetta.

### 2. Bootstrap `crustify/`

```bash
mkdir -p <repo>/crustify
cp templates/gitignore <repo>/crustify/.gitignore
git -C <repo> checkout -b crustify/<target>
```

Author `<repo>/crustify/cli-config.json` from `templates/cli-config.json`:

| block | holds |
|---|---|
| `deps` | absolute path to the crustify checkout and to `crustify-prim`|
| `bins` | absolute path to `crustify-cli` and `crustify-oracle` |

**Absolute paths only.** An agent runs inside a git worktree, so nothing
relative to a cwd resolves the same way twice. The file is machine-local and
gitignored — it reaches a worktree through `worktree.link_shared`, not git.

### 3. `build.json`

Author from `templates/build.json`. Downstream stages treat it as authoritative
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
`templates/scope-config.json`.

By default every source file under `<repo_root>/<target>/` is port scope.
**`port_files`, when non-empty, REPLACES that implicit walk — it does not add to
it.** If you list anything, you must list everything the port scope contains.
This is the single easiest way to silently under-scope a target.

Entries are a file (`include/internal/statem.h`) or a directory with a trailing
slash (`ssl/`), which expands to every source and header beneath it. Prefer
directory entries when a whole subtree is port-scope; use a bare file list when
the port cluster is a logical subset of a directory. Naming a file the build
never compiled is harmless — T1 anchoring drops uncompiled candidates.

**Headers outside the target tree are never discovered automatically.** A header
that exports structs, enums or unions implemented by code in the target must be
added to `port_files` by hand. Add it only if its **implementors** are
port-scope — distinguish implementors from consumers and referencers. A header
whose types are merely *used* by the target belongs in wrap scope, and gets
there on its own through the import closure.

`out_of_scope.paths` refines what to skip inside the target;
`out_of_scope.features` is documentation only, for the same reason as
`build.json`'s `features`.

Verify the result before proceeding:

```bash
crustify-oracle <repo_root> <target> query files --port-only
crustify-oracle <repo_root> <target> query files --wrap-only
```

### 7. Crate placement and scaffold

Author `crustify/crates.json` — the whole-repo crate/module decomposition and
the placement oracle. Schema: `docs/schemas/crates.md`; layout example:
`templates/crates.json`.

Crate names ARE the link-unit keys: they match `build.json`'s `libraries` and
`executables`, and bindgen uses them as the library identity. `depends_on` comes
from `link_dependencies` and must be acyclic. Every library with bound entities
needs a `sys_crate`.

Use the oracle for the inventory to home, and `build.json` for the artefact
hierarchy:

```bash
crustify-oracle <repo_root> <target> query types  --port-only
crustify-oracle <repo_root> <target> query symbols --wrap-only
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
| scope is what you meant | `query files --port-only` / `--wrap-only` |
| placement consistent | `scaffold --validate` exits clean |
| FFI crates link | `cargo build` + `cargo test` on each `<lib>-sys` |
| DAG resolves | `query dag --layer 0` returns the leaf set |

---

## Phase 2 — Translation

> **DRAFT — structure only.** Sections below are placeholders; nothing here is
> authoritative yet. Until it is filled, drive waves from `crustify-cli
> translate --help` and the `--dry-run` plan.

A wave is one `crustify-cli … translate` invocation: the scheduler selects
units, batches them under budget, and spawns one agent per batch in its own git
worktree. Waves repeat bottom-up until the target is closed.

### 1. Wave mechanics

<!-- TODO: session branch `crustify/session/<verb>-<SESSION_ID>` (no checkout);
one worktree per agent under `crustify/.worktrees/`, one `crustify/agent/<slug>`
branch each; landing by push-to-session + rebase-on-rejection; why isolation is
correctness (a scoped `cargo check`) and not a parallelism trick; a worktree that
outlives the wave IS the failure signal. Source: `crustify/worktree.py`. -->

### 2. Plan a wave

<!-- TODO: selection — `--name`, `--dag-layer N`, `--transitive`, `--file`,
`--skip`, `--wrap-only`/`--port-only`. The bottom-up rule and how to read the
next layer off `query dag`. The `--lifetime-for void` then `string` tiers, why
that order, and why they precede the typed clusters. -->

### 3. Objectives

<!-- TODO: `--objective wrap|port|review` — what each asks the agent to DO, and
that it is NOT the scope filter. `wrap` drops already-done units (the gate that
makes `--transitive` usable); `port` and `review` bypass that gate because both
act on filled anchors. -->

### 4. Budgets and concurrency

<!-- TODO: `--max-syms` / `--max-loc` and the whichever-hits-first rule;
`config.TRANSLATE_MAX_*` defaults. `--parallel`, `--parallel-max N`,
`--parallel-policy per-agent|serialize-per-file|per-file` and when each is
right. Sizing a wave: layer width vs. host cores. -->

### 5. Run it

<!-- TODO: `--dry-run` first — units, batches, first-layer deps — then the wave.
What the live console and `targets/<target>/logs/<session>/` show. -->

### 6. Land and promote

<!-- TODO: the session branch is never auto-merged; landing it is a deliberate
act. Reviewing it, then promoting anchors to the canonical branch. Source:
`worktree.py` "The session branch is never merged … left for review". -->

### 7. Verify

<!-- TODO: `audit` and reading its surface counts; re-running
`build_commands.test` against `build.json.test_baseline`. -->

### 8. Account

<!-- TODO: `utils/log_cost.py` over the per-agent `<stage>.usage.json`; why cost
is computed from token counts and never from provider-reported dollars; the
per-wave tables under `crustify/evaluation/`. -->

### 9. Recover

<!-- TODO: surviving worktrees and agent branches as the inspectable record;
reading a failed agent's log; re-running a wave with `--skip`; when to re-scope
instead of re-run. -->

### Gates before the next wave

<!-- TODO: the per-wave checklist, mirroring Phase 1's gate table. -->
