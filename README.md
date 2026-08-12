# Crustify

Crustify leverages LLM agents to automate the migration of production C/C++ codebases to **safe,
idiomatic Rust**, incrementally and efficiently, with little human involvement.

Point it at a repo and it will map its build system and test suite, extract an exact dependency
graph of its types and symbols, and translate them in dependency order, focusing on maximizing
safety without sacrificing correctness.


## Why Crustify

LLM agents have become extremely powerful at every software engineering task. However, they need the
right guidance, the right unit of work, and the right tools to prevent them from hallucinating,
reward hacking, burning unnecessary tokens, and being imprecise. For example, when asked to analyze
code (e.g. to find the footprint of a `struct` field), LLMs default to `grep` and regex-matching
over source code. This is, however, a notoriously inaccurate static analysis method that may miss
true sites (_false negatives_) or record false ones (_false positives_). Additionally, an LLM's
non-deterministic nature is especially aggravated in the world of transpilation where an idiom in
one language can be expressed in many ways in the other language (e.g. a C/C++ struct in Rust). It
is thus imperative to establish conventions and specifications for LLMs to enable them to produce
deterministic and reproducible outputs.

Crustify provides all of these: (a) properly engineered, clear prompts that ensure LLMs don't derail
from the task, (b) pointers to the right Rust primitives that facilitate code with memory- and
type-safety guarantees (c) a deterministic dependency graph of types and symbols to guide them
through an incremental, bottom-up translation that enables safety-first coding, (d) a balanced
workload and task decomposition to keep them focused and enable parallel agent work, and (e) coding
conventions and structured specifications to enable deterministic outputs.


## Quick Setup

The following bootstraps the harness for a C-to-Rust port driven by an AI orchestrator. You can
spawn the orchestrator as an assistant in your favorite AI-based IDE or as a fully autonomous agent
in your terminal.

**1. Clone [`crustify-cli`](https://github.com/crustify-rs/crustify-cli).**

```bash
git clone https://github.com/crustify-rs/crustify-cli.git
```

**2. Clone [`crustify-prim`](https://github.com/crustify-rs/crustify-prim).** It carries the smart
pointers and lifetime traits used to emit safe FFI wrappers for C/C++/Rust interop.

```bash
git clone https://github.com/crustify-rs/crustify-prim.git
```

**3. Install an AI assistant** *(optional — skip if you already have one).*

```bash
curl -fsSL https://claude.ai/install.sh | bash        # Claude Code
curl -fsSL https://chatgpt.com/codex/install.sh | sh  # OpenAI Codex
```

**4. Spawn the orchestrator.** Render the orchestrator prompt and hand it to your assistant:

```bash
utils/build-orchestrator-prompt.sh <crustify-prim-checkout> -o orchestrator.md
```

It will first ask you to point it at the repo root and the target subsystem(s) you want to translate
— a subset of files, an entire `src/` directory, or the whole repo. Then it will wait for your
approval before starting work. Adjust to your liking / use case.


## Use Cases

Crustify can help you automate the following C-to-Rust tasks:

**Safe wrappers for C/Rust interop.** Crustify can emit a safe wrapper interface over the public API
of a C/C++ library, and re-export it so that Rust-native consumers can integrate it without having
to use unsafe or raw pointers. Moreover, if the library is already written in Rust, or is in the
process of being migrated to Rust, Crustify can emit safe wrappers for its public API so that C/C++
consumers can integrate it without introducing undefined behavior hazards.

**Incremental migration to Rust.** Crustify can also automate the migration of production C/C++
codebases to memory-safe, idiomatic Rust. It first decomposes the target in smaller units (symbols
and types), and translates them in dependency order, bottom-up. Some lower-layer Rust items may
still need to stay interoperable with the higher-layer C/C++ code (e.g. a god object storing a Rust
handle) — Crustify reuses the same approach of safe wrappers over FFI items, temporarily, and once
they no longer cross the FFI boundary it nativizes them.

**Partial migration to Rust.** Using the above principles, Crustify can also narrow its scope to
migrate only a subset of the subsystems, files, or types/symbols, keeping them interoperable with
what stays in C/C++.


## Agent Harness

Crustify employs a simple agent harness consisting of **three LLM agents** that can run in parallel
to make the most out of available compute resources:

- A **type translator** specialized in `structs`, `enums` and `unions`;
- A **symbol translator** specialized in `functions`, `function pointers` and `global variables`;
- An **orchestrator** tasked with spawning translators and ensuring the harness runs smoothly.

**Agent Backends.** Crustify integrates state-of-the-art agent frameworks to access frontier models,
both proprietary and open-source, with built-in context management, long-horizon execution, and
fine-tuning for coding tasks. To execute an agent, the backend simply spawns a Python subprocess
that invokes the provider's CLI in a shell session.

Crustify currently supports the following agent backends:

- [Claude Code CLI](https://code.claude.com/docs/en/quickstart) for Anthropic models
- [OpenAI Codex CLI](https://github.com/openai/codex) for OpenAI-compatible models

Billing modes: subscription-based (Claude Max, Codex Pro) or via API key (BYOK).


## The Semantic Oracle

Crustify enables LLM agents to analyze code with high precision by equipping them with a **_semantic
oracle_** - a CLI tool that leverages [CodeQL](https://codeql.github.com/) to statically analyze a
target repository and extract an exact dependency graph between types and symbols. The `.ql` queries
are verified and tracked in this repo, so agents do not have to re-invent them on the fly every
time, which would re-introduce the very inaccuracy issues mentioned above.

The semantic oracle has three primary jobs:

1. It builds a **dependency graph** of the C/C++ items - types, functions, function pointers, global
   variables - by analyzing the AST at fine granularity, down to field-level access. It then applies
   Tarjan's algorithm to sort it in topological order, and turns it into a _DAG_ by flattening SCCs
   while keeping track of cut edges.

2. It maintains an **`ownership-store.json`** where agents can submit ownership and borrow judgement
   of C/C++ pointers, as well as lifetime primitives of types (drop destructors, cloners) — both of
   which are ambiguous to static and dynamic analysis alike.

3. It ships as a **CLI binary** that the orchestrator and the translator agents can query to learn
   the above properties about the types, symbols, or pointers in their workset.


## How it works

Crustify is a pipeline consisting of two phases, each with its own stages and structured I/O
artifacts, where work is split between **deterministic composers for mechanical tasks** and **LLMs
for semantic reasoning and codegen**. The composers are implemented in Python and packaged as two
CLI binaries that can be driven by LLMs and humans alike. The pipeline is designed to allow humans
to verify and modify the artifacts produced by the LLM between stages, which can help find pitfalls
and fine tune the outputs for the downstream stages.

### 1. Setup

This phase prepares the build environment, bootstraps the semantic oracle, and scaffolds the Rust
tree prior to any translation work. It can be driven entirely by the **orchestrator agent**. Below
is a simplified description of its stages, while the [playbook](docs/playbook.md), which the
orchestrator agent also follows, contains more details.

**Target discovery.** The orchestrator first identifies the build artifacts of the target
(executables, libraries) and its configure/build/test commands. Then, it configures and builds it,
runs the test suite to collect a baseline, and authors **`build.json`** where it documents its
findings. `build.json` will later drive the organization of the Rust tree in crates, and it will act
as a source of truth for downstream agents for obtaining build/test commands.

**CodeQL extraction.** The orchestrator builds the CodeQL database and runs the `.ql` extraction
queries to generate `CSV` tables that store static information about code items:
argument/return/variable types, field accesses, typedef aliasing, and more. The tables are then
consumed by the oracle to build the dependency graph.

**Scope specification.** The orchestrator authors **`scope-config.json`** where it lists all the TUs
and headers that form the port target selected when bootstrapping the orchestrator agent. Crustify
splits a target in two scopes, and the split drives everything else:
  - **port scope** - code that Rust will own. It gets rewritten as native Rust.
  - **wrap scope** - the import closure that port code reaches: types, functions and callbacks that
    stay in C and cross the FFI boundary. It gets **safe wrappers**.

**Crates specification.** The orchestrator authors **`crates.json`** where it sketches a hierarchy
of Rust crates, modules, and `.rs` source files that govern how the Rust tree will be organized, and
assigns each target type/symbol to a TU. It then runs a deterministic composer to scaffold a
skeleton Rust tree on disk based on `crates.json`. This step also emits a `-sys` companion crate for
every target crate, which will home the `bindgen` bindings. Users may adjust `crates.json` to their
liking for a custom Rust-tree organization.

**FFI bindings.** The orchestrator emits FFI bindings via `bindgen` by first running a mechanic
composer that emits an incomplete `build.rs` for every `-sys` crate, allowlisting the C/C++ items
that need a Rust binding. The allowlists are composed mechanically based on the dependency relations
fetched from the oracle. The orchestrator is then tasked to complete each `build.rs` driver, build
it, and ensure all required bindings are properly emitted.

### 2. Translation

Here's where translation work happens. The bits below give a high-level summary of Crustify's
translation philosophy, while the complete workflow can be best understood by consulting the prompts
of the [type](src/crustify/prompts/types.md) and [symbol](src/crustify/prompts/symbols.md) agents,
and the [philosophy](docs/principles.md) document.

**Agent task.** In short, each symbol and type agent is tasked with the following steps:
  - Read `principles.md`
  - Use `crustify-oracle` to obtain the footprint of your target set
  - Identify your items' `bindgen` bindings; add if missing
  - Analyze pointer ownership and type lifetimes and submit findings to `ownership-store.json` via
    `crustify-oracle`
  - Emit safe wrappers or port to native Rust
  - Write Rust unit tests, run safety audit and fix issues
  - Re-export ported items, build and run original tests
  - Commit changes, merge in parent, fix conflicts, and purge worktree once landed

**Batch scheduling.** Crustify employs a deterministic scheduler that queries the dependency graph
for a set of translation seeds to produce batches and routes them to the type/symbol agents. The
batching policy is governed by DAG layers---a single agent's batch only contains items from the same
layer, and lower layers get scheduled before higher ones. Crustify also supports **workload
tuning**: the symbol agent takes a batch of symbols capped by a configurable max number of symbols
and `LoC` (currently 50 and 1000), while the type agent currently only gets a single type. Agents
with batches at the same layer run concurrently in **isolated worktrees** via a configurable
concurrency threshold, i.e. max nr of cores. The orchestrator agent chooses how to best use the
scheduler for driving translation campaigns.

**FFI interop.** Interoperability across the FFI boundary is the hard, historically manual half, and
is Crustify's current focus. A wrapper lets a C type be used from Rust with no raw pointers, no
`unsafe` at the call site and no naked FFI — which puts the borrow checker back in charge of
ownership and lifetimes for an item the compiler could otherwise say nothing about. Field access
goes through generated accessors; lifecycle (free/clone) becomes `Drop`/`Clone`. Crustify agents
generate safe FFI wrappers for types, functions, function pointers, and globals, both that are
external dependencies of the port target (e.g. a library), and that cross the FFI boundary in an
incremental translation (e.g. a god object sitting at a higher layer). Wrap jobs are scheduled
deterministically and in dependency order via the oracle's DAG.

**Type Nativization.** Once a wrapped type stops being accessed across the FFI boundary, or even
stops crossing it, then it can be nativized and fully owned by the Rust world. We distinguish
between two cases: (a) if a type is opaque to FFI but still crosses the boundary, then its layout
can be nativized, but its storage (i.e. heap allocation) is still owned by C; (b) if a type does not
cross the FFI boundary anymore, then its storage can also be nativized. The orchestrator agent must
decide when a type is ready to be nativized.

**Safety Audit.** Crustify ships a deterministic pass over the compiled Rust's `HIR` and `typeck`
that counts the number of unsafe lines/statements/blocks and the number of raw pointers, flagging
FFI types/symbols that already have a safe wrapper or Rust-native shape, as well as the number of
`&mut` mutable borrows and field projections outside `impl` blocks. The translator agents are
instructed to use the audit pass to collect unsafe and potentially-UB sites, and to fix them unless
justified.

**LLM-as-a-Judge.** Each translator agent can act as a reviewer for an existing Rust translation or
for an ownership judgement submitted to `ownership-store.json`. Agents are instructed to enter this
mode via a simple paragraph in their prompt, and they are told to fix any mistakes if they find. The
orchestrator agent can run reviewer agents by running `crustify-cli translate --objective review`.

**Idempotency.** The scaffolder emits placeholder anchors (`// crustify:todo`) for every port/wrap
item that is in scope, and translator agents are asked to promote them for done work. This helps the
orchestrator with accounting and idempotency upon resuming an interrupted campaign.

**Regression and Equivalence Testing.** Translator agents are asked to surround the ported C/C++
items in their original source files with `#ifdef CRUSTIFY_*` macros and to modify the build/config
system to enable/disable them. Then each translator agent is asked to test the original build and
test suites with both the Rust code on and off, which catches regressions and ensures equivalence.


## CLI

Crustify facilitates LLM agents to interact with the pipeline via two binaries: the semantic oracle
and the CLI translation driver.

```bash
crustify-oracle <repo_root> <target> {extract-ql | query {types|symbols|files|dag}}

crustify-cli [globals] <repo_root> <target> {scaffold | bindgen | translate | audit}
```

### `crustify-oracle`

Four query subjects, each with its own modes:

| subject | modes | flags |
|---|---|---|
| `types` | enumerate · introspect · submit | `--fields` `--ops` `--methods` `--field-touchers` `--manifest` |
| `symbols` | enumerate · introspect · submit · lifecycle discovery | `--lifetime-for` `--taking` `--calling` `--hops` `--array` `--manifest` |
| `files` | the port set / the wrap closure | `--port-only` `--wrap-only` |
| `dag` | closure · layer slice · flattened-cycle twins | `--name` `--layer` `--scc` `--depth` `--loc` |

`--name` filters, `--file` disambiguates a name defined in more than one place, `--port-only` /
`--wrap-only` narrow any subject, and `--update` is the only writer.

`query <subject> --help` is the authority for what each flag means and for the record semantics
behind it; `--update-help` prints the findings schema `--update` expects, `--schema` the record's
own field definitions.

### `crustify-cli`

Four stages, run in this order the first time:

| stage | does | flags |
|---|---|---|
| `scaffold` | homes each C entity in the `.rs` that carries its `// Wraps:` / `// Replaces:` anchor, via `crates.json` | `--all` `--name` `--create` `--validate` `--file` `--dir` |
| `bindgen` | composes the `<lib>-sys` FFI crates, partitioning the wrap-scope surface by owning crate | `--libs` `--reset` |
| `translate` | emits the wrappers, layer by layer, one agent per batch | `--name` `--dag-layer` `--transitive` `--skip` `--objective` `--max-syms` `--max-loc` `--lifetime-for` `--dry-run` · `--model` `--billing` `--parallel` `--parallel-max` `--parallel-policy` `--override-base-prompt` `--no-console` `--no-file-log` |
| `audit` | scans the emitted tree for `unsafe`, raw pointers and naked `ffi::`, as JSON on stdout | `--all` `--name` `--crate` `--mod` `--file` |

`translate` is the only stage that spawns agents; the other three are deterministic composers.

`--objective wrap\|port\|review` says what to do with a selection, `--transitive` expands each name
through its dependency closure, and `--max-syms`/`--max-loc` cap per-agent effort so a god object
cannot blow one context. Start with `--dry-run`: a high-layer seed pulls in a large closure, and the
plan reports the objective each batch will actually get.

`<stage> --help` is the authority for what a flag means.

---


## Results

Two experiments: our own forks of [libgit2](https://github.com/crustify-rs/crustify-libgit2) (target
`src/`) and [OpenSSL](https://github.com/crustify-rs/crustify-openssl) (target `ssl/`, i.e.
`libssl`), both >100K LoC.

LLM used: `claude-opus-5`

### 1. Wrap the FFI closure

Goal: wrap a subset of types and symbols in the target's wrap closure — the prerequisite for porting
anything.

**libgit2 `src/`** — the full wrap closure of types, and a subset of symbols.

Types:

| layer | types | $ | $/type | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|
| 0 | 67 | $453.23 | $6.76 | $0.016 | 1h12m | 29,060 |
| 1 | 8 | $61.61 | $7.70 | $0.009 | 29m11s | 7,037 |
| 2 | 1 | $8.95 | $8.95 | $0.007 | 19m22s | 1,365 |
| **Σ** | **76** | **$523.79** | **$6.89** | **$0.014** | **2h01m** | **37,462** |

Symbols — the wrap closure of port layers 0–2:

| layer | symbols | $ | $/symbol | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|
| 0 | 23 | $15.46 | $0.67 | $0.006 | 29m45s | 2,613 |
| 1 | 32 | $41.30 | $1.29 | $0.010 | 53m05s | 4,053 |
| **Σ** | **55** | **$56.76** | **$1.03** | **$0.009** | **1h23m** | **6,666** |

55 of that closure's 97 symbols, one pooled agent per layer. The other 42 are 34 still open plus 8
that landed outside these waves — 6 lifecycle primitives that rode in with their owning type,
already priced in the types table above.

**OpenSSL `ssl/`** — a subset wrap closure of types and symbols.

Types:

| layer | types | $ | $/type | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|
| 0 | 40 | $283.62 | $7.09 | $0.017 | 26m42s | 16,634 |
| 1 | 6 | $50.82 | $8.47 | $0.017 | 24m41s | 3,049 |
| **Σ** | **46** | **$341.80** | **$7.43** | **$0.017** | **1h04m** | **20,354** |

Symbols — the wrap closure of port layers 0–2:

| layer | symbols | $ | $/symbol | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|
| 0 | 28 | $30.02 | $1.07 | $0.008 | 33m51s | 3,793 |
| 1 | 32 | $30.60 | $0.96 | $0.009 | 49m44s | 3,566 |
| **Σ** | **60** | **$60.62** | **$1.01** | **$0.008** | **1h23m** | **7,359** |

### 2. Transitively wrap god objects

Goal: pick three port-scope types with a large field surface and wrap their entire transitive
dependency closure, bottom layer up. This is the shape a real port takes.

Both targets were run with three seeds of ≥25 declared fields.

| | libgit2 `src` | OpenSSL `ssl` |
|---|---|---|
| seeds | `git_indexer` · `git_packbuilder` · `git_repository` | `record_layer_st` · `quic_stream_st` · `ssl_session_st` |
| seed fields | 30 · 29 · 29 | 25 · 35 · 41 |
| units (incl. seeds) | 75 | 65 |
| port / wrap | 66 / 9 | 39 / 26 |
| new here (rest wrapped by experiment 1) | 68 | 47 |
| depth | 8 layers | 12 layers |
| share of port scope | 10.2% of 646 types | 9.0% of 399 types |

**libgit2** — 68 units, port and wrap scope; the 7 wrap-scope types experiment 1 already wrapped are
excluded so the two experiments do not count the same agent twice:

| layer | types | $ | $/type | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|
| 0 | 18 | $156.49 | $8.69 | $0.011 | 28m47s | 13,971 |
| 1 | 22 | $265.39 | $12.06 | $0.010 | 42m44s | 25,918 |
| 2 | 12 | $150.21 | $12.52 | $0.013 | 38m31s | 11,585 |
| 3 | 7 | $91.31 | $13.04 | $0.009 | 31m44s | 10,065 |
| 4 | 4 | $54.23 | $13.56 | $0.013 | 33m11s | 4,139 |
| 5 | 2 | $40.27 | $20.14 | $0.006 | 38m06s | 6,286 |
| 6 | 2 | $34.24 | $17.12 | $0.009 | 40m53s | 3,835 |
| 7 | 1 | $24.08 | $24.08 | $0.008 | 38m28s | 3,167 |
| **Σ** | **68** | **$816.22** | **$12.00** | **$0.010** | **4h52m** | **78,966** |

**OpenSSL** — 47 units on the same rule, the 18 already wrapped by experiment 1 excluded:

| layer | types | $ | $/type | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|
| 0 | 17 | $159.81 | $9.40 | $0.011 | 42m44s | 14,908 |
| 1 | 11 | $108.56 | $9.87 | $0.009 | 33m37s | 12,545 |
| 2 | 6 | $86.96 | $14.49 | $0.007 | 49m08s | 12,495 |
| 3 | 4 | $41.50 | $10.38 | $0.008 | 30m08s | 5,308 |
| 4 | 2 | $26.65 | $13.32 | $0.007 | 37m38s | 3,835 |
| 5 | 1 | $11.51 | $11.51 | $0.006 | 24m25s | 1,860 |
| 6 | 1 | $13.71 | $13.71 | $0.005 | 28m40s | 2,709 |
| 7 | 1 | $12.34 | $12.34 | $0.008 | 27m33s | 1,498 |
| 8 | 1 | $9.98 | $9.98 | $0.006 | 24m15s | 1,740 |
| 9 | 1 | $10.04 | $10.04 | $0.007 | 23m13s | 1,509 |
| 10 | 1 | $8.52 | $8.52 | $0.007 | 20m35s | 1,252 |
| 11 | 1 | $13.90 | $13.90 | $0.006 | 26m50s | 2,337 |
| **Σ** | **47** | **$503.48** | **$10.71** | **$0.008** | **6h08m** | **61,996** |

At **$0.008 per line** against libgit2's $0.010, the two runs price a wrapper almost identically
across unrelated codebases, unrelated domains and closures of different shape. That is the number to
plan with.

### Unsafe surface

`crustify-cli audit` at the close of experiment 2 — a deterministic scan, no LLM:

| target | `unsafe` LoC | share of tree LoC | `unsafe` blocks | inside an `impl T` |
|---|---|---|---|---|
| libgit2 `src/` | 2,680 | 5.55% of 48,309 | 1,695 | 1,314 (77.5%) |
| OpenSSL `ssl/` | 2,531 | 7.39% of 34,235 | 1,859 | 1,739 (93.5%) |

An `impl T` reaches wrapped state through the type's own accessors, so that share is the part of the
`unsafe` surface confined to the one place the discipline sanctions.

### Tests

Wrapper agents write their own unit tests. They sit in per-module `#[cfg(test)]` blocks, so they are
measurable for the tree but not per type — a module holds every unit homed in one C source file, not
one:

| target | `#[test]` fns | test LoC | files with tests |
|---|---|---|---|
| libgit2 `src/` | 1,917 | 32,961 | 95 |
| OpenSSL `ssl/` | 1,368 | 22,446 | 84 |

Every unit lands only after `cargo check`, `cargo clippy` and `cargo test --workspace` pass over the
whole tree. A second visit to an already-wrapped unit runs as LLM-as-a-Judge review against the
agent-owned state on disk.
