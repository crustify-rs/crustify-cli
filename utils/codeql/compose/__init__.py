"""Composer layer — deterministic Python over the CodeQL fact CSVs (Tier 1+2
queries under `utils/codeql/entities/` and `utils/codeql/edges/`).

Emits the structural half of the type and symbol records, the scope manifest
and the dependency dag. Everything here is scope-agnostic except where a
caller passes the target's scope manifest to narrow the emit.

See `README.md` in this directory for the architectural rationale.
"""
