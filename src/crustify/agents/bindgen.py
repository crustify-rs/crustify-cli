"""CrustifyBindgenShimmer — per-``<lib>-sys`` bindgen agent stage.

The deterministic composer (``compose/bindgen_manifest.py``) has already
scaffolded each ``<lib>-sys`` crate (Cargo.toml, build.rs with the
allowlists, bindgen.h include closure, empty agent-owned managed blocks)
and emitted a per-crate ``crustify-bindgen.json`` worklist. This agent
does everything that needs a compiler or judgement:

  1. **Macro shims** — for each ``macros[]`` entry, infer the signature
     from the expansion + call sites and emit a ``static inline`` verbatim-call
     wrapper into ``bindgen.h``'s ``crustify:macros`` block (made linkable by
     ``wrap_static_fns``), or decline with a justification.
  2. **Verify loop** — ``cargo check -p <lib>-sys``; inspect the generated
     ``bindings.rs``; fix opaque/non-opaque misses (add the missing header
     to ``bindgen_extra.h``); recover ``macro_constant`` misses with a
     const-shim. Iterate until it builds and the worklist's
     ``non_opaque_types`` / ``const_macros`` are satisfied.

One agent instance per ``-sys`` crate. The orchestrator runs them in
``foreign_libs`` dependency order (a dependency crate must ``cargo check``
before a dependent can resolve its ``use <dep>_sys::*``).
"""

from __future__ import annotations

from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyBindgenShimmer(CrustifyAgent):
    """Per-``<lib>-sys`` crate: macro/global shims + cargo-check verify loop."""

    name = "CrustifyBindgenShimmer"
    model = "anthropic/claude-opus-4-8"

    def __init__(self, target: Path, *, library: str) -> None:
        super().__init__(target)
        self._library = library
        # Disambiguate concurrent/sequential per-crate logs.
        self.stage_suffix = library

    @property
    def output(self) -> str | None:  # type: ignore[override]
        # No single artifact — the agent edits several files and must
        # converge `cargo check`. Idempotency is the prompt's job (skip
        # macros already in the managed block; stop when the build is
        # green). Always runs; the orchestrator gates invocation.
        return None

    @property
    def stage(self) -> str:  # type: ignore[override]
        return "bindgen"

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        # Shared workspace at crustify/rust/<lib>-sys/ (not per-target).
        rust_root = self.layout.rust
        sys_dir = rust_root / f"{self._library}-sys"
        return {
            "target":         self.target_rel,
            "repo_root":      str(self.repo_root),
            "library":        self._library,
            "sys_crate":      f"{self._library}-sys",
            "sys_dir":        str(sys_dir),
            "workspace_root": str(rust_root),
            "worklist":       str(sys_dir / "crustify-bindgen.json"),
            "build_rs":       str(sys_dir / "build.rs"),
            "bindgen_h":      str(sys_dir / "bindgen.h"),
            "bindgen_extra":  str(sys_dir / "bindgen_extra.h"),
            # macro shims now live in bindgen.h's crustify:macros block
            "discipline":     str(crustify_root / "docs" / "DISCIPLINE.md"),
        }
