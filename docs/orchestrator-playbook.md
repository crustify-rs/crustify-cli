# Orchestrator playbook

Driving crustify, in two phases. Setup: toolchain install through the first
commit of the initial Rust tree — authoring `build.json`, `cli-config.json` and
a campaign-wide `oracle-config.json`, building the CodeQL database, extracting
the T1/T2 tables, emitting `subsystems.json`, and seeding crate shells.
Translation: preparing, running,
landing and scanning waves with `crustify-audit`. Read Setup
before any wave; every later stage reads what it produces.

Paths below are relative to the crustify checkout (`deps.crustify` in
`cli-config.json`). Run any command's `--help` for exact flags — argparse is the
source of truth.

## The artifact tiers

Three artifact tiers decide where a file goes.

| tier | path | authored | derived |
|---|---|---|---|
| repo | `<repo>/crustify/` | `build.json`, `crates.json`, `cli-config.json` | `subsystems.json`, `rust/` |
| oracle | `<repo>/crustify/oracle/` | `targets/<target>/oracle-config.json`, `ownership-store.json` | `codeql/{db,t1,t2}/`, `.cache/` |
| campaign | `<repo>/crustify/campaigns/<target>/` | `<sub-campaign>/scope-config.json` | `<sub-campaign>/<wave-name>.json`, `logs/<session>/` |

Repo-tier describes the whole repository. Oracle targets describe C inventory;
campaigns contain one directory per sub-campaign, its tracked scope and wave
plans, and one target-wide execution log namespace. A repo can carry several
oracle targets and many named sub-campaigns.

Every repo-tier artifact contract has a commented example under `specs/` —
except `oracle-config.json`, whose example lives in the standalone oracle
checkout's own `specs/`. Read the template before authoring or emitting an
artifact; detailed schema documents supplement the `_comment_*` keys.

### Campaign directory layout

Sub-campaign scope and wave plans live below the target campaign directory;
logs remain target-wide:

```text
crustify/campaigns/<target>/
├── raw-lifetime-void/
│   ├── scope-config.json
│   └── <wave-name>.json
├── raw-lifetime-string/
│   ├── scope-config.json
│   └── <wave-name>.json
├── <sub-campaign>/
│   ├── scope-config.json
│   └── <wave-name>.json
└── logs/
    └── <session>/
        ├── session.log
        ├── <stage>[__<agent-suffix>].log
        └── <stage>[__<agent-suffix>].usage.json
```

`<target>` is the repo-relative oracle target passed to the CLI, so a target
such as `ssl/statem` creates nested directories, while the repo-root target
uses `crustify/campaigns/` directly. `<session>` is generated once per CLI
invocation as `YYYY-MM-DD_HH-MM-SS_<4-hex>`.

Wave paths do not select the log directory. The CLI always emits logs under
`crustify/campaigns/<target>/logs/<session>/` from its target argument, so all
sub-campaigns for one target share the same log namespace. Scope configs and
wave plans are tracked; `logs/` is gitignored.

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
| `wavefront` | clone beside `ffibox`; `python -m pip install -e <checkout>` |

On macOS arm64 the CodeQL bundle needs Rosetta.

**A provisioned environment has already done all of this.** When
`CRUSTIFY_DEP_CRUSTIFY` is set, the toolchains are installed, the four
checkouts are in place and the Python projects are installed editable — the
table above is already satisfied. Do not clone or reinstall any of it: a second
copy is not the one on `PATH`, and the paths the agents are handed below must
be the provisioned ones. Skip to step 2.

### 2. Bootstrap `crustify/`

```bash
mkdir -p <repo>/crustify
cp specs/gitignore <repo>/crustify/.gitignore
```

Author `<repo>/crustify/cli-config.json` from `specs/cli-config.json`:

| block | holds |
|---|---|
| `deps` | absolute paths to the crustify, wavefront, ffibox and crustify-audit checkouts |
| `bins` | absolute paths to `crustify`, `wavefront` and `crustify-audit` |
| `prompt_capabilities` | optional skill instructions injected per agent role |

**Absolute paths only.** An agent runs inside a git worktree, so nothing
relative to a cwd resolves the same way twice. The file is machine-local and
gitignored — it reaches a worktree through `worktree.link_shared`, not git.

**Take the paths from the environment when it offers them.** A provisioned
environment may export exactly these values.

For translators, list any of `wavefront`, `ffibox` and
`crustify-audit` under `prompt_capabilities.translator`. A missing capability
is omitted from the rendered prompt. This is an instruction ablation only: it
does not hide the checkout, executable, dependency or path from the agent.

### 3. `build.json`

Author from `specs/build.json`. It fixes the exact shell strings used from the
repo root for the campaign's three build stages: `configure`, `build`, and
`test`. Increment its `version` whenever any command changes; derived artifacts
record that version as provenance.

- Prefer a `configure` that disables deprecated features.
- Enable sanitizers, so agents catch memory-safety violations when testing their
  Rust against the C.
- Prefer parallel `build` commands; on a hybrid-core host, distribute over
  performance cores.

### 4. Build and baseline

Run `configure`, then `build`. Then run `test` to collect the port-equivalence
baseline, disabling any test that fails on the unported tree.

Record pass/total plus the name of every test disabled to reach that state in
the campaign record. A post-port run must match it. This is the only evidence
that a translation preserved behaviour, and it cannot be reconstructed later.

### 5. CodeQL database and the T1/T2 tables

Build the CodeQL database manually, and run `wavefront extract-ql`
to emit the T1 (entities) and T2 (edges) against that database.

It writes one CSV per query under `crustify/oracle/codeql/{t1,t2}/` — T1 entities, T2
edges. Every type/symbol record, the scope sets and the dependency DAG derive
from these on demand, which is why this is the one oracle command with side
effects and the only one that must be run explicitly. It takes minutes; re-run
it only after the C tree or the database changes.

### 6. Configure the campaign-wide oracle target

Author `crustify/oracle/targets/<campaign-target>/oracle-config.json` from the
standalone oracle's `specs/oracle-config.json`.

This first target spans the user's campaign selection. If the user named target
subsystems, include those target implementation paths; if the user selected the
whole target, include all of its implementation paths; if the user asked you to
choose subsystems, include what you chose. This common target is
the inventory from which the orchestrator decomposes both the selected target
surface and its imported producer closure. More narrowly scheduled
sub-campaigns may be derived after decomposition.

It names **two file sets**. Entries in either set are a file
(`include/internal/statem.h`) or a directory with a trailing slash (`ssl/`),
which expands to every source and header beneath it. Naming a file the build
never compiled is harmless — T1 anchoring drops uncompiled candidates.

| key | what it names |
|---|---|
| `impl_files` | the sources — and private headers — that **implement** the library |
| `api_headers` | the headers that **publish** its API |

Both file sets are authored for every target. The oracle has no wrap/port
objective.

**Implementation graph.** `impl_files` + `api_headers`
together seed the `targeted` section. Classification is *definition-anchored*:
an entity is targeted iff its **body** lives in a named file (or, having no
body, all its declarations do). Name the implementations **and** the headers
that define the types — headers outside the target tree are never discovered
automatically, and a header-only list drops every function it merely
*declares*, whose body sits in a `.c` you did not name. Put a header in
`api_headers` only if its **implementors** are in `impl_files`; one whose types
are merely *used* reaches the imported section on its own.

**Public API graph.** Pass `schedule --api-headers-only`. It walks no bodies,
and only a struct **defined** in `api_headers` keeps its field layout. Forward
declarations stay opaque. API declarations seed the selection; `--transitive`
still includes their non-public signature dependencies.

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
| implementation graph | full field layout | opaque handle |
| `--api-headers-only` | full layout iff defined in `api_headers` | opaque handle |

So `--transitive` over an opaque-exported type pulls the type and nothing else.

`translate --objective` is the **verb** handed to every agent in the submitted
wave, chosen per wave by the orchestrator. A target type in a port campaign might first
be wrapped and then ported, which is what the flag exists for. 

`out_of_scope.paths` refines what a directory entry expands to;
`out_of_scope.features` is documentation only.

Verify the result before proceeding:

```bash
wavefront <repo_root> <target> query files --targeted-only
wavefront <repo_root> <target> query files --imported-only
```

After the user picked a target, create the campaign's base branch and
logs directory:

```bash
git -C <repo> checkout -b crustify/<target>-<model>
mkdir -p <repo>/crustify/campaigns/<target>/logs
```

This scaffolding is orchestrator-owned. `wavefront schedule --output`
writes the requested wave file but fails if its parent directory does not
already exist.

### 7. Emit `subsystems.json`

After the campaign-wide oracle target is populated, emit
`crustify/subsystems.json` from `specs/subsystems.json`. Field semantics:
`docs/schemas/subsystems.md`.

Discover link units from the configured build's actual linker outputs. Store
link units and their subsystems as ordered lists; each list entry is identified
by its `name`. Cover exactly the target span the user selected and include its
complete imported producer closure. Mark every subsystem `scope: targeted` or
`scope: imported`, consistently with the oracle.

Home every covered translation unit to exactly one subsystem. Keep a subsystem
scope-homogeneous: do not mix targeted and imported translation units. Use the
oracle's LoC, type, symbol, and edge statistics whenever available. Aggregate
each consumer-to-producer relationship into one `depends_on` record with
`nr_edges`.

The resulting subsystem graph must be acyclic. Resolve a cycle by changing the
decomposition—rehome translation units or merge subsystems—rather than omitting
real dependency records. A subsystem with more incoming consumer edges has
greater producer weight and should preferentially remain a producer;
`nr_edges` refines that judgment.

This is orchestrator judgment, not a new mechanical validation command or
gate.

### 8. Seed crate shells

Author `crustify/crates.json`, the placement oracle. Schema:
`docs/schemas/crates.md`; example: `specs/crates.json`.

Seed the campaign's target crate and the top-level crates that own its imported
dependencies. Leave `modules` empty and do not home items yet. Crate names
match `subsystems.json`'s `link_units[*].name`; derive their dependency
relationships from subsystem `depends_on` records.

The orchestrator creates minimal compiling wrapper crates. Each starts with a
`Cargo.toml` and empty crate root following `conventions.md`. Do not create
campaign modules yet.

For each target or imported library crate, create its `<lib>-sys` placeholder:
`Cargo.toml`, `src/lib.rs`, `build.rs` and the bindgen input. Its bindgen
pipeline must compile with an empty, no-match agent-owned allowlist. Translator
agents populate that allowlist lazily.

Gate the shells:

```bash
crustify <repo_root> <target> crates validate
cargo build
cargo test
```

### 9. Commit

Commit the initial `rust/` tree on `crustify/<target>-<model>`. Translate waves
branch from this baseline.

### Gates before the first wave

| check | how |
|---|---|
| baseline recorded | campaign record names pass/total and every disabled test |
| T1/T2 populated | `crustify/oracle/codeql/{t1,t2}/` non-empty |
| scope is what you meant | `query files --targeted-only` / `--imported-only` |
| placement consistent | `crates validate` exits clean |
| FFI crates link | `cargo build` + `cargo test` on each `<lib>-sys` |
| DAG resolves | `query dag --layer 0` returns the leaf set |

---

## Phase 2 — Translation

A wave is one scheduler-produced JSON document. The oracle selects its workset
and divides it into sequential steps; the CLI enforces each step barrier and
routes that step's batches to agents. Waves repeat until the target is closed.
See `docs/schemas/wave.md` for the producer/consumer contract.

### Plan sub-campaigns

For a port campaign, split the selected campaign span into one sub-campaign per
subsystem in `subsystems.json`, including imported producer subsystems needed by
the selected targets. Imported or deliberate C-boundary subsystems use the
`wrap` objective; selected migration subsystems use `port` according to the
objective rules below.

Schedule these sub-campaigns bottom-up over the subsystem graph. Because a
`depends_on` record points consumer to producer, complete producers before
their consumers. Use a deterministic topological order, breaking equally ready
subsystems by greater producer weight and then by `(link_unit, subsystem)`
name. Never start a consumer sub-campaign while one of its required producer
sub-campaigns remains incomplete.

Author
`crustify/campaigns/<target>/<sub-campaign>/scope-config.json` from
`specs/scope-config.json` for every raw-lifetime and subsystem sub-campaign.
Field semantics: `docs/schemas/scope-config.md`. Use the campaign-wide oracle
target to resolve and record the exact targeted and imported file, type, and
symbol closure before scheduling the first wave. Do not infer closure sets from
directory names or from `subsystems.json` statistics.

### Prepare each wave

Before `translate`, the orchestrator:

1. runs `wavefront schedule` with the selection and batch budgets, writing
   `crustify/campaigns/<target>/<sub-campaign>/<wave-name>.json` from that
   sub-campaign's `scope-config.json`;
2. runs imported campaigns for any unsatisfied dependencies;
3. homes each wave item in `crates.json`;
4. creates its `.rs` files and connects them to the crate root;
5. runs `crates validate` and compiles the affected crates;
6. runs `translate <wave.json> --dry-run`, then executes it.

```bash
wavefront <repo_root> <target> schedule \
  --output <repo>/crustify/campaigns/<target>/<sub-campaign>/<wave-name>.json \
  --name <items...> [--transitive] [--api-headers-only] \
  [--max-syms N] [--max-loc N] [--max-types N] [--min-fields N]
crustify --parallel-max N <repo_root> <target> translate \
  <repo>/crustify/campaigns/<target>/<sub-campaign>/<wave-name>.json \
  --objective wrap --dry-run
```

The bottom-up plan should already have completed imported producers before a
target wave needs them. If an exact oracle closure reveals an unplanned
imported subsystem, pause the consumer, add that producer as its own
sub-campaign and `scope-config.json`, complete it, then resume the consumer.
Imported work uses the crate shells seeded during Setup.

The scheduler inserts TODO anchors. Translator agents extend their worklist's
bindgen allowlists and regenerate bindings in their worktrees.

Do not change `crates.json` during a wave. When landing parallel agents, union
their `<lib>-sys` allowlist changes and rerun the affected crate tests.

### Choosing the objective

A **wrap** campaign runs every wave at `--objective wrap`.

A **port** campaign distinguishes the user-selected migration set from its
dependency closure. A selected type runs `wrap` when it is scheduled for the
first time, so it stays layout-compatible while C still reads its fields, and
runs `port` once those C-side readers are gone. A selected symbol runs `port`
directly. `port` re-visits a filled anchor deliberately, so it is how an item is
escalated rather than redone, and it is what starts the opacification burn-down.

When the user chooses to migrate only a subset of the targeted closure, the
remaining targeted dependencies may run `wrap` and form the deliberate C/Rust
boundary. This applies to symbols as well as types: a wrapped dependency keeps
its C implementation and exposes a safe Rust surface to the selected ported
items. Partition these into an earlier `wrap` wave because one submitted wave
has one objective; do not mix closure-only wrappers and selected port items in
the same wave. If the user chooses the whole
targeted closure instead, targeted symbols continue to run `port` directly.

Do not include completed items when authoring the next oracle schedule.

### Raw lifetime discovery sub-campaigns

Regardless of the target set, the first two sub-campaigns are raw lifetime
discovery. They produce release/clone strategies for owned pointers that host
type-erased and NUL-terminated objects. Generate the `raw-lifetime-void` waves
with `schedule --lifetime-for void`, complete and review that sub-campaign as
allowed, then do the same for `raw-lifetime-string` with
`schedule --lifetime-for string`. When resuming an interrupted campaign, skip
either sub-campaign only if it has already completed.

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
with its safety justification.

`crustify/audit/unsafe.json` is reproducible and gitignored. The
orchestrator's post-merge scan is the wave record.

After verifying everything is green, promote session branches to the canonical branch.

At the end of the campaign, record one unseeded tree-wide scan:

```bash
crustify-audit <repo_root> unsafe --json
```

### Review objective

If the user allowed agentic review, prefer one `--objective review` pass after
each completed sub-campaign, including each raw-lifetime sub-campaign. Review
the whole landed sub-campaign rather than reviewing every wave independently.
Use the backend and model the user selected, or orchestrator's choice when they
delegated it. This agentic review is independent of the deterministic
`crustify-audit unsafe` gate above. Prefer larger caps for review so each agent
sees more related units; use 3x the translation caps by default.

### UB patch promotion

Run `crustify-audit ub` only with the user's explicit approval. The UB agent
should normally run once at the end of the whole campaign, after all
sub-campaigns and their allowed review passes have landed. Run it earlier only
when the user explicitly requests another milestone or a confirmed finding
blocks further work. The UB agent
owns both the evidence and the repair: it creates a dedicated branch in the
target repository, follows that repository's conventions, implements focused
regression tests, builds the affected targets, runs their gates, reruns the
reproduction, and commits the patch without merging it. The orchestrator does
not rewrite that patch. Inspect its diff and evidence, independently rerun the
relevant build, test, and reproduction gates, and merge the agent branch into
the canonical campaign branch only when they are green and the change is
confined to the confirmed finding. Otherwise leave it unpromoted and report the
specific failure.

### Accounting

Use `crustify-log-cost` over the per-agent `<stage>.usage.json` to compute cost
and fetch token usage, and never from provider-reported dollars.
Fetch the session wall from `session.log`, agent wall from `<stage>.usage.json`.

Fill whatever evaluation table the user provides.

---

## Self-repair

If throughout driving campaigns you discover any bugs or flaws in `crustify`,
`wavefront`, `crustify-audit`, or `ffibox`, including new generic primitives that can be used
for C/Rust interop in `ffibox`, then create a new branch and worktree on the respective repository,
naming it accordingly, and develop a patch for the fix / enhancement.
