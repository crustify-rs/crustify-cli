---
name: crustify-inspect
roles: [translator, orchestrator]
description: >-
  Discover what you need before porting/wrapping a C symbol or type: its record
  and signature, its already-emitted Rust module, its dependency closure, and
  the unsafe surface of ported code. Use the read-only crustify commands
  `query` (syms / types / dag / files / --schema), `scaffold --name` (locate a
  module), and `audit` (scan the ported tree). Read this when you need to look
  something up — not to mutate state.
---

# Inspecting the crustify model (`query` / `scaffold` / `audit`)

Read-only discovery. These never mutate the tree — reach for them freely. For
**exact flags and arguments, run the command's `--help`** (argparse is the
source of truth and never drifts); this skill is the *router*: which command
for which intent, and the idioms `--help` can't tell you.

> Invocation shape: `crustify <repo_root> <target> <command> …`. Global flags
> (`--model`, `--parallel`) go **before** `<repo_root>`; a stage's own flags
> (e.g. `--parallel-max`) go **after** the subcommand. Use the target that owns
> a `scope.json` (e.g. `src/libgit2`), not `_root`, for scope-aware commands.

## Intent → command

| You need… | Command |
|-----------|---------|
| a symbol's record (signature, pointer analysis, type/sym deps) | `query syms --name <name> --with-details` |
| a type's record (you **call** its wrapper, don't port it) | `query types --name <tag> --with-details` |
| where an element's Rust module lives (placeholder anchor, or an already-wrapped type's module) | `scaffold --name <name>` |
| a symbol/type's **dependency closure** (what's already emitted; call its safe API, never raw `ffi::`) | `query dag --name <X> [--depth 1] [--with-details]` |
| the synthetic **array** families (ownership-transfer of pointer-to-array) | `query types --arrays --with-details` |
| the synthetic **string** families and what they instantiate to | `query types --strings --with-details` |
| field/record **schema** definitions | `query --schema <kind>` |
| the unsafe / raw-ptr / naked-`ffi::` surface of ported code | `audit [--name <seed>]` |

## Idioms `--help` won't tell you

- **`query` is an enumerate-or-introspect oracle**: no `--name` → list (filtered)
  entries; `--name X` → introspect one; several names → several records.
- **Enumerate → xargs a stage**: `query types --wrap-only | xargs … wrap --name`.
- **`dag` closure vs scope**: `query dag --name X` is X's transitive *deps*
  (emitted before it); `--depth 1` = direct deps only. A WRAP-scope node's
  `depends_on.syms` is empty by design — read the closure, don't infer from it.
- **`audit` is deterministic (no LLM)**: per-seed unsafe surface + a tree-wide
  `global` section + `totals`; printed to stdout, nothing written.

## Rule

Whatever `query`/`scaffold` surface as already emitted, **call its safe Rust
API — never raw `ffi::`**. If you find yourself reaching for `ffi::`, re-check
whether a wrapper already exists (`scaffold --name`).

<!-- TODO: expand per-command recipes if/when they outgrow `--help`; keep
     mechanics in `--help`, idioms here. -->
