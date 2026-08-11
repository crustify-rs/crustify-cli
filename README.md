# Crustify

Crustify leverages LLM agents to automate the migration of production C/C++
codebases to **safe, idiomatic Rust**, incrementally and efficiently, with little
human involvement.

Point it at a repo and it will map its build system and test suite, extract an
exact dependency graph of its types and symbols, and translate them in
dependency order, focusing on maximizing safety without sacrificing
correctness.


## Why Crustify

LLM agents have become extremely powerful at every software engineering
task. However, they need the right guidance, the right unit of work, and the
right tools to prevent them from hallucinating, reward hacking, and burning
unnecessary tokens. Additionally, the inherent non-deterministic nature of LLMs leads
them to produce different outputs for the same inputs, which is especially aggravated
in the world of cross-language transpilation where one idiom can be expressed in multiple different
ways across languages (e.g. a C/C++ struct in Rust). Thus, it is crucial to
point LLMs at the right conventions and expose them as much as possible to structured
specificaitons so they produce more deterministic and reproducible outputs.

Crustify provides all of these: (a) properly engineered prompts that ensure
LLMs don't derail from the task, (b) a deterministic dependency graph of types
and symbols to guide them through an incremental, bottom-up translation that
enables safety-first coding, (c) a balanced workload to keep them focused and
enable parallel agent execution, (d) coding conventions and structured specifications
to enable more deterministic outputs. Moreover, Crustify points LLMs at the right
Rust primitives enabling them to emit code that carries memory- and type-safety
guarantees instead of unsafe blocks and raw pointers.


## Quick Setup

The following bootstraps the harness for a C-to-Rust port driven by an AI
orchestrator. You can spawn the orchestrator as an assistant in your
favorite AI-based IDE or as a fully autonomous agent in your terminal.

**1. Clone [`crustify-cli`](https://github.com/crustify-rs/crustify-cli).**

```bash
git clone https://github.com/crustify-rs/crustify-cli.git
```

**2. Clone [`crustify-prim`](https://github.com/crustify-rs/crustify-prim).**
It carries the smart pointers and lifetime traits every emitted wrapper is
built from.

```bash
git clone https://github.com/crustify-rs/crustify-prim.git
```

**3. Install an AI assistant** *(optional — skip if you already have one).*
Any assistant that reads files and runs shell commands will do:

```bash
curl -fsSL https://claude.ai/install.sh | bash        # Claude Code
curl -fsSL https://chatgpt.com/codex/install.sh | sh  # OpenAI Codex
```

**4. Spawn the orchestrator.** Render the orchestrator prompt and hand it to your
preferred AI assistant:

```bash
utils/build-orchestrator-prompt.sh <crustify-prim-checkout> -o orchestrator.md
```

It will first ask you to point it at the repo root and the target subsystem(s)
you want to translate — a subset of files, an entire `src/` directory, or the
whole repo. Then it will wait for your go before starting work. Adjust to your
liking / use case.


## What can it do for you

Crustify can help you automate the following C-to-Rust tasks:

**Safe wrappers for C/Rust interop.** Crustify can emit a safe wrapper interface over the public
  API of a C/C++ library, and re-export it so that Rust-native consumers can integrate it without
  having to use unsafe or raw pointers. Moreover, if the library is already written in Rust, or is in the process
  of being migrated to Rust, Crustify can emit safe wrappers for its public API so that C/C++ consumers can
  integrate it without introducing undefined behavior hazards.

**Incremental migration to Rust.** Crustify can also help you automate
  the migration of production C/C++ codebases to memory-safe, idiomatic Rust. It first decomposes
  the target in smaller units, i.e. symbols and types, and then translates them in dependency order, bottom-up.
  As each lower-layer items become migrated to Rust, some may still needs to stay interoperable with the higher
  C/C++ layers---Crustify reuses the same principle of temporarily wrapping FFI items
  with safe Rust abstractions until they do not cross the FFI boundary anymore and can be nativized.

**Partial migration to Rust.** Using the above principles, Crustify can also narrow its scope to
  migrate to Rust only a subset of the subsystems, files, or types, keeping them interoperable with
  what stays in C/C++.  


## Agent Harness

Crustify uses a simple agent harness consisting of **three LLM agents**:

- A **type translator** specialized in `structs`, `enums` and `unions`;
- A **symbol translator** specialized in `functions`, `function pointers` and
  `global variables`;
- An **orchestrator** tasked with driving the harness and ensuring translation
  waves land correctly.

**Agent Backends.** Crustify integrates state-of-the-art agent frameworks to
access frontier models, both proprietary and open-source, with built-in context
management, long-horizon execution, and fine-tuning for coding tasks. The
backend simply spawns Python subprocesses that invoke the provider's CLI in a
shell session.

Crustify currently supports the following agent backends:

- [Claude Code CLI](https://code.claude.com/docs/en/quickstart) for Anthropic
  models
- [OpenAI Codex CLI](https://github.com/openai/codex) for OpenAI-compatible
  models

Billing modes: subscription-based (Claude Max, Codex Pro) or via API key (BYOK).


## The Semantic Oracle

When asked to analyze code (e.g. to find all touchers of a `struct` field),
LLMs default to `grep` and regex-matching over source text. This is, however, a
notoriously inaccurate static analysis method that may miss true sites (false
negatives) or record false ones (false positives). In our experiments we
observed LLMs falling into both.

Crustify mitigates this by equipping LLM agents with a _semantic oracle_ that
leverages [CodeQL](https://codeql.github.com/) queries to statically analyze
the target repository and extract semantic properties of its types and symbols.
The queries are tracked in the repo and verified, so agents do not have to
invent them on the fly every time — which would re-introduce the very accuracy
issues above.

The semantic oracle has three primary jobs:

1. It builds a **dependency graph** of the C/C++ items — types, functions,
   function pointers, global variables — by analyzing the AST at fine
   granularity, down to field-level access. It then applies Tarjan's algorithm
   to sort it into topological order, flattening SCCs while keeping track of
   fallback edges.

2. It maintains an **`ownership-store.json`** where agents can submit ownership
   and borrow properties of C/C++ pointers, as well as lifetime primitives of
   types — both of which are ambiguous to static and dynamic analysis alike.

3. It ships as a **CLI binary** that translator agents query on demand to learn
   these facets about the type or symbol they are processing.


## How it works

Crustify is a pipeline consisting of two phases, each with its own stages and
structured I/O artifacts, where work is split between **deterministic composers** for
mechanical tasks and **LLMs** for semantic reasoning and codegen. The composers are
implemented in Python and packaged as two CLI binaries that can be driven by
LLMs and humans alike. A human is free to verify and modify the artifacts produced by the LLM
between stages, which can help find ptifalls and fine tune the outputs of the downstream stages.

### 1. Setup

This phase prepares the build environment, generates the semantic oracle, and scaffolds the Rust
tree prior to any translation work. It consists of the following stages and can be driven entirely
by the **orchestrator agent**.

**Target discovery.** The orchestrator first configures and builds the target codebase, and
runs the test suite to collect a baseline. It then authors **`build.json`** where it lists
the build artifacts of the target (executables, shared libraries), their TU and header sources,
feature flags that enable/disable features at build-time, and the configure/build/clean/test commands.
`build.json` will then drive the organization of the Rust tree in crates, and it will act as a
source of truth for downstream agents for obtaining the commands configuring/building/testing the
system.

**CodeQL extraction.** The orchestrator builds the CodeQL database and runs the `.ql` extraction
  queries to generate `CSV` tables that store static information about code items: argument/return/variable
  types, field accesses, typedef aliasing, and more. The tables are then consumed by the oracle to
  build the dependency graph.

**Scope specification.** The orchestrator authors **`scope-config.json`** where it lists all the
  TUs and headers that form the port target selected when bootstrapping the orchestrator agent.
  Crustify splits a target into two scopes, and the split drives everything else:
  - **port scope** - code that Rust will own. It gets rewritten as native Rust.
  - **wrap scope** - the import closure port code reaches: types, functions and
    callbacks that stay in C and cross the FFI boundary. It gets **safe wrappers**.

**Crates specification.** The orchestrator authors **`crates.json`** where it sketches a hierarchy of
  Rust crates, modules, and `.rs` source files that govern how the Rust tree will be organized. It
  then fetches each port- and wrap-scope item using the oracle, and homes it in a `.rs` source.
  The organization is faithful to the build artefact structure listed in `build.json` for the top-level
  crates while modules and `.rs` files mirror the directory structure of the original tree to the extend
  possible (e.g. Rust does not have `.h` headers, just TUs). The orchestrator then runs the deterministic
  composer to scaffold a skeleton Rust tree on disk based on the `crates.json` specification. Every crate
  has a companion `-sys` which will host the bindings generated via `bindgen` in the next stage. Users may
  adjust `crates.json` to their liking for a custom Rust-tree organization.

**FFI bindings.** The orchestrator emits FFI bindings via `bindgen` by first running a mechanic composer that
  emits an incomplete `build.rs` for every `-sys` crate, allowlisting the C/C++ items that need a Rust binding.
  The allowlists are composed mechanically based on the dependency relations fetched from the oracle. The
  orchestrator is then tasked to complete each `build.rs` driver, build it, and ensure all required bindings
  are properly emitted.

### 2. Translation

Here's where translation work happens.

**FFI interoperability.** Interoperability across the FFI boundary is the hard, historically
  manual half, and is Crustify's current focus. A wrapper lets a C type be used from Rust with no raw pointers, no
  `unsafe` at the call site and no naked FFI — which puts the borrow checker back
  in charge of ownership and lifetimes for an item the compiler could otherwise
  say nothing about. Field access goes through generated accessors; lifecycle
  (free/clone) becomes `Drop`/`Clone`.

**Translation to native Rust.**


## CLI

Two binaries. The oracle answers static questions about the C
codebase and maintains an ownership store where agents can submit their
judgement on pointer ownership and lifetimes; the CLI runs the stages.

```bash
crustify-oracle <repo_root> <target> {extract-ql | query {types|symbols|files|dag}}

crustify-cli [globals] <repo_root> <target> {scaffold | bindgen | translate | audit}
```

**`crustify-oracle`.** `extract-ql` runs the T1/T2 `.ql` batches against the
CodeQL database and writes one CSV per query — the only oracle command with
side effects, and the only one you run explicitly; every view below derives
from those tables on demand. `query` is read-only: `types`/`symbols` enumerate
or introspect one record, `files` lists the port / wrap scope file sets, `dag`
does the graph walks (closure, layer, scc). Agents write their ownership and
lifetime judgement back with `query … --update`; the merge unions at field
level, so re-composing the records never clobbers it.

The oracle is also shipped as a [`SKILL.md`](skills/crustify-oracle/SKILL.md),
loaded by both translator agents and the orchestrator — so an agent reaches for
it instead of `grep` without being told to on every task.

**`crustify-cli`.**

| global | effect |
|---|---|
| `--model NAME` | override every agent's model, as `<provider>/<model>` |
| `--parallel` / `--parallel-max N` | concurrent agent chains (default 8) |
| `--parallel-policy P` | `per-agent` \| `serialize-per-file` \| `per-file` |
| `--billing subscription\|api` | how the provider CLI authenticates |
| `--no-console` / `--no-file-log` | quiet the live output / per-agent logs |

**scaffold** resolves a C type or symbol to the `.rs` module homing its
`// Wraps:` / `// Replaces:` anchor, through `crates.json`. Deterministic, no
LLM: the placement oracle is authored outside the stage, so an unplaced
selection is a hard error rather than a guess. `--all` materializes the whole
in-scope tree; `--name` answers "where does this live?" and `--create` writes
the stub; `--validate` runs the consistency gate — every entity homed in
exactly one `.rs`, crate `depends_on` acyclic. A name with several homes is
refused rather than picked between: pass `--file` to say which.

**bindgen** scaffolds the `<lib>-sys` FFI crates, partitioning the wrap-scope
surface by owning crate. Also deterministic. The crates come out deliberately
incomplete — `build.rs` carries the per-kind allowlists but no `fn main`, and
`bindgen.h`'s shim block is empty — because finishing them needs a compiler in
the loop. `--reset` recomputes composer-owned state from scratch instead of
accumulating onto it, so an entity that left the scope leaves the allowlist; it
never touches the agent-owned blocks.

**translate** selects units with `--name N...`, `--dag-layer N`, or
`--transitive` to expand each name through its dependency closure; `--file`
disambiguates, `--skip` blocklists, `--objective wrap|port|review` says what to
do with the selection (`port` and `review` also bypass the already-done gate),
and `--max-syms`/`--max-loc` cap per-agent effort so a god object can't blow
one context. Start with `--dry-run`: a high-layer seed pulls in a large
closure.

The scheduler never gates a unit on whether its deps are already emitted — the
C/FFI bridge keeps every intermediate state compiling — so a layer's units are
mutually independent and run concurrently. A wave costs its **longest** agent,
not the sum.

**audit** is the deterministic counter-check on what the agents emitted: no
LLM, nothing written to disk, JSON to stdout. It takes one seed selector
(`--all`, `--name`, `--crate`, `--mod`, `--dir`, `--file`), but the scan is
always tree-wide. Each seed reports its own `unsafe`, raw-pointer and naked
`ffi::` surface; a `global` section adds outside-`impl` raw pointers, the
`ffi::` type-surface partition and a `c_void` filter, then `totals`. The
numbers under [Unsafe surface](#unsafe-surface) are one such run.

---


## Results

Two experiments, on
[libgit2](https://github.com/crustify-rs/crustify-libgit2) (target `src`) and
[OpenSSL](https://github.com/crustify-rs/crustify-openssl) (target `ssl`, i.e.
`libssl`). Both ran `claude-opus-5`, one agent per unit, each in its own git
worktree.

### 1. Wrap the FFI closure

Wrap every type and callback in the target's wrap closure, plus the wrap-scope
symbols the first port layers demand — the prerequisite for porting anything.

**libgit2 `src`** — the full wrap closure, 3 DAG layers.

| layer | types | symbols | units | $ | $/type | $/symbol | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 67 | 2 | 69 | $466.55 | $6.86 | $3.33 | $0.015 | 1h12m | 30,980 |
| 1 | 8 | 1 | 9 | $75.24 | $8.55 | $6.81 | $0.009 | 29m11s | 8,235 |
| 2 | 1 | 0 | 1 | $8.95 | $8.95 | — | $0.007 | 19m22s | 1,365 |
| **Σ** | **76** | **3** | **79** | **$550.74** | **$7.07** | **$4.49** | **$0.014** | **2h01m** | **40,580** |

Layer 0 alone was 68 agents at 29.2x speedup over the serial sum. Only 22 of
the 76 wrap types have a field any port function reads; the other 54 are opaque
handles, which is what makes the closure affordable.

**OpenSSL `ssl`** — 169 wrap types and 63 callbacks across 4 layers, of which
22 have a port-touched field. The run covered the 46 types carrying the port
surface, plus every wrap-scope symbol the first two port layers demand.

| | types | symbols | units | $ | $/type | $/symbol | $/line | wall | lines |
|---|---|---|---|---|---|---|---|---|---|
| types | 46 | — | 46 | $341.80 | $7.43 | — | $0.017 | 1h04m | 20,354 |
| symbols / callbacks | — | 60 | 60 | $60.62 | — | $1.01 | $0.008 | 2h01m | 7,359 |
| **Σ** | **46** | **60** | **106** | **$402.42** | **$7.43** | **$1.01** | **$0.015** | **3h05m** | **27,713** |

Symbol counts are units *wrapped*; 58 were scheduled, and two batches wrapped
one extra each. Callbacks are counted as symbols throughout.

**$1.01 and 123 lines per symbol, against $7.43 and 442 per type.** One symbol
wave bound 29 symbols in a single agent at $0.92 each — one shared idiom read
once, applied 29 times.

### 2. Transitively wrap a god object

Pick a port-scope type with a large field surface and wrap its entire
transitive dependency closure, bottom layer up. This is the shape a real port
takes.

Both targets were run with three seeds of ≥25 declared fields. Every unit in
both closures is emitted and promoted.

| | libgit2 `src` | OpenSSL `ssl` |
|---|---|---|
| seeds | `git_indexer` · `git_packbuilder` · `git_repository` | `record_layer_st` · `quic_stream_st` · `ssl_session_st` |
| seed fields | 30 · 29 · 29 | 25 · 35 · 41 |
| units (incl. seeds) | 75 | 65 |
| port / wrap | 66 / 9 | 39 / 26 |
| depth | 8 layers | 12 layers |
| if seeds were disjoint | 143 → 73, **overlap saves 70** | 73 → 62, **overlap saves 11** |
| share of port scope | 10.2% of 646 types | 9.0% of 399 types |

**The same experiment reads differently on the two codebases.** libgit2's god
objects sit on shared infrastructure — `git_str`, `git_vector`, `git_oid` — so
three closures collapse into one and half the work is paid once. OpenSSL's sit
on a layered protocol stack: barely any overlap, and a chain 12 deep where
`quic_stream_st` reaches through eleven layers to its leaves. Overlap is what
makes seed selection pay off, and it is a property of the codebase, not of the
tool.

**libgit2** — layers 0–1 landed earlier; the run that closed the remaining 28
units, all port-scope:

| layer | types | symbols | units | $ | $/type | $/symbol | $/line | wall | lines |
|---|---|---|---|---|---|---|---|---|---|
| 2 | 12 | 0 | 12 | $150.21 | $12.52 | — | $0.013 | 38m31s | 11,585 |
| 3 | 7 | 0 | 7 | $91.31 | $13.04 | — | $0.009 | 31m44s | 10,065 |
| 4 | 4 | 0 | 4 | $54.23 | $13.56 | — | $0.013 | 33m11s | 4,139 |
| 5 | 2 | 0 | 2 | $40.27 | $20.14 | — | $0.006 | 38m06s | 6,286 |
| 6 | 2 | 0 | 2 | $34.24 | $17.12 | — | $0.009 | 40m53s | 3,835 |
| 7 | 1 | 0 | 1 | $24.08 | $24.08 | — | $0.008 | 38m28s | 3,167 |
| **Σ** | **28** | **0** | **28** | **$394.34** | **$14.08** | **—** | **$0.010** | **3h41m** | **39,077** |

A god-object closure is all types: the symbols its members touch are reached
through the wrapped type, not scheduled beside it. Both closures below are
likewise 100% types.

Every declared field of all three seeds is wrapped: `git_indexer` 30/30,
`git_packbuilder` 29/29, `git_repository` 25/29 — the four without accessors
are the four no port-scope function reads. All four callbacks reached through
function-pointer fields are wrapped too, each by the type owning its field.

**Cost per agent rises with depth while cost per line falls.** Layer 4 paid
$13.56 an agent to wrap 16 fields across four small structs; layer 5 paid
$20.14 to wrap 45. An agent's price is set by the reading its dependency stack
demands before it writes anything, not by the fields it emits — so once the
stack beneath it is correct, the marginal field is nearly free. The three
deepest layers are the three cheapest per line. Depth is the cost driver; field
count is not.

**OpenSSL** — all 65 units carry a promoted anchor: **$643.98**, **70,910**
lines, **447** distinct fields given accessors. Median **$9.14** per unit, max
$25.62 (`ossl_record_layer_st`, 61 port-touched fields). Most of the closure
landed in a single wave — **6h09m, 52 batches, 0 failures.**

At **$0.009 per line** against libgit2's $0.010, the two runs price a wrapper
almost identically across unrelated codebases, unrelated domains and closures
of different shape. That is the number to plan with.

### Unsafe surface

`crustify-cli audit` over the libgit2 tree at the close of experiment 2 — a
deterministic scan, no LLM:

- **1,606 `unsafe` blocks over 45,661 lines (5.6%)**, of which 1,314 — **81.8%**
  — sit inside an `impl T` that reaches wrapped state through its own
  accessors.
- **950 field projections go through accessors, against 2 that do not**, and
  both predate the run.
- **0 mutable borrows of a wrapped type**, the discipline's one flat
  prohibition.

Every unit lands only after `cargo check`, `cargo clippy` and `cargo test
--workspace` pass over the whole tree. Wrapper agents write their own unit
tests; a second visit to an already-wrapped unit runs as LLM-as-a-Judge review
against the agent-owned state on disk.
