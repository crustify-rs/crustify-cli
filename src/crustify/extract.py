"""extraction — the one composer stage with side effects.

Runs the T1 (entities) + T2 (edges) `.ql` batches against the CodeQL database
at `crustify/codeql/db/` and writes one CSV per query under
`crustify/codeql/{t1,t2}/`. Every other artifact derives from those tables on
demand: scope and the dag through :mod:`crustify.cache`, the type and symbol
records through :mod:`crustify.manifests`.

The database itself is not produced here — configuring the project, building it
under `codeql database create --language=cpp --command=...`, and depositing the
result is done by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

from crustify.layout import Layout

# The composer package lives at `utils/codeql/compose/` in the crustify
# checkout, not as an installed Python package. Add the parent dir to
# sys.path so `from compose.X import Y` works from this orchestrator.
_CRUSTIFY_ROOT = Path(__file__).resolve().parent.parent.parent
_COMPOSE_PARENT = _CRUSTIFY_ROOT / "utils" / "codeql"
if str(_COMPOSE_PARENT) not in sys.path:
    sys.path.insert(0, str(_COMPOSE_PARENT))


def extract_ql(target: Path) -> None:
    """Run every `.ql` under `utils/codeql/entities/` and `utils/codeql/edges/`
    against the CodeQL database, writing one CSV per query."""
    import shutil

    from compose.extract_csvs import extract_t1_t2

    if shutil.which("codeql") is None:
        print(
            "error: the `codeql` CLI is not on PATH. Install it and run "
            "`codeql pack install` in utils/codeql/ so codeql/cpp-all "
            "resolves.",
            file=sys.stderr,
        )
        sys.exit(1)

    layout = Layout.discover(target)
    db = layout.codeql_db
    if not db.is_dir():
        print(
            f"error: CodeQL database not found at {db}.\n"
            f"       Build the project under CodeQL trace first, e.g.\n"
            f"         codeql database create {db} --language=cpp "
            f"--command=\"<build command>\"",
            file=sys.stderr,
        )
        sys.exit(1)

    succeeded, failed = extract_t1_t2(db, _CRUSTIFY_ROOT, layout.codeql)
    print(
        f"[crustify-oracle extract-ql] {succeeded} queries ok, "
        f"{failed} failed"
    )
    if failed:
        print(
            f"error: {failed} query extraction(s) failed; see output above. "
            f"Analyze stages will see empty / missing CSVs for those "
            f"queries.",
            file=sys.stderr,
        )
        sys.exit(1)
