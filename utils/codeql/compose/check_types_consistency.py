#!/usr/bin/env python3
"""
check_types_consistency.py — consistency gate + op-claim lens for the
types.json analysis tree.

Read-only, deterministic, no CodeQL. Walks every
<analysis_root>/**/types.json, builds a tag -> [entries] index (the same
type tag may legitimately recur across disjoint TUs/headers — NOT an
error), and either runs the whole-tree gate or answers a focused
op-claim query.

Modes:

  (no flag)         whole-tree GATE — runs every check, exits non-zero
                    on any `error`. Use as the post-parallel
                    reconciliation gate.
  --type T          focused lens: ops of T also claimed by another type
                    (post-write self-check while annotating T).
  --check fn,fn,..  focused lens: which candidates are already claimed
                    somewhere (pre-write self-check).

Checks run by the GATE:

  - op-uniqueness      a non-lifecycle op belongs to at most one type.
  - ptr-invariants     per pointer field: exclusive/shared only when owned;
                       borrowed => lifetime set; string XOR array; const =>
                       mutable != true. (owned+borrowed and exclusive+shared
                       MAY co-occur -- runtime-conditional dual ownership.)

Lifecycle ops — the union of every entry's `ctors`, `dtor`, `up_ref`,
`clone`, `locking.{acquire,release}` — are EXEMPT from op-uniqueness in
every mode: they are legitimately shared across related types (a ctor
that allocates a wider type and returns an embedded handle; a lock pair
may guard more than one type). Pass `--raw` to a focused mode to disable
the exemption and see every overlap.

Detect-only: it flags, it does not resolve. Resolution is the agent's
job (type_analyzer.md). Parallel agents in separate chains cannot see
each other's in-flight writes, so cross-type invariants must be checked
after the fact (PITFALLS 2026-06-07).
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from collections import defaultdict, namedtuple

try:                                  # package import (composer) or script run
    from . import scope
except ImportError:                   # invoked as `python3 …/check_types_consistency.py`
    import scope

Finding = namedtuple("Finding", "severity check type message")


# ---------------------------------------------------------------- load

def load(analysis_root: Path) -> dict[str, list[tuple[dict, Path]]]:
    """tag -> [(entry, manifest_path), ...] across the whole tree.

    Duplicate tags are kept (collisions in disjoint headers / TUs are
    legitimate).
    """
    by_type: dict[str, list[tuple[dict, Path]]] = defaultdict(list)
    for f in analysis_root.rglob("types.json"):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            continue
        for e in doc.get("types", []):
            tag = e.get("name") or e.get("type")
            if tag:
                by_type[tag].append((e, f))
    return by_type


def op_index(by_type) -> dict[str, dict[str, Path]]:
    """fn_name -> {type_tag: manifest_path} over every type's method surface
    (lifecycle ops, derived via scope.type_method_syms; the explicit `ops` list
    for synthetic clusters)."""
    idx: dict[str, dict[str, Path]] = defaultdict(dict)
    for tag, entries in by_type.items():
        for entry, path in entries:
            for op in scope.type_method_syms(entry):
                idx[op][tag] = path
    return idx


def lifetime_set(by_type) -> set[str]:
    """Global union of lifecycle/lifetime function names: ctors + dtor
    + up_ref + clones + locking.{acquire,release}. Exempt from
    op-uniqueness.
    """
    out: set[str] = set()
    for entries in by_type.values():
        for entry, _ in entries:
            lc = scope.lifetime(entry)
            out |= set(scope.alloc_fns(lc))
            # `dtor` is `{shared, exclusive, fields}` (all lifecycle funcs);
            # `scope.drop_op_names` tolerates the legacy `{storage, fields}`
            # and flat-string shapes during migration.
            out |= set(scope.drop_op_names(lc.get("drop")))
            out |= set(scope.clone_op_names(lc))
            out |= set(scope.locking_op_names(lc.get("locking")))
    return out


def load_signatures(csv_path: Path | None) -> dict[str, str]:
    if not csv_path or not csv_path.exists():
        return {}
    out: dict[str, str] = {}
    with csv_path.open() as fh:
        for r in csv.DictReader(fh):
            if r.get("name") and r.get("signature"):
                out.setdefault(r["name"], r["signature"])
    return out


# ------------------------------------------------------- gate checks

def check_entry_shape(analysis_root: Path) -> list[Finding]:
    """Every types.json entry must carry a non-empty `type` tag.
    A comment-only / tag-less entry is malformed agent output that the
    rest of the pipeline (and load()) silently drops — flag it so it
    gets cleaned up rather than vanishing.
    """
    out: list[Finding] = []
    for f in analysis_root.rglob("types.json"):
        try:
            doc = json.loads(f.read_text())
        except (ValueError, OSError):
            out.append(Finding(
                "error", "entry-shape", str(f),
                "types.json does not parse as JSON",
            ))
            continue
        for i, e in enumerate(doc.get("types", [])):
            if not (e.get("name") or e.get("type")):
                rel = f
                out.append(Finding(
                    "error", "entry-shape", str(rel),
                    f"entry #{i} has no `type` tag (keys: "
                    f"{sorted(e.keys())[:4]})",
                ))
    return out


def _multidef_names(csv_path: Path | None) -> set[str]:
    """Names with >1 distinct def_file in t1/functions.csv — i.e.
    file-local (static) functions that legitimately recur under the
    same name in different TUs. Such a name is NOT a single symbol, so
    two types claiming "it" are claiming different functions and must
    not be flagged as an op-uniqueness collision.
    """
    if not csv_path or not csv_path.exists():
        return set()
    seen: dict[str, set[str]] = defaultdict(set)
    with csv_path.open() as fh:
        for r in csv.DictReader(fh):
            name = r.get("name")
            df = r.get("def_file")
            if name and df:
                seen[name].add(df)
    return {n for n, files in seen.items() if len(files) > 1}


def check_op_uniqueness(by_type, *, multidef=frozenset()) -> list[Finding]:
    exempt = lifetime_set(by_type)
    idx = op_index(by_type)
    out: list[Finding] = []
    for fn, tags in sorted(idx.items()):
        if fn in exempt:
            continue
        if fn in multidef:
            # File-local (static) function name that recurs across TUs;
            # the claimers are claiming distinct functions.
            continue
        if len(tags) > 1:
            claimers = sorted(tags)
            out.append(Finding(
                "error", "op-uniqueness", ",".join(claimers),
                f"non-lifecycle op '{fn}' claimed by {claimers}",
            ))
    return out


def check_ptr_invariants(by_type) -> list[Finding]:
    out: list[Finding] = []
    for tag, entries in by_type.items():
        for entry, _ in entries:
            for fld in entry.get("fields") or []:
                ptr = fld.get("ptr")
                if not isinstance(ptr, dict):
                    continue
                fname = fld.get("name", "?")
                owned = ptr.get("owned")
                borrowed = ptr.get("borrowed")
                string = ptr.get("string")
                array = ptr.get("array")
                mutable = ptr.get("mutable")
                const = "const" in (fld.get("type") or "")

                def bad(msg):
                    out.append(Finding(
                        "error", "ptr-invariants", tag,
                        f"field '{fname}': {msg}",
                    ))

                # Ownership dependencies are now STRUCTURAL: exclusive/shared nest
                # under `owned`, lifetime under `borrowed`, element-ownership under
                # `array.by_ref`. `owned` and `borrowed` may BOTH be non-null
                # (runtime-conditional dual ownership). Only shape validity, the
                # string/array mutex, borrowed-requires-lifetime, and const/mutable
                # remain.
                if array is not None:
                    if not isinstance(array, dict):
                        bad("array must be null or {by_val|by_ref}")
                    elif len([k for k in ("by_val", "by_ref") if array.get(k)]) != 1:
                        bad("array needs exactly one of by_val / by_ref")
                if string and array is not None:
                    bad("string and array both set (must be XOR)")
                if isinstance(borrowed, dict) and not borrowed.get("lifetime"):
                    bad("borrowed set but lifetime unset")
                if owned is not None and not isinstance(owned, dict):
                    bad("owned must be null or {exclusive, shared}")
                if const and mutable is True:
                    bad("const in type but mutable == true")
    return out


def run_gate(
    by_type, analysis_root, *, multidef=frozenset(),
) -> list[Finding]:
    """Run every gate check and return all findings."""
    return [
        *check_entry_shape(analysis_root),
        *check_op_uniqueness(by_type, multidef=multidef),
        *check_ptr_invariants(by_type),
    ]


# --------------------------------------------------- focused lenses

def report_focused(
    by_type, *, only_type, check_list, raw, sigs,
) -> int:
    """Single-type / candidate op-claim lens (advisory)."""
    idx = op_index(by_type)
    exempt = set() if raw else lifetime_set(by_type)

    if check_list is not None:
        flagged = [
            fn for fn in check_list
            if fn in idx and fn not in exempt
        ]
        print(
            f"# pre-write check: {len(flagged)}/{len(check_list)} "
            f"candidate(s) already claimed"
            + ("" if raw else " (lifecycle ops exempt; --raw to include)")
        )
    else:
        flagged = sorted(
            fn for fn, ts in idx.items()
            if only_type in ts and len(ts) > 1 and fn not in exempt
        )
        print(
            f"# post-write check for `{only_type}`: {len(flagged)} "
            f"non-lifecycle op(s) also claimed by another type"
            + ("" if raw else " (lifecycle ops exempt; --raw to include)")
        )

    for fn in flagged:
        claimers = sorted(idx[fn])
        others = [t for t in claimers if t != only_type] if only_type else claimers
        label = "also claimed by" if only_type else "claimed by"
        print(f"- {fn} :: {label} {others}")
        if sigs.get(fn):
            print(f"    signature: {sigs[fn]}")
    return 1 if flagged else 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--analysis-root", required=True, type=Path,
        help="<repo>/.crustify/analysis",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--type", dest="only_type",
        help="focused lens: post-write self-check on this type's ops",
    )
    g.add_argument(
        "--check", dest="check",
        help="focused lens: comma-separated candidate list; pre-write check",
    )
    ap.add_argument(
        "--raw", action="store_true",
        help="focused modes only: disable the lifecycle-op exemption",
    )
    ap.add_argument(
        "--functions-csv", type=Path, default=None,
        help="optional t1/functions.csv: annotate focused output with "
             "signatures; suppress static-name collisions in the gate",
    )
    ap.add_argument(
        "--warn-as-error", action="store_true",
        help="gate mode: treat warnings as errors for the exit code",
    )
    args = ap.parse_args()

    if not args.analysis_root.is_dir():
        print(
            f"error: analysis root not found: {args.analysis_root}",
            file=sys.stderr,
        )
        return 2

    by_type = load(args.analysis_root)

    # Focused lenses (single-type / candidate) — advisory, exit 1 if any
    # overlap so a caller can branch, but the agent treats it as a lens.
    if args.only_type or args.check:
        sigs = load_signatures(args.functions_csv)
        check_list = (
            [s.strip() for s in args.check.split(",") if s.strip()]
            if args.check else None
        )
        return report_focused(
            by_type,
            only_type=args.only_type,
            check_list=check_list,
            raw=args.raw,
            sigs=sigs,
        )

    # Whole-tree gate.
    multidef = _multidef_names(args.functions_csv)
    findings = run_gate(
        by_type, args.analysis_root, multidef=multidef,
    )

    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]

    for sev, group in (("error", errors), ("warn", warns)):
        if not group:
            continue
        print(f"# {len(group)} {sev}(s)")
        for f in group:
            print(f"{sev.upper()} {f.check} {f.type}: {f.message}")

    if not findings:
        print("# 0 findings — types tree consistent")

    fail = bool(errors) or (args.warn_as_error and bool(warns))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
