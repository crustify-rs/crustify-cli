# Crustify

Crustify leverages LLM agents to (a) **migrate production C/C++ codebases to safe,
idiomatic Rust**, and (b) **generate safe Rust wrappers for unsafe APIs** - automatically,
incrementally and efficiently, with little human involvement.

Point it at a repo and it will map its build system and test suite, extract an exact dependency
graph of its types and symbols, and translate/wrap them in dependency order, focusing on maximizing
safety without sacrificing correctness. See the [Results](#results) section for our experiments
on translating **libgit2** and **OpenSSL**.


## Quick Setup

The following bootstraps the harness for a C-to-Rust port/wrap driven by an AI orchestrator. You can
spawn the orchestrator as an assistant in your favorite AI-based IDE or as a fully autonomous agent
in your terminal.

**1. Clone [`crustify-cli`](https://github.com/crustify-rs/crustify-cli).**

```bash
git clone https://github.com/crustify-rs/crustify-cli.git
```

**2. Clone [`ffibox`](https://github.com/crustify-rs/ffibox).** It carries the smart
pointers and lifetime traits used to emit safe FFI wrappers for C/C++/Rust interop.

```bash
git clone https://github.com/crustify-rs/ffibox.git
```

**3. Install an AI assistant** *(optional — skip if you already have one).*

```bash
curl -fsSL https://claude.ai/install.sh | bash        # Claude Code
curl -fsSL https://chatgpt.com/codex/install.sh | sh  # OpenAI Codex
```

**4. Spawn the orchestrator.** Render the orchestrator prompt and hand it to your assistant:

```bash
utils/build-orchestrator-prompt.sh <ffibox-checkout> -o orchestrator.md
```

It will first ask you to point it at the repo root and the target subsystem(s) you want to translate
— a subset of files, an entire `src/` directory, or the whole repo. Then it will wait for your
approval before starting work. Adjust to your liking / use case.


## Use Cases

Crustify helps you automate the following C-to-Rust tasks:

**Safe wrappers for C/C++/Rust interop.** Crustify can emit a safe wrapper interface over the public API
of a C/C++ library, and re-export it so that Rust-native consumers can integrate it without having
to use unsafe or raw pointers. If the target library is already written in Rust, or is in the
process of being migrated to Rust, Crustify can emit safe wrappers for its public API so that C/C++
consumers can integrate it without introducing undefined behavior.

**Incremental migration to Rust.** Crustify can also automate the migration of production C/C++
codebases to memory-safe, idiomatic Rust. It first decomposes the target in smaller units (symbols
and types), and translates them in dependency order, bottom-up. As some lower-layer Rust items may
still need to stay interoperable with the higher-layer C/C++ code (e.g. a god object storing a Rust
handle) — Crustify reuses the same approach of safe wrappers over FFI items, temporarily, and once
they no longer cross the FFI boundary it nativizes them.

**Partial migration to Rust.** Using the above principles, Crustify can also narrow its scope to
migrate only a subset of the subsystems, files, or types/symbols, keeping them interoperable with
what stays in C/C++.


## Why Crustify

LLM agents have become extremely powerful and versatile at every software engineering task. However,
they need the right guidance, the right unit of work, and the right tools to enable them to produce
high quality outputs. We tested bare LLMs on translating large, production codebases from the real
world (libgit2 and OpenSSL) and observed the following notable pitfalls:

- **Overthinking.** There are many ways to translate C/C++ to Rust, and there is no conventional playbook,
  causing LLMs to overthink and get lost in endless reasoning traces, which makes them very expensive and slow,
  especially on large codebases.

- **Early termination.** When faced with large codebases, bare LLMs cannot reliably decompose the task
  in subtasks and tackle all of them in a logical order. However, when tasked with smaller, finer-grained
  tasks we observed that LLMs produce higher quality outputs, faster, and cheaper.

- **Reward hacking.** When faced with complex type systems, pointers with rich ownership semantics,
  or FFI code, LLMs often tend to fall back to emitting `unsafe` blocks and _raw pointers_ in order
  to claim success, thus losing the safety benefits of Rust.

- **Inaccuracy.** When asked to analyze code (e.g. to find the users of a `struct` field), LLMs
  default to using grep and regex, which is, however, a notoriously inaccurate static analysis
  method that misses true sites (_false negatives_) and records false ones (_false positives_).

- **Non-determinism.** When re-running LLMs on translating the same function or struct they would
  often use a different coding convention and struct shape across runs. This is especially
  aggravated on translation tasks where an idiom in one language can be expressed in many ways in
  the other language (e.g. a C/C++ `struct` in Rust).

Crustify mitigates all these via: (a) a clear translation playbook and properly engineered prompts
that ensure LLMs don't derail from the task, (b) access to the right Rust primitives that facilitate code
with memory- and type-safety guarantees, free of UB (c) a deterministic dependency graph of types and symbols
to guide them through an incremental, bottom-up translation that enables safety-first coding, (d) a balanced
workload and task decomposition to keep them focused and enable parallel agent work, and (e) coding
conventions and structured specifications to enable deterministic outputs.


## Agent Harness

Crustify employs a simple agent harness consisting of **three LLM agents** that can run in parallel
to make the most out of available compute resources:

- A **type translator** specialized in `structs`, `enums` and `unions`;
- A **symbol translator** specialized in `functions`, `function pointers` and `global variables`;
- An **orchestrator** tasked with driving translators in dependency-order and ensuring the harness
  completes succesfully.

**Agent Backends.** Crustify integrates state-of-the-art agent frameworks to access frontier models
that are fine-tuned for coding tasks, both proprietary and open-weight, with built-in context management and
long-horizon execution. Additionally, it also employs the latest agentic techniques **adversatial testing**
and **auto-discovery** to improve output quality. 

To execute an agent, the  backend simply spawns a Python subprocess that invokes the provider's CLI in a shell session.
Crustify currently supports the following agent backends:

- [Claude Code CLI](https://code.claude.com/docs/en/quickstart) for Anthropic models
- [OpenAI Codex CLI](https://github.com/openai/codex) for OpenAI-compatible models

Billing modes: subscription-based (Claude Max, Codex Pro) or via API key (BYOK).


## The Semantic Oracle

Crustify enables LLM agents to analyze code with high precision by equipping them with a **_semantic
oracle_** - a CLI tool that leverages [CodeQL](https://codeql.github.com/) to statically analyze a
target repository and extract an exact dependency graph of types and symbols. We developed, tested,
and shipped the `.ql` queries that index the CodeQL database to compose the graph (see [utils](utils/codeql/)),
so agents do not have to re-invent them every time, which would re-introduce the very inaccuracy
issues mentioned above.

The semantic oracle provides the following capabilities:

1. It builds a **dependency graph** of the C/C++ items - types, functions, function pointers, global
   variables - by analyzing the AST at fine granularity, down to field-level access. It then applies
   Tarjan's algorithm to sort it in topological order, and turns it into a _DAG_ by flattening SCCs
   while keeping track of cut edges. This enables agents to tackle even the largest codebases by
   decomposing the task into smaller, finer-grained units.

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
to verify and modify the artifacts produced by the LLM between stages, facilitating debugging and
fine tuning for custom preferences.

### 1. Setup

This phase prepares the build environment, bootstraps the semantic oracle, and scaffolds an empty Rust
tree prior to any translation work. It can be driven entirely by the **orchestrator agent**. Below
is a simplified description of its stages, while the [playbook](docs/playbook.md), which the
orchestrator agent also follows, contains the full picture.

**Target discovery.** The orchestrator first identifies the build artifacts of the target
(executables, libraries) and its configure/build/test commands. Then, it configures and builds it,
runs the test suite to collect a baseline, and authors **`build.json`** where it documents its
findings. `build.json` will later drive the organization of the Rust tree in crates, and it will act
as a source of truth for downstream agents to obtain build/test commands.

**CodeQL extraction.** The orchestrator builds the CodeQL database and runs the `.ql` extraction
queries to generate `CSV` tables that store static information about code: argument/return/variable types,
field accesses, typedef aliasing, and more. The tables are then consumed by the oracle to build the dependency graph.

**Scope specification.** The orchestrator authors **`scope-config.json`** where it names the TUs and
headers of the target selected when bootstrapping the orchestrator agent. It names two file sets —
**`impl_files`** (the sources and private headers that implement the library) and **`api_headers`**
(the headers that publish its API) — and one verb, **`campaign_objective`**, which is what decides
how they are read:
Scope itself is the same either way: **targeted** is `impl_files` + `api_headers`, anchored on
definition sites (the library this campaign owns); **imported** is what that reaches and does not
own (its external dependencies); and the **api** view cuts across both, anchored on *declaration*
sites, carrying what the headers publish. `campaign_objective` decides only how deep the dependency
graph reads the library:
  - **`port`** - walk every targeted body; a struct defined anywhere in the targeted set keeps its
    full field layout.
  - **`wrap`** - walk no bodies at all, only signatures; only a struct defined in `api_headers`
    keeps its layout, everything else orders as an opaque handle.

A section says what the campaign contains; `campaign_objective` says how deeply to read it; what one
agent does with one selection is the translate stage's per-wave `--objective`.

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

Here's where translation work happens. The sections below give a high-level view of Crustify's
translation philosophy, while the complete workflow can be best understood from the prompts
of the [type](src/crustify/prompts/types.md) and [symbol](src/crustify/prompts/symbols.md) agents,
and the [principles](docs/principles.md) document.

**Agent Task.** In short, each translator agent is tasked with the following workflow:
  - Read [principles](docs/principles.md)
  - Use `crustify-oracle` to obtain the deps of your target set
  - Identify the `bindgen` bindings of your items; add if any missing
  - Analyze pointer ownership and type lifetimes and submit findings to `ownership-store.json` via
    `crustify-oracle`
  - Emit safe wrappers for import items / port to native Rust target items
  - Write unit tests in Rust, run the _safety audit_ pass (see below), fix issues
  - Re-export ported items, build and run original tests
  - Commit changes, merge in parent branch, fix conflicts, purge worktree once landed

**Batch Scheduling.** Crustify employs a deterministic scheduler that queries the oracle's DAG
to compose translation batches and routes them to the type/symbol agents. The
batching policy is governed by kind and DAG. First a batch is made of either types or symbols, never
both. Second, a single agent's batch either contains items from a single DAG layer, or from multiple
layers if their dep closure is also in the batch; lower layers get scheduled before higher ones.
Selection is section-blind, and every batch of a run carries the run's `--objective`.

**Workload Tuning.** Crustify also supports workload tuning: the symbol agent takes a batch of
symbols capped by a configurable max number of symbols and `LoC` (currently `50` and `1000`), while
the type agent gets a batch of types capped by a max number of types and a min number of fields
(currently `5` and `10`). Both have tunable CLI parameters. Agents with separate batches
run concurrently in **isolated worktrees** via a configurable concurrency threshold, i.e. max nr of
parallel agents. The orchestrator agent chooses how to best use the scheduler for driving
translation campaigns.

**FFI Interop.** Interoperability across the FFI boundary is the hard, historically manual half, and
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
references taken to a wrapped C type and field projections outside `impl` blocks. The translator agents are
instructed to use the audit pass to collect unsafe and potentially-UB sites, and to fix them unless
justified.

**LLM-as-a-Judge.** Each translator agent can act as a reviewer for an existing Rust translation or
for an ownership judgement submitted to `ownership-store.json`. Agents are instructed to enter this
mode via a simple paragraph in their prompt, and they are told to fix any mistakes if they find. The
orchestrator agent can run reviewer agents by running `crustify-cli translate --objective review`.

**Idempotency.** The scaffolder emits placeholder anchors (`// crustify:todo`) for every in-scope
item, and translator agents are asked to promote them for done work. This helps the
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
| `types` | enumerate · introspect · submit | `--fields` `--lifecycle-ops` `--users` `--field-touchers` `--manifest` `--api-only` `--in-tree` `--out-of-tree` |
| `symbols` | enumerate · introspect · submit · lifecycle discovery · call-graph closure | `--lifetime-for` `--taking` `--calling` `--callees` `--callers` `--depth` `--array` `--manifest` `--in-tree` `--out-of-tree` |
| `files` | the api headers / targeted set / imported closure | `--api-only` `--targeted-only` `--imported-only` |
| `dag` | closure · layer slice · flattened-cycle twins | `--name` `--layer` `--scc` `--depth` `--loc` `--full` |

`--name` filters, `--file` disambiguates a name defined in more than one place, `--targeted-only` /
`--imported-only` narrow any subject on the OWNERSHIP axis while `--api-only` cuts the independent
PUBLICATION axis (they intersect, so `--api-only --imported-only` is the re-export set), and
`--update` is the only writer. `--in-tree` / `--out-of-tree`
narrow an enumeration by whether an entry's home is inside the repository: `--imported-only
--out-of-tree` is the permanent FFI floor, `--imported-only --in-tree` the remaining port backlog.
`query dag --full` recomposes scope as if `campaign_objective` were `port`, so a `wrap` campaign can
read the body-deep graph without editing its config.

`query symbols --callees` / `--callers` walk the **raw use graph** the C wrote — codebase-wide,
unnarrowed, keyed `(name, defined_in)` — while `query dag --name` walks the **ordering graph**,
scope-narrowed and layered. `--depth` bounds both (default 1 = direct edges); cycles terminate.

`query <subject> --help` is the authority for what each flag means and for the record semantics
behind it; `--update-help` prints the findings schema `--update` expects, `--schema` the record's
own field definitions.

### `crustify-cli`

Four stages, run in this order the first time:

| stage | does | flags |
|---|---|---|
| `scaffold` | homes each C entity in the `.rs` that carries its `// Wraps:` / `// Replaces:` anchor, via `crates.json` | `--all` `--name` `--create` `--validate` `--file` `--dir` |
| `bindgen` | composes the `<lib>-sys` FFI crates, partitioning the import surface by owning crate | `--libs` `--reset` |
| `translate` | emits the wrappers, layer by layer, one agent per batch | `--name` `--file` `--dag-layer` `--transitive` `--skip` `--force` `--objective` `--max-syms` `--max-loc` `--max-types` `--min-fields` `--lifetime-for` `--dry-run` · `--model` `--billing` `--parallel` `--parallel-max` `--parallel-policy` `--override-base-prompt` `--no-override-base-prompt` `--no-console` `--no-file-log` |
| `audit` | scans the emitted tree for `unsafe`, raw pointers and naked `ffi::`, as JSON on stdout | `--all` `--name` `--crate` `--mod` `--file` `--dir` |

`translate` is the only stage that spawns agents; the other three are deterministic composers.

`--objective wrap\|port\|review` says what to do with a selection; a fourth, `raw`, is set by
`--lifetime-for` and selects the lifetime tier's discovery arm. `--transitive` expands each name
through its dependency closure, `--force` schedules items the selection would otherwise drop, and
`--max-syms`/`--max-loc` cap per-agent effort so a god object cannot blow one context. Start with
`--dry-run`: a high-layer seed pulls in a large closure.

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

Goal: pick three target types with a large field surface and wrap their entire transitive
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

**libgit2** — 68 units, port and wrap scope; the 7 import types experiment 1 already wrapped are
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


## Acknowledgements

This material is based upon work supported by the Defense Advanced Research Projects Agency (DARPA)
Translating All C To Rust (TRACTOR) program under Agreement No. HR00112590134.
