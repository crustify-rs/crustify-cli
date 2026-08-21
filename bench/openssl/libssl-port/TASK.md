---

The user has chosen the following configuration:

target repo: `https://github.com/openssl/openssl.git`, commit 0ffaa24c6148514fa77f76a48ae48852df7be9e7
target: `ssl` — the whole `ssl/` dir of openssl, plus the headers outside it
        that libssl itself implements (see `scope-config.json`)
max-syms: default
max-loc: default
max-types: 1
billing: subscription
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

Four waves, in this order. Each is `--objective wrap`.
Check out the plan first from `--dry-run`.

**1. The untyped lifetime tiers**, `void` then `string`. The playbook requires
these ahead of every other wave: they produce the release/clone strategies that
owned pointers to type-erased and NUL-terminated objects need.

```
crustify-cli /work/<openssl-checkout> ssl translate --lifetime-for void
crustify-cli /work/<openssl-checkout> ssl translate --lifetime-for string
```

**2. The type import-closure**, 46 types, one DAG layer at a time, lowest
first. DAG layer 0 — 40 types:

```
crustify-cli /work/<openssl-checkout> ssl translate --objective wrap --name \
    SHAstate_st MD5state_st ossl_param_st buf_mem_st ring_buf pollfd \
    ossl_dispatch_st SHA512state_st wpacket_st SHA256state_st OSSL_TIME \
    evp_keymgmt_st evp_pkey_ctx_st evp_cipher_st ossl_provider_st evp_md_st \
    ossl_lib_ctx_st evp_pkey_st evp_kdf_st evp_mac_st evp_cipher_ctx_st \
    bio_st lhash_st bio_method_st err_state_st stack_st comp_method_st \
    conf_imodule_st ct_policy_eval_ctx_st evp_md_ctx_st evp_rand_ctx_st \
    ossl_algorithm_st PACKET pthread_mutex_t evp_kdf_ctx_st evp_mac_ctx_st \
    CRYPTO_REF_COUNT ASN1_VALUE_st crypto_mutex_st engine_st
```

Then DAG layer 1 — 6 types:

```
crustify-cli /work/<openssl-checkout> ssl translate --objective wrap --name \
    ssl_comp_st x509_st dh_st X509_name_st stack_st_SCT stack_st_SSL_COMP
```

**3. The import symbols the target needs at layers L0–>L2**, 68 functions.
These are what target code at those layers calls but does not own. Layer 1's
demand — 27:

```
crustify-cli /work/<openssl-checkout> ssl translate --objective wrap --name \
    CRYPTO_free __assert_fail memcpy memset CRYPTO_malloc CRYPTO_zalloc \
    strlen memcmp __errno_location OPENSSL_LH_COMPFUNC \
    OPENSSL_LH_DOALL_FUNCARG OPENSSL_LH_DOALL_FUNC OPENSSL_LH_HASHFUNC \
    OPENSSL_strcasecmp CRYPTO_realloc_array strcmp memchr \
    ossl_quic_vlint_encode_len strspn BIO_closesocket OBJ_sn2nid OBJ_ln2nid \
    ossl_isdigit ossl_ctype_check write read BIO_fd_non_fatal_error
```

Then layer 2's, wrap layer 0 — 12:

```
crustify-cli /work/<openssl-checkout> ssl translate --objective wrap --name \
    ERR_new ERR_set_debug ossl_assert_int CRYPTO_memdup ossl_time_now \
    ERR_pop_to_mark ossl_ticks2time ERR_set_mark ossl_time_compare \
    CRYPTO_realloc ossl_time2ticks ossl_time_add
```

Then layer 2's, wrap layer 1 — 29:

```
crustify-cli /work/<openssl-checkout> ssl translate --objective wrap --name \
    PACKET_remaining WPACKET_put_bytes__ BIO_ctrl PACKET_data OPENSSL_sk_num \
    OPENSSL_sk_value WPACKET_sub_memcpy__ PACKET_buf_init PACKET_get_net_2 \
    PACKET_get_1 WPACKET_memcpy OPENSSL_sk_push WPACKET_get_total_written \
    PACKET_get_length_prefixed_2 RAND_bytes_ex EVP_MD_CTX_new EVP_MD_get_size \
    OSSL_PARAM_construct_end BIO_puts PACKET_get_length_prefixed_1 \
    PACKET_get_quic_vlint ossl_time_is_zero ossl_time_zero PACKET_forward \
    PACKET_get_bytes WPACKET_quic_write_vlint BIO_indent ossl_time_infinite \
    WPACKET_allocate_bytes
```

**4. The god objects.** The three target types with 25 or more declared fields,
and their transitive closure:

```
crustify-cli /work/<openssl-checkout> ssl translate \
    --name record_layer_st quic_stream_st ssl_session_st \
    --transitive --objective wrap --dry-run
```

## Autonomy

After establishing the campaign parameters with the user, you run full autonomously, end-to-end.
You do not wait for the user's approval before launching waves.

## Recording

Record results in `/work/wrappers-results.md` using the exact format of the template.

After each wave: `utils/log_cost.py` over the per-agent `<stage>.usage.json`
for cost, the session branch diff for what landed, and `audit` for the unsafe
and raw-pointer surface. Cost comes from token counts, never from
provider-reported dollars.
