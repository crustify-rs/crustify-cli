#!/usr/bin/env python3
"""Regenerate utils/codeql/port_scope.qll from a target's scope.json.

Reads the target's target file list (``scope.json``'s
``port.files``, emitted by ``compose.scope_manifest``) and emits a
CodeQL library file with the path set inlined as a string predicate.
The library exports:

  - ``portFile(File f)`` — predicate that holds when ``f``'s
    repository-relative path is in the target set.
  - ``portPath(string p)`` — predicate that holds when ``p`` (a
    repository-relative path string) is in the target set. Used
    by callers that have a path string rather than a File entity.

Usage:

    python3 utils/codeql/generate_port_scope.py <scope.json>

Where ``<scope.json>`` is the target's scope manifest (e.g.
``crustify/targets/<target>/scope.json``).

The script writes ``utils/codeql/port_scope.qll`` (always in the same
directory as the script — the .qll lives alongside the queries that
import it). Re-run whenever the target's port scope changes. Standalone
dev tool: not invoked by the pipeline, but the .qll it produces IS
imported by a live query (edges/function_calls.ql).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def collect_port_paths(scope_json: Path) -> list[str]:
    """Read scope.json's `port.files` and return the sorted unique path
    list. Schema: ``{"target": {"files": ["a/b.c", ...], ...}}`` —
    mirrors ``compose.scope.load_target_paths``. Defensively also accepts
    a list of ``{"path": ...}`` entries or bare strings under `files`."""
    data = json.loads(scope_json.read_text())
    port = data.get("port", {}) if isinstance(data, dict) else {}
    entries = port.get("files", []) if isinstance(port, dict) else []
    paths: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            p = entry.get("path")
            if p:
                paths.add(p)
        elif isinstance(entry, str):
            paths.add(entry)
    return sorted(paths)


def render_qll(paths: list[str]) -> str:
    """Render the port_scope.qll source from the path list."""
    header = '''/**
 * GENERATED FILE — do not hand-edit.
 *
 * Regenerate with:
 *   python3 utils/codeql/generate_port_scope.py <target>
 *
 * Source of truth: the target's scope.json (its port.files)
 *
 * Exports:
 *   - portFile(File f)   — f's relative path is in the target set.
 *   - portPath(string p) — p is in the target set.
 *
 * Consumed by every query in utils/codeql/ that needs scope
 * partitioning. The path set is inlined here so queries are
 * self-contained at evaluation time; no --external flag required.
 */
import cpp

'''

    # Emit the path set as a string predicate. CodeQL idiom:
    # ``string portPath()`` is a multi-result predicate that returns
    # each path in turn; callers join with ``= ...`` to test membership.
    body_lines = [
        "predicate portPath(string p) {",
    ]
    if paths:
        # Generate `p = "path1" or p = "path2" or ...` with proper
        # CodeQL string escaping (double quotes, backslashes).
        clauses = []
        for path in paths:
            escaped = path.replace("\\", "\\\\").replace('"', '\\"')
            clauses.append(f'  p = "{escaped}"')
        body_lines.append(" or\n".join(clauses))
    else:
        # Empty set: predicate is unsatisfiable.
        body_lines.append("  none()")
    body_lines.append("}")
    body = "\n".join(body_lines)

    portfile = '''

predicate portFile(File f) {
  portPath(f.getRelativePath())
}
'''
    return header + body + portfile


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "scope_json",
        type=Path,
        help="Path to the target's scope.json (e.g. "
             "crustify/targets/<target>/scope.json)",
    )
    args = ap.parse_args()

    scope_json = args.scope_json
    if not scope_json.exists():
        print(f"error: {scope_json} not found. Run "
              f"the scope composer first (`crustify.scope.build`).",
              file=sys.stderr)
        sys.exit(1)

    paths = collect_port_paths(scope_json)
    if not paths:
        print(f"warning: {scope_json} resolves to an empty port path set; "
              f"port_scope.qll will be unsatisfiable.", file=sys.stderr)

    output = render_qll(paths)
    target_qll = Path(__file__).parent / "port_scope.qll"
    target_qll.write_text(output)
    print(f"wrote {target_qll} — {len(paths)} target-section paths inlined")


if __name__ == "__main__":
    main()
