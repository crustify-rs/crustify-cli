---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://github.com/libgit2/libgit2.git`, `ddf3b5c85d86a389330b1d1dd90f08f60ae05fe4`
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: create safe Rust wrappers
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: the named types and functions below; define the `import-type-closure`, `import-symbols-l0-l2`, and `god-objects` sub-campaigns now

# Sub-campaign questions

## `import-type-closure`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the oracle's imported section for `src/`
5. **Which headers define its public API?**
   - Answer: derive from the imported declarations
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole imported type and callback closure, in dependency order
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `import-symbols-l0-l2`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the oracle's imported section for `src/`
5. **Which headers define its public API?**
   - Answer: derive from the imported declarations
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the imported functions and globals reached by target layers L0 through L2
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `god-objects`

4. **Which implementation paths belong to this subsystem?**
   - Answer: `src/`
5. **Which headers define its public API?**
   - Answer: derive from the selected declarations
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: `git_indexer`, `git_packbuilder`, and `git_repository`, including their transitive closure
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: defaults except `max-types: 1`; parallelism is orchestrator-selected
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: no
10. **Run autonomously or pause after each sub-campaign?**
    - Answer: pause after each sub-campaign before promoting its session branch
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: no

# Benchmark recording questions

12. **Which backend and model run the orchestrator?**
    - Answer: ask the user, showing available backends and models
13. **Which billing mode should agentic stages use?**
    - Answer: `api`
14. **Has setup already been approved?**
    - Answer: yes; Phase 1 is pre-approved
15. **Where and in what format should results be recorded?**
    - Answer: `/work/wrappers-results.md`, standard template

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
