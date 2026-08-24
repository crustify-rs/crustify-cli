---

# Campaign

- repository: `https://gitlab.gnome.org/GNOME/libxml2.git`
- revision: `v2.15.3` (`c94eb0210183b9d7cb43f8e7fddc6be55843ef49`)
- objective: `wrap`

# Sub-campaigns

## `public-api-types`

- subsystem: public types and callbacks
- implementation-paths: the libxml2 implementation selected during setup
- api-headers: `include/libxml/`
- coverage: whole published type and callback closure
- named-items: derive from the API-only DAG in dependency order
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `xml-writer`

- subsystem: XML writer
- implementation-paths: derive from the declaration inventory
- api-headers: `include/libxml/xmlwriter.h`
- coverage: whole subsystem
- named-items: all API declarations from this header
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `dtd-validation`

- subsystem: DTD validation
- implementation-paths: derive from the declaration inventory
- api-headers: `include/libxml/valid.h`
- coverage: whole subsystem
- named-items: all API declarations from this header
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `xpath-internals`

- subsystem: XPath extension and context API
- implementation-paths: derive from the declaration inventory
- api-headers: `include/libxml/xpathInternals.h`
- coverage: whole subsystem
- named-items: all API declarations from this header
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `catalog-resolution`

- subsystem: catalog resolution
- implementation-paths: derive from the declaration inventory
- api-headers: `include/libxml/catalog.h`
- coverage: whole subsystem
- named-items: all API declarations from this header
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `sax2`

- subsystem: SAX2
- implementation-paths: derive from the declaration inventory
- api-headers: `include/libxml/SAX2.h`
- coverage: whole subsystem
- named-items: all API declarations from this header
- translator-backend: ask the user, showing available options
- translator-model: ask the user, showing available options

## `public-api-remainder`

- subsystem: remaining published API
- implementation-paths: the libxml2 implementation selected during setup
- api-headers: `include/libxml/`
- coverage: everything not completed by earlier sub-campaigns
- named-items: derive from the remaining API-only DAG
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

- mode: pause after each sub-campaign; never promote a session branch

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
