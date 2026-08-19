---

The user has chosen the following configuration:

target repo: `https://github.com/libgit2/libgit2.git`, commit ddf3b5c85d86a389330b1d1dd90f08f60ae05fe4
target files: the whole `src/` dir of libgit2
max-syms: default
max-loc: default
max-types: 1
billing: API
parallel-max: the orchestrator picks an optimal value
parallel-policy: default
agent backend: ask user, showing available options
model: ask user, showing available options

## Phase 1

Run Phase 1 of the playbook end to end.
The following artifacts are already authored, you can skip authoring them: 
    - `/campaign/{build, scope-config, crates}.json`

Skip installing the playbook's toolchain if already installed.

## Phase 2

Three waves, in this order. Each is `--objective wrap`. Report the plan from
`--dry-run` and wait for approval before spending on any of them.

**1. The type import-closure.** Every import type and callback, bottom-up by
its own wrap DAG layer:

```
crustify-oracle /work/<libgit2-checkout> src query types --imported-only
crustify-oracle /work/<libgit2-checkout> src query dag --layer <L> --imported-only
```

Wave one layer at a time, lowest first, using `--name`.

**2. The import symbols the target needs at layers L0–>L2.** First, compute the
symbol target closure at layers L0->L2. Second, compute their import symbol deps
(functions, callbacks, globals). These are functions and globals target code at
L0->L2 calls but does not own. Select by `--name`.

**3. The god objects.** The three target types with more than 25 declared
fields, and their transitive closure:

```
crustify-cli /work/<libgit2-checkout> src translate \
    --name git_indexer git_packbuilder git_repository \
    --transitive --objective wrap --dry-run
```

## Autonomy

Wait for the user's go before promoting a session branch and proceeding with the next wave.

## Recording

Record results in `/work/wrappers-results.md` and use the exact format of the template.

After each wave: `utils/log_cost.py` over the per-agent `<stage>.usage.json`
for cost, the session branch diff for what landed, and `audit` for the unsafe
and raw-pointer surface. Cost comes from token counts, never from
provider-reported dollars.
