---

The user has chosen the following configuration:

campaign repo: `https://github.com/intel/cryptography-primitives`, commit 9d397ba62e2369b63171bc995e9c1179aaa5c0dc
campaign objective: wrap
campaign target: the whole public API, except the imported closure
max-syms: default
max-loc: default
max-types: 2
billing: API
parallel-max: you pick an optimal value
parallel-policy: default
agent backend: codex
model: gpt-5.6-sol
review mode: run the review objective on all waves at the end of the campaign using gpt-5.6-sol as the model

## Phase 1

Run Phase 1 of the crustify playbook end to end.

## Phase 2

The goal is to wrap the entire API surface of the library, except the imported closure.

## Autonomy

You have the approval to run fully autonomously end to end,
without waiting for the user's approval to proceed between waves.

## Recording

Record and and git track results in `<repo-checkout>/crustify/wrappers-results.md`.
Use the exact format as in the template.