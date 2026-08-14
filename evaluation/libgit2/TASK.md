---

The user has chosen the following configuration:

target repo: `https://github.com/libgit2/libgit2.git`, commit ddf3b5c85d86a389330b1d1dd90f08f60ae05fe4
port-scope target: the whole `src/` dir of libgit2
max-syms: default
max-loc: default
max-types: 1
billing: API
parallel-max: you pick an optimal value
parallel-policy: default
agent backend: ask user, showing available options
model: ask user, showing available options

## Phase 1

Run Phase 1 of the playbook end to end using the already-authored `/campaign/build.json`.

Playbook toolchain is already installed.

## Phase 2

Three waves, in this order. Each is `--objective wrap`. Report the plan from
`--dry-run` and wait for approval before spending on any of them.

**1. The type wrap-closure.** Every wrap-scope type and callback, bottom-up by
its own wrap DAG layer:

```
crustify-oracle /work/libgit2 src query types --wrap-only
crustify-oracle /work/libgit2 src query dag --layer <L> --wrap-only
```

Wave one layer at a time, lowest first, using `--name`.

**2. The wrap-scope symbols the port scope needs at port layers 0–2.** The
functions and globals port-scope code calls but does not own. Select by `--name`.

**3. The god objects.** The three port-scope types with more than 25 declared
fields, and their transitive closure:

```
crustify-cli /work/libgit2 src translate \
    --name git_indexer git_packbuilder git_repository \
    --transitive --objective wrap --dry-run
```

## Recording

Record results in /work/wrappers-results.md.
Make two copies: one for the wrap closure and one for the god objects.

After each wave: `utils/log_cost.py` over the per-agent `<stage>.usage.json`
for cost, the session branch diff for what landed, and `audit` for the unsafe
and raw-pointer surface. Cost comes from token counts, never from
provider-reported dollars.

Do not promote a session branch. Landing is a deliberate act and this run
ends with the branches left for review.
