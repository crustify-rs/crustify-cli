"""Two explicit build phases: `propose` and `execute`.

State machine driven by two CLI subcommands rather than on-disk file
presence. Each phase produces a distinct artifact:

  <repo_root>/.crustify/build.json     — drafted by `build propose`
                                          (CrustifyBuildPropose)
  <repo_root>/.crustify/codeql/db/     — produced by `build execute`
                                          (CrustifyBuildExecute)
  <repo_root>/.crustify/codeql/t1/*.csv — produced by `build execute`
                                          (Phase 3, query extract)
  <repo_root>/.crustify/codeql/t2/*.csv — produced by `build execute`
                                          (Phase 3, query extract)

Gating rules (intentionally strict — no implicit transitions):

  | Subcommand        | Required pre-state                | Action               |
  |-------------------|-----------------------------------|----------------------|
  | `build propose`   | build.json must NOT exist         | run Phase 1          |
  | `build propose --redo` | (any)                        | delete build.json,   |
  |                   |                                   | then run Phase 1     |
  | `build execute`   | build.json MUST exist             | run Phase 2 + Phase 3|
  | `build execute --redo` | build.json MUST exist        | delete codeql/db/,   |
  |                   |                                   | then run Phase 2 + 3 |

`build execute` always runs the full configure + build + tests +
CodeQL extract pipeline, by design. After the CodeQL database is
built, the T1 (entities) and T2 (edges) queries are run mechanically
and their CSVs deposited under `<repo_root>/.crustify/codeql/{t1,t2}/`.
The analyze pipeline consumes those CSVs.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from crustify.agents.build import CrustifyBuildExecute, CrustifyBuildPropose


def build_propose(target: Path, *, redo: bool = False) -> None:
    """Phase 1: draft `<repo_root>/.crustify/build.json`.

    Refuses to run if `build.json` already exists, unless `redo=True`,
    in which case the existing file is deleted first.
    """
    propose = CrustifyBuildPropose(target)
    build_json = propose.root_store.path("build.json")

    if build_json.exists():
        if not redo:
            print(
                f"[crustify build propose] build.json already exists at "
                f"{build_json}.\n"
                f"                          Pass --redo to delete it and "
                f"re-propose, or run `build execute` to use it.",
                file=sys.stderr,
            )
            sys.exit(1)
        build_json.unlink()
        print(
            f"[crustify build propose] --redo: deleted {build_json}; "
            f"re-proposing."
        )

    propose.run()
    print(
        f"[crustify build propose] drafted {build_json}.\n"
        f"                          Review the file, then run "
        f"`crustify {target} build execute`."
    )


def build_execute(target: Path, *, redo: bool = False) -> None:
    """Phase 2 + 3: configure + build + tests + CodeQL DB, then T1/T2 extract.

    Refuses to run if `build.json` does not exist (point user at
    `build propose`). With `redo=True`, deletes any existing CodeQL
    database before re-executing so Phase 2 produces a fresh DB.
    """
    execute = CrustifyBuildExecute(target)
    build_json = execute.root_store.path("build.json")

    if not build_json.exists():
        print(
            f"[crustify build execute] build.json not found at "
            f"{build_json}.\n"
            f"                          Run `crustify {target} build "
            f"propose` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    repo_root = execute.repo_root
    from crustify.layout import Layout
    codeql_db = Layout(repo_root).codeql_db

    if redo and codeql_db.exists():
        shutil.rmtree(codeql_db)
        print(
            f"[crustify build execute] --redo: deleted {codeql_db}; "
            f"re-executing."
        )

    execute.run()
    _extract_t1_t2(repo_root)


def _extract_t1_t2(repo_root: Path) -> None:
    """Run every `.ql` under `utils/codeql/entities/` and
    `utils/codeql/edges/` against the CodeQL DB and write one CSV
    per query under `<repo_root>/.crustify/codeql/{t1,t2}/`.
    """
    # The composer modules live at `utils/codeql/compose/`; the helper
    # we need is `compose.extract_csvs.extract_t1_t2`.
    crustify_root = Path(__file__).resolve().parent.parent.parent
    compose_parent = crustify_root / "utils" / "codeql"
    if str(compose_parent) not in sys.path:
        sys.path.insert(0, str(compose_parent))
    from compose.extract_csvs import extract_t1_t2

    from crustify.layout import Layout
    _lay = Layout(repo_root)
    db = _lay.codeql_db
    out_root = _lay.codeql
    if not db.is_dir():
        raise SystemExit(
            f"build execute: CodeQL database not found at {db}; "
            f"BuildExecute did not produce the expected artifact."
        )
    succeeded, failed = extract_t1_t2(db, crustify_root, out_root)
    print(
        f"[crustify build execute] T1+T2 extraction: {succeeded} queries "
        f"ok, {failed} failed"
    )
    if failed:
        raise SystemExit(
            f"build execute: {failed} query extraction(s) failed; see "
            f"output above. Analyze stages will see empty / missing "
            f"CSVs for those queries."
        )
