# crustify-audit

Audit Rust repositories—especially wrappers over C—for unsafe surface area and
undefined behavior reachable from safe code.

| command | kind | result |
|---|---|---|
| `unsafe` | deterministic rustc analysis | unsafe and raw-pointer metrics |
| `ub` | agentic audit | investigated leads and reproducible advisories |

## Quick start

Requires Python 3.13 or newer:

```sh
pip install -e .

crustify-audit /path/to/repo unsafe
crustify-audit /path/to/repo ub --model anthropic/claude-opus-5
```

Pass the repository root. The audited Cargo workspace is the root when it has a
`Cargo.toml`; otherwise it is `crustify/rust/` for a Crustify campaign.

`unsafe` requires a nightly toolchain with `rustc-dev` and `llvm-tools`, plus a
workspace that compiles. `ub` requires the selected Claude or Codex CLI and the
instrumentation appropriate to each finding. It warns when Miri or
BorrowSanitizer is unavailable.

## Output

Every run uses `<repo>/crustify/audit/`:

```text
unsafe.json        deterministic scan output
advisories/        confirmed bugs and their reproducers
leads/             every investigated candidate, including cleared ones
scratch/           disposable experiments
logs/              agent logs and usage records
```

An advisory requires a safe reproducer that depends on the audited crate, calls
its public API without writing `unsafe`, and triggers one of the selected
instruments. Anything short of that is a lead — and a `revisit` run exists to
settle leads once an instrument that can decide them is available.

Runs accumulate. Auditors read existing advisories and leads before starting,
so later runs extend the record instead of repeating completed investigations.

## CLI

```text
crustify-audit REPO unsafe [--json] [--name NAME ...]
crustify-audit REPO ub [--objective audit|audit+patch|patch|revisit]
                       [--workset PATH ...]
                       [--instruments miri|asan/ubsan|bsan|msan|tsan ...]
                       [--model PROVIDER/MODEL]
                       [--billing subscription|api]
                       [--timeout MINUTES]
```

- `unsafe --json` prints the document written to `unsafe.json`.
- `unsafe --name` adds source sites for selected C types or symbols.
- `ub --workset` confines an auditor to specified work items. For audit
  objectives these are source files; omit it for the whole crate. Under
  `--objective patch` it carries advisory directories under
  `crustify/audit/advisories/` instead of source files; omit it to repair every
  advisory. Under `--objective revisit` it carries lead notes under
  `crustify/audit/leads/` instead of source files.
- `ub --instruments` constrains the hunt and advisory evidence; omit it to
  select all five. Before spending, the command prints the exact selected
  instruments, their bug classes, and their reach limitations.
- `ub --timeout` is a wall-clock budget, not a kill deadline. The current agent
  finishes even when that overshoots the budget; `0` runs one agent.
- `audit` never edits target source. `audit+patch` and `patch` develop repairs
  in Git worktrees. `revisit` hunts nothing new: it re-investigates leads an
  earlier campaign left open, appends a dated verdict to each, and promotes one
  to an advisory if it now reproduces. Use it after adding an instrument that
  can settle a hypothesis the earlier run had to leave standing.

Run `crustify-audit --help` or a subcommand's `--help` for complete flag
semantics.

### Instrument scopes

| selection | bug classes the auditor hunts |
|---|---|
| `miri` | Rust-side bounds and lifetime errors, uninitialized or invalid values, alignment and intrinsic violations, aliasing under Stacked/Tree Borrows, and data races |
| `asan/ubsan` | native bounds errors, use-after-free/return/scope and invalid frees, pointer/alignment UB, integer/division/shift UB, and invalid C/C++ runtime values |
| `bsan` | Tree Borrows aliasing across Rust and foreign code, including conflicting foreign-pointer writes and pointers invalidated by reborrows |
| `msan` | use of uninitialized memory: branches and addresses computed from it, uninitialized bytes crossing the FFI boundary, and struct tails or buffers a foreign initializer left partly unwritten |
| `tsan` | data races between Rust and foreign threads, unsynchronized access through `&T` where `Send`/`Sync` is hand-written, and use of an object being destroyed on another thread |

These are execution-based scopes, not promises of exhaustive detection. Miri
usually cannot execute foreign code; ASan/UBSan require the relevant native
code and final executable to be instrumented; BorrowSanitizer specifically
checks Rust aliasing rules. The same definitions are injected into each auditor
prompt, so CLI selection, displayed plan, and hunt scope cannot drift.

MemorySanitizer and ThreadSanitizer each need a build of their own and cannot
share a binary with ASan/UBSan, so selecting them costs an extra build per
auditor. MemorySanitizer additionally needs every component instrumented — the
standard library via `-Zbuild-std` and the foreign code it links — because
memory written by uninstrumented code reads as uninitialized; a partial build
reports falsely rather than cleanly. They cover the two classes the other three
cannot reach at all: ASan/UBSan does not model uninitialized memory, and no
other instrument can decide a hand-written `unsafe impl Sync`.

## Container

The container starts an orchestrator that resolves the run plan and launches
auditors against the checkout mounted at `/target`.

```sh
docker build -t crustify-audit -f examples/crustify_audit/Dockerfile .

docker run --rm -it --name audit-target \
  -e ANTHROPIC_API_KEY \
  -e CRUSTIFY_BACKEND=claude \
  -e CRUSTIFY_MODEL=claude-opus-5 \
  -v /path/to/target-repo:/target \
  -v /host/campaign/TASK.md:/campaign/TASK.md:ro \
  -v audit-target-work:/work \
  crustify-audit
```

The harness is installed in the image. During harness development, mount this
checkout at `/opt/crustify-audit` to run its live sources instead.

For Codex against OpenAI, use `CRUSTIFY_BACKEND=codex`, pass the model ID
exactly as Codex expects it, and provide `OPENAI_API_KEY`. To drive an
Anthropic model through OpenRouter instead, add:

```sh
-e OPENROUTER_API_KEY \
-e CRUSTIFY_BACKEND=codex \
-e CRUSTIFY_PROVIDER=openrouter \
-e CRUSTIFY_MODEL=anthropic/claude-sonnet-5
```

OpenRouter must support the Responses API for the selected model. Model IDs are
passed verbatim to both CLIs.

Before running, complete [`examples/crustify_audit/TASK.md.template`](examples/crustify_audit/TASK.md.template) and place it at
the host path mounted as `/campaign/TASK.md` above. Without that mount, the
orchestrator asks for mandatory decisions. A headless run therefore needs a
complete task.

| variable | values | default |
|---|---|---|
| `CRUSTIFY_BACKEND` | `claude`, `codex` | `claude` |
| `CRUSTIFY_PROVIDER` | `anthropic`, `openai`, `openrouter` | derived from backend |
| `CRUSTIFY_MODEL` | backend-specific model ID | `claude-opus-5` |
| `CRUSTIFY_BILLING` | `api`, `subscription` | `api` |
| `CRUSTIFY_HEADLESS` | `0`, `1` | `0` |
| `CRUSTIFY_TIMEOUT` | minutes per auditor; `0` runs one | `60` |
| `CRUSTIFY_EFFORT` | Codex orchestrator and auditor reasoning effort | `high` |
| `CRUSTIFY_VERB` | `orchestrate`, `unsafe` | `orchestrate` |

`api` uses `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OPENROUTER_API_KEY` for
the selected provider. OpenRouter requires `api`; `subscription` uses Claude or
Codex credentials saved under `/work`, which is also the persistent Cargo/build
cache. Set `CRUSTIFY_VERB=unsafe` to run only the deterministic scan, without
an agent or authentication.

## Reference

- [Deterministic output and named-site semantics](docs/unsafe-output.md)
- [Task questionnaire](examples/crustify_audit/TASK.md.template)
- [Example results and report format](examples/crustify_audit/results.md)
- [`ub` auditor prompt](src/crustify_audit/prompts/ub.md)
- [Orchestrator prompt](src/crustify_audit/prompts/orchestrator.md)
