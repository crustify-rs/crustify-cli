---

The user has chosen the following configuration:

target repo: `https://gitlab.gnome.org/GNOME/libxml2.git`, tag v2.15.3 (commit c94eb0210183b9d7cb43f8e7fddc6be55843ef49)
scope: WRAP campaign — `files.import` seeds off the whole published API
       under `include/libxml/`. Nothing is owned; there is no target section.
max-syms: default
max-loc: default
max-types: 1
billing: API
parallel-max: you pick an optimal value
parallel-policy: default
agent backend: ask user, showing available options
model: ask user, showing available options

## Why this target

Of the four libraries measured in `safe-ffi-surface.md`, libxml2 has the widest
gap between its C API and what the established Rust crate reaches safely:
`libxml` 0.3.21 covers **113 of 1,649** exported functions. The uncovered part
is not deprecated tail — it is whole subsystems with no safe path at all: the
XML writer (`xmlwriter.h`, 81 fns), DTD validation (`valid.h`, 71), SAX2 (36),
catalog resolution (37), and most of XPath's internals (`xpathInternals.h`, 117).

The maintainers name the cause directly: *"providing a more or less complete
wrapper would be too much work"*. That is the economics a closure-driven
pipeline does not face, and it is what this campaign tests.

Two corrections to `safe-ffi-surface.md`, which measured Debian's 2.9.14:

- `xmlunicode.h` — listed there as the single largest gap (166 fns, 0% covered)
  — is **fully deprecated and empty** at v2.15.3. It was dead weight in the
  denominator; do not plan a wave around it.
- The public surface is 1,416 `XMLPUBFUN` declarations at this tag, down from
  1,669, almost entirely deprecation removal (`nanoftp` is gone). The
  substantive gaps above are all intact and slightly larger.

One gap this campaign will NOT close: libxml2's global memory management is
documented as not thread-safe. That is a property of the C library, so a
generated wrapper inherits it. Do not claim otherwise in the results.

## Phase 1

Run Phase 1 of the playbook end to end.
The following artifacts are already authored, you can skip authoring them:
    - `/campaign/{build, scope-config}.json`

`/campaign/crates.json` is a **seeded shell**: crate identity, paths and link
edges are settled, `modules` is empty. Fill it after `extract-ql`, when the
entity inventory exists. Note the difference from a port campaign — a wrap-only
target homes entities by IMPORT HEADER, so the module tree mirrors
`include/libxml/`, and every `.rs` carries `tu: null`. Its `_comment_modules`
records a suggested grouping; verify it against the real inventory rather than
trusting it.

`build.json` needs two fields filled by the run: `test_baseline` (step 4) and
the `codeql_database` provenance block (step 5).

Playbook toolchain is already installed.

Before spending on Phase 2, report:

```
crustify-oracle /work/libxml2 . query types  --import-only
crustify-oracle /work/libxml2 . query types  --import-only --out-of-tree
crustify-oracle /work/libxml2 . query symbols --import-only
crustify-oracle /work/libxml2 . query dag --stats
```

`--out-of-tree` is the permanent FFI floor (system headers reached through the
API); the complement is libxml2's own surface. libxml2 vendors nothing, so the
out-of-tree share should be small — a large one means the scope is wrong.

Everything is import-section here, so `--target-only` returns nothing. That is
correct, not a misconfiguration: a wrap campaign owns no C.

## Phase 2

Every wave is `--objective wrap`; there is no port stage. Report the plan from
`--dry-run` and wait for approval before spending on any of them.

**1. The type closure.** Every wrap-scope type and callback, bottom-up by DAG
layer:

```
crustify-oracle /work/libxml2 . query dag --layer <L> --import-only
```

One layer at a time, lowest first, using `--name`. libxml2 defines most of its
tree structs publicly (`xmlNode`, `xmlDoc`, `xmlAttr`), so expect a much higher
share of full-layout value types than OpenSSL's mostly-opaque surface — a
struct whose definition is visible in an anchor header gets its fields walked
in full and needs real accessors, not an opaque handle.

**2. The uncovered subsystems, largest gap first.** These are the campaign's
actual deliverable — each is a block the hand-written crate leaves at 0%:

```
xmlwriter.h        81 fns    no safe XML writer exists today
valid.h            71 fns    no safe DTD validation
xpathInternals.h  117 fns    XPath extension/context API
catalog.h          37 fns
SAX2.h             36 fns
```

Select by `--file include/libxml/<header>.h`, one subsystem per wave.

**3. The remainder.** Whatever the closure has not reached, by DAG layer.

## Recording

Record results in /work/wrappers-results.md.

After each wave: `utils/log_cost.py` over the per-agent `<stage>.usage.json`
for cost, the session branch diff for what landed, and `audit` for the unsafe
and raw-pointer surface. Cost comes from token counts, never from
provider-reported dollars.

Report coverage the same way `safe-ffi-surface.md` measured it — safe functions
in CALL position, doc comments stripped — so the result is comparable to the
113/1,649 baseline rather than to a differently-counted number.

Do not promote a session branch. Landing is a deliberate act and this run
ends with the branches left for review.
