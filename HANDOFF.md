# Session state — 2026-08-07

## Branches
- `crustify` @ **oracle** (main checkout; worktree purged; venv reinstalled,
  `crustify-oracle` entry point live). 1 squashed commit on top of main + 2 doc
  commits. NOT merged to `main`.
- `openssl-crustify` @ **crustify/ssl**, 10 ahead of origin, NOT pushed.

## Done this session
- oracle refactor: `ownership-store.json` (authored analysis only), records
  composed on demand, `scope.json`/`deps-dag.json` as fingerprinted caches,
  `crustify-oracle` CLI split from `crustify-cli`, per-stem analysis tree deleted.
- Promoted session `wrap-2026-08-04_08-25-32_c442` (the V3 opus run: evp_pkey_st,
  evp_cipher_st, evp_md_st, evp_pkey_ctx_st, evp_kdf_st, evp_mac_st, stack_st).
  It had never been merged; `deps-dag.md` was reporting it as done.
- `stack.rs` taken wholesale from c442 → `OpensslStackView` (a symbol-wave
  workaround) replaced by `OpensslStackOwned<E>` / `OpensslStackBorrowed<'a,E>`.
- Reset the two instances: `SctList` lost len/is_empty; `SslCompStack` lost
  len/is_empty/get/push/find; 9 tests deleted (audited by receiver), 4 restored.
  Store records dropped for stack_st_SSL_COMP, OPENSSL_sk_num/_value/_push.
- `cargo check --workspace --all-targets` GREEN (cargo at ~/.cargo/bin).

## NEXT ACTION — spawn the rerun wave
    ~/.venvs/crustify/bin/crustify-cli --billing subscription --parallel --parallel-max 6 \
      /home/marius.guest/Workspace/git/openssl-crustify ssl \
      wrap --name stack_st_SCT stack_st_SSL_COMP \
                  OPENSSL_sk_num OPENSSL_sk_value OPENSSL_sk_push
Dry-run verified: 5 units → 3 batches, 1 layer, per-agent, `stack_st` as first-layer dep.

## Known open items
- `deps-dag.md` counts the 7 c442 types in its Σ ($334.44 / 19,683 lines); now true.
- 23 unmerged v1-era session branches remain; `deb9` is c442's discarded twin.
- `cargo test` never run this session; only `check`.
- `docs/schemas/*.md` + prompts updated for the oracle; README says `translate`
  where the verbs are `wrap`/`port`.
