# Deterministic `unsafe` output

`crustify-audit REPO unsafe` builds the workspace through a rustc driver over
HIR and type checking. Its output is a deterministic description of the code
that the build's active `cfg` selects—not a soundness verdict or quality score.

The scan is written to `crustify/audit/unsafe.json`. For ordinary repositories,
the tool adds `/unsafe.json` to `crustify/audit/.gitignore`; campaigns may ignore
it from `crustify/.gitignore` instead.

## Availability

The scan requires:

- nightly Rust with `rustc-dev` and `llvm-tools`;
- successful `cargo metadata`;
- a workspace that builds under the nightly driver, including its native
  dependencies.

When measurement cannot run, `counts` is `null` and `counts_unavailable`
contains the failure. The tool does not substitute approximate results.

Generated `-sys` binding crates are excluded from the aggregate because they
are not the wrapper being audited.

## Document shape

- `crate_path`: audited Cargo workspace.
- `counts`: aggregate compiled-code metrics, or `null`.
- `counts_unavailable`: failure text when counts are unavailable.
- `derived`: ratios and boundary-oriented comparisons calculated from counts.
- `seed` and `entries`: present when `--name` requests named source sites.

Unsafe-block totals provide context only. A C wrapper must contain unsafe code,
and combining many small unsafe blocks into fewer large ones improves the count
without improving the code.

The more categorical derived fields describe obligations outside the expected
FFI seam:

- `unsafe_fn_smell`: unsafe functions not classified as seam functions.
- `raw_ptr_smell`: raw-pointer argument/return positions not classified as
  seam positions.
- `unsafe_fn_pub_ratio`: fraction of unsafe functions exposed publicly.
- `raw_ptr_seam_ratio`: fraction of raw-pointer positions at the seam.

`ref_to_type_wrapper` counts references over layout-compatible wrapper types
whose underlying memory C may mutate. Read it together with
`wrapper_newtypes`; when there are no wrapper newtypes, zero is vacuous.

## Named sites

Repeat `--name` to search for C type or symbol names:

```sh
crustify-audit REPO unsafe --name SSL SSL_new --name SSL_free
```

Names resolve independently within each compiled workspace crate, and each
entry retains its `crate` field.

For types, entries may contain:

- `raw_ptr_sites`: raw-pointer declarations in arguments, returns, fields, and
  explicitly typed locals.
- `raw_deref_sites`: dereference expressions for matching pointers.
- `deref_impl_sites` and `deref_mut_impl_sites`: manual wrapper dereference
  implementations.
- `slice_ref_sites` and `slice_mut_sites`: wrapper slice types and expressions
  that materialize them, including inferred `slice::from_raw_parts` calls.

For symbols, matching uses the Rust item name or linked/exported C name.
Signature/body pointer declarations and body dereferences are attributed to the
symbol; calls to external C functions attribute the enclosing wrapper
function's corresponding sites. Wrapper-specific dereference and slice fields
remain type-only.
