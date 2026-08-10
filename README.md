# Crustify

Crustify leverages LLM agents to automate the migration of production C/C++ codebases to safe,
idiomatic Rust, incrementally, efficiently, and with little human involvement.

Just point it at a repository and it will map its build system and test suite,
extract an exact dependency graph of its types and symbols, and translate them in dependency order,
focusing on maximizing safety without sacrificing correctness. 

## Value Proposition

LLM agents have become extremeley powerful at all software engineering tasks.
However, they need the right guidance, the right amount of work, and the right
tools to prevent them from hallucinating, reward hacking, and burning
unnecessary tokens. Additionally, there's a myriad of ways to express a software idiom across
programming languages (e.g. representing a C/C++ struct in Rust) - as software engineers, we need
tools (in this case LLMs) that emit reproducible, deterministic outputs, which are on par with the original specification /
behavior of the original software. We share more more about these pitfalls from real world experiments below.

Crustify provides all these in the form of (a) porperly enegineered prompts that ensure LLMs
don't derail from the task, (b) a deterministic dependency graph of types and symbols to guide
them through an incremental, bottom-up translation that enables safety-first coding, (c) a
balanced workload to keep them focused and enable them to work in parallel,
and coding conventions to make their output more deterministic. Moreover, Crustify points LLMs at the
right Rust primitives to generate code that uses the language's memory- and type-safety features.  

## Quick Setup

## Agent Harness

Crustify uses a simple agent harness consisting of just three LLM agents:
- a types-translator agent specialized in `structs`, `enums` and `unions` 
- a symbols-translator agent specialized in `functions`, `function pointers` and `global variables`
- an orchestrator agent tasked with driving the harness and housekeeping

**Agent Backends.** Crustify leverages state-of-the-art LLM agent frameworks to access frontier
models (both proprietary and open-source), providing context management, long-horizong execution,
fine-tuning for coding tasks. It simply installs the CLI from the model provider and runs it in
Python subprocesses, without shiping a custom backend that invokes the API, which would require
laborious maintanance.

Crustify currently supports the following agent backends:
- [Claude Code CLI](https://code.claude.com/docs/en/quickstart) for Anthropic models
- [OpenAI Codex CLI](https://github.com/openai/codex) for OpenAI-compatible models

**Translator Agents.** Each translator agent is tasked

**Orchestrator Agent.**

## Semantic Oracle

When asked to reason about code (e.g. find all touchers of a struct field), LLMs are naturally trained to use `grep`
and pattern matching, which however is an inaccurate static analyis method. Crustify counters that by shipping an accurate
_semantic oracle_ that leverages CodeQL to statically analyze the target repository and extract semantic properties of its elements.


## Translation Workflow

Crustify currently employs the following translation stages:
- 


## The two scopes

Crustify splits a target into two scopes, and the split drives everything else.

- **port scope** — code Rust will own. It gets rewritten as native Rust.
- **wrap scope** — the import closure port code reaches: types, functions and
  callbacks that stay in C and cross the FFI boundary. It gets **safe wrappers**.

Wrapping is the hard, historically manual half, and it is Crustify's current
focus. A wrapper lets a C type be used from Rust with no raw pointers, no
`unsafe` at the call site and no naked FFI — which puts the borrow checker back
in charge of ownership and lifetimes for an item the compiler could otherwise
say nothing about. Field access goes through generated accessors; lifecycle
(free/clone) becomes `Drop`/`Clone`.

## Pipeline

```
(you: toolchains, build.json, project build, CodeQL db)
  -> extract-ql -> scaffold -> bindgen -> translate
        query / audit  (read-only, anytime)
```

| stage | deterministic composer | LLM agents |
|---|---|---|
| **extract-ql** | T1/T2 CodeQL batches -> entity + edge tables; type/symbol records and the unified dependency DAG derive from them on demand | — |
| **scaffold** | `crates.json` -> `.rs` placement (query / create / validate) | — |
| **bindgen** | `<lib>-sys` crate skeletons, allowlists, include closure | — |
| **translate** | DAG-layered scheduler: scope filter, per-file/per-dep batching, budget slicing, per-wave worktree isolation | one wrapper agent per unit |
| **query / audit** | graph walks, record reads, unsafe-surface counts | — |

The composer never gates a unit on whether its deps are already emitted — the
C/FFI bridge keeps every intermediate state compiling — so a layer's units are
mutually independent and run concurrently. A wave costs its **longest** agent,
not the sum.

An agent's judgement (pointer facets, ownership, lifetime, locking) is written
back into the records via `query … --update`; the merge unions at field level,
so re-composing never clobbers it.

## Results

Two experiments, on [libgit2](git@github.com:crustify-rs/crustify-libgit2.git) (target `src`)
and [OpenSSL](git@github.com:crustify-rs/crustify-openssl.git) (target `ssl`, i.e. `libssl`).
Both ran `claude-opus-5`, one agent per unit, each in its own git worktree.

### 1. Wrap the FFI closure

Wrap every type and callback in the target's wrap closure, plus the wrap-scope
symbols the first port layers demand — the prerequisite for porting anything.

**libgit2 `src`** — the full wrap closure, 3 DAG layers.

| layer | units | $ | $/line | wall (longest agent) | lines |
|---|---|---|---|---|---|
| 0 | 69 | $459.89 | $0.015 | 1h12m | 30,340 |
| 1 | 9 | $68.43 | $0.009 | 29m11s | 7,636 |
| 2 | 1 | $8.95 | $0.007 | 19m22s | 1,365 |
| **Σ** | **79** | **$537.27** | **$0.014** | **2h01m** | **39,341** |

Layer 0 alone was 68 agents at 25.6x speedup over the serial sum. Only 22 of the
76 wrap types have a field any port function reads; the other 54 are opaque
handles, which is what makes the closure affordable.

**OpenSSL `ssl`** — 169 wrap types and 63 callbacks across 4 layers, of which 22
have a port-touched field. The run covered the 46 types carrying the port
surface, plus every wrap-scope symbol the first two port layers demand.

| | units | $ | wall | lines |
|---|---|---|---|---|
| types | 46 | $341.80 | 1h04m | 20,354 |
| symbols / callbacks | 58 | $60.62 | 2h01m | 7,359 |
| **Σ** | **104** | **$402.42** | **3h05m** | **27,713** |

**$1.01 and 123 lines per symbol, against $7.43 and 442 per type.** One symbol
wave bound 29 symbols in a single agent at $0.92 each — one shared idiom read
once, applied 29 times.

### 2. Transitively wrap a god object

Pick a port-scope type with a large field surface and wrap its entire transitive
dependency closure, bottom layer up. This is the shape a real port takes.

**libgit2** — three seeds of ≥25 declared fields: `git_indexer`,
`git_packbuilder`, `git_repository`.

| | closure | port | wrap | fields |
|---|---|---|---|---|
| `git_indexer` | 37 | 32 | 5 | 30 |
| `git_packbuilder` | 61 | 52 | 9 | 29 |
| `git_repository` | 45 | 39 | 6 | 29 |
| **union** | **75** | **66** | **9** | — |

143 members if the three were disjoint, 75 in the union: **the overlap saves 70
units.** The 66 port types are 10.2% of all 646 port-scope types in the target.

The closure is 8 layers deep. Layers 0–1 landed earlier; the run that closed the
remaining 28 units, all port-scope:

| layer | units | $ | $/agent | $/line | wall | lines |
|---|---|---|---|---|---|---|
| 2 | 12 | $150.21 | $12.52 | $0.013 | 38m31s | 11,585 |
| 3 | 7 | $91.31 | $13.04 | $0.009 | 31m44s | 10,065 |
| 4 | 4 | $54.23 | $13.56 | $0.013 | 33m11s | 4,139 |
| 5 | 2 | $40.27 | $20.14 | $0.006 | 38m06s | 6,286 |
| 6 | 2 | $34.24 | $17.12 | $0.009 | 40m53s | 3,835 |
| 7 | 1 | $24.08 | $24.08 | $0.008 | 38m28s | 3,167 |
| **Σ** | **28** | **$394.34** | **$14.08** | **$0.010** | **3h52m** | **39,077** |

Every declared field of all three seeds is wrapped: `git_indexer` 30/30,
`git_packbuilder` 29/29, `git_repository` 25/29 — the four without accessors are
the four no port-scope function reads. All four callbacks reached through
function-pointer fields are wrapped too, each by the type owning its field.

**Cost per agent rises with depth while cost per line falls.** Layer 4 paid
$13.56 an agent to wrap 16 fields across four small structs; layer 5 paid $20.14
to wrap 45. An agent's price is set by the reading its dependency stack demands
before it writes anything, not by the fields it emits — so once the stack beneath
it is correct, the marginal field is nearly free. The three deepest layers are
the three cheapest per line. Depth is the cost driver; field count is not.

**OpenSSL** — the same analysis over `record_layer_st`, `quic_stream_st` and
`ssl_session_st` (25/35/41 fields) gives a 62-member union closure 12 layers
deep, 36 port and 26 wrap; 36 port types is 9.0% of all 399. Its wrap-scope
members are covered by experiment 1; the port-scope tail is not yet translated.

### Unsafe surface

`crustify-cli audit` over the libgit2 tree at the close of experiment 2 — a
deterministic scan, no LLM:

- **1,606 `unsafe` blocks over 45,661 lines (5.6%)**, of which 1,314 — **81.8%**
  — sit inside an `impl T` that reaches wrapped state through its own accessors.
- **950 field projections go through accessors, against 2 that do not**, and
  both predate the run.
- **0 mutable borrows of a wrapped type**, the discipline's one flat
  prohibition.

Every unit lands only after `cargo check`, `cargo clippy` and `cargo test
--workspace` pass over the whole tree. Wrapper agents write their own unit tests;
a second visit to an already-wrapped unit runs as LLM-as-a-Judge review against
the agent-owned state on disk.

## Using it

Two binaries. The oracle is read-only and answers static questions about the C
codebase; the CLI runs the stages.

```
crustify-oracle <repo_root> <target> {extract-ql | query {types|symbols|files|dag}}

crustify-cli [globals] <repo_root> <target> {scaffold | bindgen | translate | audit}
```

| global | effect |
|---|---|
| `--model NAME` | override every agent's model, as `<provider>/<model>` |
| `--parallel` / `--parallel-max N` | concurrent agent chains (default 8) |
| `--parallel-policy P` | `per-agent` \| `serialize-per-file` \| `per-file` |
| `--billing subscription\|api` | how the provider CLI authenticates |
| `--no-console` / `--no-file-log` | quiet the live output / per-agent logs |

**translate** selects units with `--name N...`, `--dag-layer N`, or
`--transitive` to expand each name through its dependency closure; `--file`
disambiguates, `--wrap-only`/`--port-only` narrow by scope, `--skip` blocklists,
`--review` re-schedules already-wrapped units, and `--max-syms`/`--max-loc` cap
per-agent effort so a god object can't blow one context. Start with `--dry-run`:
a high-layer seed pulls in a large closure.

**audit** takes one seed selector (`--all`, `--name`, `--crate`, `--mod`,
`--dir`, `--file`); the search is always tree-wide.

Supported agent backends: Claude Code CLI, OpenAI Codex.

## Status

The wrapper half is the working half: everything above is emitted by it. The
port stage — rewriting port-scope code as native Rust behind those wrappers — is
next; its `// Replaces:` anchors are scaffolded but unfilled.
