# Retired prompts

Kept for reference. Nothing resolves a prompt from this directory: `CrustifyAgent`
looks in `prompts/<prompt_dir>/<stage>.md`, and every agent that used one of these
is deleted. Each entry describes what the prompt drove and what replaced it.

| prompt | drove | retired because |
|---|---|---|
| `scaffolder.md` | `CrustifyScaffolder` — placed entities into crates/modules/`.rs` | Placement moved to a hand-authored `crates.json` (`docs/schemas/crates.md`); `scaffold` is now a pure composer that reads it. |
| `bindgen.md` | `CrustifyBindgenShimmer` — macro shims + `cargo check` verify loop per `-sys` crate | `bindgen` is composer-only. It emits the per-kind allowlists and the include closure; the `fn main`, `-I` discovery and shims are completed in the crate itself, against a real compiler. |
| `alloc.md` | the `alloc` stage | Stage removed. The per-symbol lifetime model is snapshotted during `analyze` instead. |
| `buffer_analyzer.md` | the buffer-analyzer pass of `CrustifyTypeAnalyzer` | Pass removed. |
| `symbol_analyzer.md` | `CrustifySymbolAnalyzer` — filled `syms.json`'s LLM-owned fields (ptr facets, `lifetime`, ownership) ahead of wrapping | `analyze symbols` is composer-only. The schema's judgement fields are submitted by the WRAPPER agents through `query symbols --update`, at the point the symbol is actually wrapped — one pass over the surface instead of two, and the analysis is made where its consumer needs it. |
| `type_analyzer.md` | `CrustifyTypeAnalyzer` — same for `types.json` (per-field ownership, lifecycle, locking) | Same: `analyze types` is composer-only and the type wrapper submits the judgement fields via `query types --update`. |
| `strings_wrapper.md` | wrapping of synthetic string clusters | Synthetic clusters dropped as a concept across every stage. |
| `arrays_wrapper.md` | wrapping of synthetic array clusters | Same. |
| `build_propose.md` | proposing `build.json` — library partitioning, link topology, feature discovery, the configure/make invocation | No command or agent drives it. `crustify build` is superseded by `crustify <target> analyze extract-ql`, which reads a CodeQL database created by hand (`codeql database create --language=cpp --command=…`). `build.json` stays a hand-authored artifact. |
| `build_execute.md` | executing the proposed build to produce the CodeQL database | Same. |
| `merge.md` | `CrustifyMerge` — applied every parallel agent's `base..HEAD` into the main tree, resolved shared-file conflicts, ran the integrated validation, removed the worktrees | Integration is the worker agent's own job now: it commits in its worktree, rebases onto the session base branch and lands with `--ff-only`, retrying when a sibling got there first. Nothing applies into the user's checkout, and no stage tears a worktree down. |

These are stale against the current schemas — they reference fields and artifacts
that no longer exist (`crustify-bindgen.json`, `bindgen_extra.h`,
`non_opaque_types`, `const_macros`, the retired refined macro kinds, `linked_in`,
synthetic cluster kinds). Read them as a record of the approach, not as
instructions.
