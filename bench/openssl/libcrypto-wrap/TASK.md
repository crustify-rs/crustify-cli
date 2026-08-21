---

The user has chosen the following configuration:

- campaign repo: `https://github.com/openssl/openssl`, commit 2924476b5591e691e904c4baf57894c526c4b8de
- campaign objective: wrap
- campaign target: the whole public API of libcrypto, except its imported closure
- max-syms: default
- max-loc: default
- max-types: 2
- billing: API
- parallel-max: you pick an optimal value
- parallel-policy: default
- agent backend: codex
- model: gpt-5.6-sol
- review mode: run the review objective on all waves at the end of the campaign using gpt-5.6-sol as the model

## Phase 1 - Setup

You have the user's approval to run Phase 1 of the crustify playbook end to end.

## Phase 2 - Translation

You have the user's approval to run Phase 2 fully autonomously end to end.

## Recording

Record and and git track results in `<repo-checkout>/crustify/wrappers-results.md`.
Use the exact format as in the template.