"""Orchestration for the ``audit`` command.

A **deterministic** pass (no LLM): an **entity-seeded, global** scan of the
ported Rust tree. Seeds are types/symbols (``--name``) — or every entity homed
under a ``--file`` / ``--dir`` / ``--crate``, or ``--all``. For each seed it
reports its own implementation's unsafe/raw-pointer surface and its **naked
``ffi::`` footprint** (the wrapper being bypassed elsewhere). Printed to the
console as JSON — nothing written to disk. See
``utils/codeql/compose/audit_manifest.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))


def audit(
    target: Path,
    *,
    all: bool = False,
    names: list[str] | None = None,
    crate: str | None = None,
    mod: str | None = None,
    file: str | None = None,
    dir: str | None = None,
) -> None:
    """Audit the seed set and print the report (deterministic; console-only).

    Exactly one selector: ``all`` / ``names`` / ``crate`` / ``mod`` /
    ``file`` / ``dir``. ``--file`` / ``--dir`` / ``--crate`` / ``--mod`` resolve
    to the entities *homed* there; the naked-FFI search is always global."""
    selectors = [all, bool(names), crate is not None, mod is not None,
                 file is not None, dir is not None]
    if sum(bool(x) for x in selectors) != 1:
        raise SystemExit(
            "audit: pass exactly one of --all / --name / --crate / --mod / "
            "--file / --dir.")

    from compose.audit_manifest import audit as _scan
    from crustify.layout import Layout

    layout = Layout.discover(target)
    rust_root = layout.rust
    if not rust_root.is_dir():
        raise SystemExit(f"error: no Rust tree at {rust_root}. Run `scaffold` first.")

    # --dir is --mod by another name (a path prefix under the crate's src/).
    doc = _scan(rust_root, all=all, names=names, crate=crate,
                mod=(mod or dir), file=file)

    sel = ("--all" if all else f"--name {' '.join(names)}" if names else
           f"--crate {crate}" if crate else f"--mod {mod}" if mod else
           f"--dir {dir}" if dir else f"--file {file}")
    doc = {"seed": sel, **doc}
    if not doc["entries"]:
        raise SystemExit(f"audit: no types/symbols matched {sel}.")
    print(json.dumps(doc, indent=2))
