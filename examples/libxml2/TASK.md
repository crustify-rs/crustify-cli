---

# Campaign questions

1. **Which repository and revision should this campaign use?**
   - Answer: `https://gitlab.gnome.org/GNOME/libxml2.git`, `v2.15.3` (`c94eb0210183b9d7cb43f8e7fddc6be55843ef49`)
2. **Should this campaign port the C implementation to Rust, or create safe Rust wrappers?**
   - Answer: create safe Rust wrappers
3. **Where should this campaign start: one or two subsystems, a named subset of functions or types, or the whole target? Should sub-campaigns be defined now or brainstormed during the live session?**
   - Answer: the whole target; define the `public-api-types`, `xml-writer`, `dtd-validation`, `xpath-internals`, `catalog-resolution`, `sax2`, and `public-api-remainder` sub-campaigns now

# Sub-campaign questions

## `public-api-types`

4. **Which implementation paths belong to this subsystem?**
   - Answer: the libxml2 implementation selected during setup
5. **Which headers define its public API?**
   - Answer: `include/libxml/`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole published type and callback closure, in dependency order
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `xml-writer`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the declaration inventory
5. **Which headers define its public API?**
   - Answer: `include/libxml/xmlwriter.h`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole subsystem
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `dtd-validation`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the declaration inventory
5. **Which headers define its public API?**
   - Answer: `include/libxml/valid.h`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole subsystem
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `xpath-internals`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the declaration inventory
5. **Which headers define its public API?**
   - Answer: `include/libxml/xpathInternals.h`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole subsystem
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `catalog-resolution`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the declaration inventory
5. **Which headers define its public API?**
   - Answer: `include/libxml/catalog.h`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole subsystem
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `sax2`

4. **Which implementation paths belong to this subsystem?**
   - Answer: derive from the declaration inventory
5. **Which headers define its public API?**
   - Answer: `include/libxml/SAX2.h`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: the whole subsystem
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

## `public-api-remainder`

4. **Which implementation paths belong to this subsystem?**
   - Answer: the libxml2 implementation selected during setup
5. **Which headers define its public API?**
   - Answer: `include/libxml/`
6. **Should it cover the whole subsystem or only named types and functions?**
   - Answer: everything not completed by earlier sub-campaigns
7. **Which backend and model should translate this sub-campaign?**
   - Answer: ask the user, showing available backends and models

# Campaign execution questions

8. **Use default workload settings, or customize them?**
   - Answer: defaults except `max-types: 1`; parallelism is orchestrator-selected
9. **Do you want agentic review? At which milestones and with which model?**
   - Answer: no
10. **What batch caps should review agents use? We recommend 3x the translation caps.**
    - Answer: not applicable
11. **Run the optional agentic UB pass? If so, with which model?**
    - Answer: no

# Autonomy questions

A1. **Should I run fully autonomously end to end?**
    - Answer: no
A2. **If no, should I wait for your approval before starting the setup phase?**
    - Answer: no; Phase 1 is pre-approved
A3. **Should I wait for your approval before starting the translation phase?**
    - Answer: no
A4. **Should I wait for your approval in between sub-campaigns?**
    - Answer: yes; never promote a session branch without approval
A5. **Should I wait for your approval before starting review passes?**
    - Answer: not applicable
A6. **Should I wait for your approval before starting UB audit passes?**
    - Answer: not applicable

# Benchmark recording questions

12. **Which billing mode should agentic stages use?**
    - Answer: `api`
13. **Where and in what format should results be recorded?**
    - Answer: `/work/results.md`, standard template

# Why this target

The historical safe-FFI measurement found that `libxml` 0.3.21 safely covered
113 of 1,649 exported functions. The uncovered surface included the XML writer
(81 functions), DTD validation (71), XPath internals (117), catalog resolution
(37), and SAX2 (36). At v2.15.3, `xmlunicode.h` is fully deprecated and empty;
do not treat it as a sub-campaign. The current tag has 1,416 `XMLPUBFUN`
declarations after deprecation removal.

Libxml2's global memory management is documented as not thread-safe. Generated
wrappers inherit that C-library property; do not claim otherwise.

# Setup notes

Run Phase 1 end to end. The pre-authored `build.json` and `oracle-config.json`
may be copied from `/campaign/`. Its `crates.json` is a seeded shell: fill its
module inventory after `extract-ql`, mirroring `include/libxml/`, and verify the
suggested grouping against the real inventory. Populate `build.json`'s
`test_baseline` and CodeQL provenance fields during setup. The toolchain is
already installed.

Before translation, report the API-only types, symbols and files and the
out-of-tree imported type floor. A large out-of-tree share indicates a scope
error because libxml2 vendors no dependencies.

# Selection and recording notes

Execute the sub-campaigns in their listed order. The orchestrator owns internal
wave construction and reports each dry-run plan before spending.

Record coverage using safe functions in call position with documentation
comments stripped, so it remains comparable to the 113/1,649 baseline. After
each sub-campaign, record token-derived cost, the session-branch diff, and the
deterministic unsafe/raw-pointer scan over the selected names.
