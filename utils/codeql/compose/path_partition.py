"""File → manifest directory mapping.

Per-source-file partitioning rule for the repo-root analysis tree at
`<repo_root>/.crustify/analysis/`:

  - In-tree files use stem-grouped manifest dirs:
      `ssl/record/record.c` → `Path("ssl/record/record")`
      `ssl/record/record.h` → `Path("ssl/record/record")` (same dir;
                                                          .h and .c of
                                                          the same stem
                                                          coexist)

  - System / external files (CodeQL gives absolute paths for these)
    route under the `system/` prefix:
      `/usr/include/string.h` → `Path("system/usr/include/string")`

  - Bare filenames (no directory component) also route to system:
      `string.h` → `Path("system/string")`

  - Empty / null file paths return `None`; the caller decides whether
    to drop the entry or emit it elsewhere.
"""
from __future__ import annotations

from pathlib import Path

SYSTEM_PREFIX = Path("system")


def manifest_dir_for(file_path: str | None) -> Path | None:
    """Return the repo-relative manifest directory for a source/header
    file path. See module docstring for the partitioning rule.
    """
    if not file_path:
        return None
    p = Path(file_path)
    stem = p.stem
    parent = p.parent

    if p.is_absolute():
        # Strip the leading "/" so the system bucket gets a relative
        # path underneath it.
        rel_parent = Path(*parent.parts[1:])
        return SYSTEM_PREFIX / rel_parent / stem

    if str(parent) in ("", "."):
        return SYSTEM_PREFIX / stem

    return parent / stem
