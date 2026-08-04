"""Run every CodeQL query in a directory and emit one CSV per query.

The composer modules (`syms_manifest`, `types_manifest`)
consume T1 + T2 CSVs at fixed paths:

  <repo_root>/.crustify/codeql/t1/<query_name>.csv  ← entities/
  <repo_root>/.crustify/codeql/t2/<query_name>.csv  ← edges/

This module produces those CSVs by:

  1. Walking every `*.ql` in the source query directory
     (`utils/codeql/entities/` or `utils/codeql/edges/`).
  2. Running `codeql query run` against the DB to produce a `.bqrs`.
  3. Running `codeql bqrs decode --format=csv` to convert the BQRS
     to a CSV.

Per-query failures are isolated — one bad query doesn't halt the
batch; the failure is surfaced in the run output but other CSVs
still get emitted. The composer modules tolerate missing CSVs
(treat as empty).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _run_query(
    db: Path,
    query: Path,
    bqrs_out: Path,
    csv_out: Path,
) -> tuple[bool, str]:
    """Run one query → BQRS → CSV. Returns (ok, message)."""
    bqrs_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)

    # codeql query run --database=<db> --output=<bqrs> <query.ql>
    r = subprocess.run(
        [
            "codeql", "query", "run",
            "--database", str(db),
            "--output", str(bqrs_out),
            str(query),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False, f"query run failed: {r.stderr.strip()[:200]}"

    # codeql bqrs decode --format=csv --output=<csv> <bqrs>
    #
    # Decode to a sibling temp path and `os.replace` into position rather than
    # letting codeql write `csv_out` directly. The CSVs live in the ONE shared
    # `codeql/` dir that every agent worktree symlinks, and readers
    # (`query._scope_touched_fields` streams `field_accesses.csv` unlocked)
    # take no lock. A direct write truncates in place, so a reader landing in
    # that window gets a SHORT read -- fewer field touchers, no error, no
    # warning. `os.replace` is atomic within a filesystem: a reader sees the
    # whole old file or the whole new one.
    tmp_out = csv_out.with_name(csv_out.name + ".tmp")
    r = subprocess.run(
        [
            "codeql", "bqrs", "decode",
            "--format=csv",
            "--output", str(tmp_out),
            str(bqrs_out),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        tmp_out.unlink(missing_ok=True)
        return False, f"bqrs decode failed: {r.stderr.strip()[:200]}"
    os.replace(tmp_out, csv_out)

    return True, "ok"


def extract_all(
    db: Path,
    queries_dir: Path,
    csv_out_dir: Path,
    *,
    bqrs_workdir: Path | None = None,
) -> tuple[int, int, list[str]]:
    """Run every `*.ql` under `queries_dir` against `db` and write
    one CSV per query to `csv_out_dir/<query_stem>.csv`.

    `bqrs_workdir` is the temp dir for intermediate `.bqrs` files;
    defaults to `csv_out_dir / "_bqrs"`.

    Returns ``(succeeded, failed, failure_messages)``.
    """
    if bqrs_workdir is None:
        bqrs_workdir = csv_out_dir / "_bqrs"
    csv_out_dir.mkdir(parents=True, exist_ok=True)

    queries = sorted(queries_dir.glob("*.ql"))
    if not queries:
        return 0, 0, [f"no .ql files in {queries_dir}"]

    succeeded = 0
    failed = 0
    failures: list[str] = []

    for q in queries:
        bqrs = bqrs_workdir / f"{q.stem}.bqrs"
        csv = csv_out_dir / f"{q.stem}.csv"
        ok, msg = _run_query(db, q, bqrs, csv)
        if ok:
            succeeded += 1
            print(f"  ✓ {q.stem}.csv ({csv.stat().st_size:,} bytes)")
        else:
            failed += 1
            failures.append(f"{q.stem}: {msg}")
            print(f"  ✗ {q.stem}: {msg}")

    # Cleanup intermediate BQRS files; keep CSVs.
    if bqrs_workdir.exists() and bqrs_workdir.is_dir():
        shutil.rmtree(bqrs_workdir, ignore_errors=True)

    return succeeded, failed, failures


def extract_t1_t2(
    db: Path,
    crustify_root: Path,
    out_root: Path,
) -> tuple[int, int]:
    """Run both T1 (entities/) and T2 (edges/) query batches.

    Writes to `out_root / "t1" / *.csv` and `out_root / "t2" / *.csv`.

    Returns ``(total_succeeded, total_failed)``.
    """
    entities_dir = crustify_root / "utils" / "codeql" / "entities"
    edges_dir = crustify_root / "utils" / "codeql" / "edges"

    print(f"[crustify-cli analyze extract-ql] extracting T1 (entities) → "
          f"{out_root / 't1'}/")
    s1, f1, _ = extract_all(db, entities_dir, out_root / "t1")
    print(f"  T1: {s1} ok, {f1} failed")

    print(f"[crustify-cli analyze extract-ql] extracting T2 (edges) → "
          f"{out_root / 't2'}/")
    s2, f2, _ = extract_all(db, edges_dir, out_root / "t2")
    print(f"  T2: {s2} ok, {f2} failed")

    return s1 + s2, f1 + f2
