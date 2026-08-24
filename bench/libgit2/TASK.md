---

# Campaign

- repository: `https://github.com/libgit2/libgit2.git`
- revision: `ddf3b5c85d86a389330b1d1dd90f08f60ae05fe4`
- objective: `wrap`

# Sub-campaigns

## `import-type-closure`

- subsystem: imported types and callbacks needed by `src/`
- implementation-paths: derive from the oracle's imported section
- api-headers: derive from the imported declarations
- coverage: whole imported type and callback closure
- named-items: all imported types and callbacks, dependency order preserved
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `import-symbols-l0-l2`

- subsystem: imported functions and globals needed by target layers L0 through L2
- implementation-paths: derive from the oracle's imported section
- api-headers: derive from the imported declarations
- coverage: named subset reached by target code at layers L0 through L2
- named-items: compute from the target closure and its imported symbol dependencies
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `god-objects`

- subsystem: large target types in `src/`
- implementation-paths: `src/`
- api-headers: derive from the selected declarations
- coverage: named subset with transitive closure
- named-items: `git_indexer`, `git_packbuilder`, `git_repository`
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

# Workload

- batching: customize
- max-types: `1`
- max-syms: default
- max-loc: default
- parallel-max: orchestrator-selected

# Agentic review

- checkpoints: none

# Execution

- mode: pause after each sub-campaign before promoting its session branch

# UB audit

- run: no
- model: not applicable

# Benchmark metadata

- orchestrator-backend: ask the user, showing available options
- orchestrator-model: ask the user, showing available options
- billing: `api`
- setup-approval: not required; Phase 1 is pre-approved
- results-path: `/work/wrappers-results.md`
- results-template: standard

# Setup notes

Run Phase 1 end to end. The pre-authored `build.json`, `oracle-config.json`, and
`crates.json` may be copied from `/campaign/`. Skip toolchain installation when
the required tools are already installed.

# Selection and recording notes

Execute the sub-campaigns in their listed order. The orchestrator chooses the
internal steps and wave filenames, reports each dry-run plan, and waits for
approval before spending on or promoting the next sub-campaign.

After each sub-campaign, record cost from the per-agent `<stage>.usage.json`,
measure the session-branch diff, and run `crustify-audit unsafe --name ...
--json` over the selected names. Derive cost from token counts, never from
provider-reported dollars.
