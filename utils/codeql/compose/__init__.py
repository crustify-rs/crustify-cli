"""Composer layer — deterministic Python that bridges raw CodeQL
fact CSVs (Tier 1+2 queries under `utils/codeql/entities/` and
`utils/codeql/edges/`) to the factual skeletons of the wrap-stage
manifests — the per-stem `files.json` / `types.json` / `syms.json`
under the analysis tree (scope-agnostic and cumulative; port vs wrap
is applied at read time via the target's `scope.json`).

See `README.md` in this directory for the architectural rationale
and the agent input contract.
"""
