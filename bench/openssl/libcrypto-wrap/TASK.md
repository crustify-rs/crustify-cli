---

The user has chosen the following configuration:

- campaign repo: `https://github.com/openssl/openssl`, commit 2924476b5591e691e904c4baf57894c526c4b8de
- campaign objective: wrap
- campaign target: the public API of libcrypto, excluding libssl and its imported closure
- max-syms: default
- max-loc: default
- max-types: 2
- billing: API
- parallel-max: you pick an optimal value
- agent backend: codex
- model: gpt-5.6-sol
- review mode: decide with user on the fly

## Phase 1 - Setup

Wait for the user's approval before starting Phase 1.

## Phase 2 - Translation

Brainstorm with the user a list of waves over a subset of libcrypto's public API.

## Recording

Record and and git track results in `<repo-checkout>/crustify/wrappers-results.md`.
Use the exact format as in the template.
