"""Access layer for ``crates.json`` — the whole-repo crate/module decomposition.

``crates.json`` is the placement oracle: it maps every in-scope C symbol/type
to the unique Rust ``.rs`` that homes it. ``CrustifyScaffolder`` writes the file
(filling ``templates/crates.json``'s layout); this module is the consumer-side
read / lookup / validate API the ``scaffold`` command uses against it. Schema
authority: ``templates/crates.json``.

Shape (eliding ``_comment_*`` keys)::

    crates.<crate> = {
      kind, in_tree, crate_path, sys_crate?, depends_on: [crate, ...],
      modules.<module> = {
        rust_path,
        rs.<path> = {                 # single source of truth; the module's
          def_file: str | None,       # TU list + header surface derive from these
          decl_files: [str, ...],
          members: {functions: [], types: [], macros: [], globals: []},
        },
      },
    }
"""

from __future__ import annotations

import json
from typing import Optional

_KINDS = ("functions", "types", "macros", "globals")


# --------------------------------------------------------------- load / save

def load(layout) -> dict:
    """Load ``crates.json`` (the repo-root artifact). An absent file yields an
    empty ``{"crates": {}}`` shell; a malformed file raises."""
    path = layout.crates_json
    if not path.exists():
        return {"crates": {}}
    return json.loads(path.read_text())


def save(layout, doc: dict) -> None:
    layout.crates_json.write_text(json.dumps(doc, indent=2) + "\n")


# ------------------------------------------------------------------- lookup

def lookup(
    doc: dict, name: str, *,
    def_file: str | None = None, decl_file: str | None = None,
) -> Optional[dict]:
    """Resolve an entity to its home, or ``None`` on a miss. Returns
    ``{crate, module, rs, crate_path}``.

    Matches a member ``name`` in some ``.rs`` whose provenance matches the given
    ``def_file`` (exact) or ``decl_file`` (∈ ``decl_files``). With neither
    qualifier the first ``.rs`` containing ``name`` wins — pass ``def_file`` /
    ``decl_file`` to disambiguate a name collision (file-local statics)."""
    for crate, c in (doc.get("crates") or {}).items():
        for mod, m in (c.get("modules") or {}).items():
            for rs, r in (m.get("rs") or {}).items():
                members = r.get("members") or {}
                if not any(name in (members.get(k) or []) for k in _KINDS):
                    continue
                if def_file is not None and r.get("def_file") != def_file:
                    continue
                if decl_file is not None and decl_file not in (r.get("decl_files") or []):
                    continue
                return {"crate": crate, "module": mod, "rs": rs,
                        "crate_path": c.get("crate_path")}
    return None


# ----------------------------------------------------------------- validate

def validate(doc: dict) -> list[str]:
    """Return a list of error strings (``[]`` = valid):

      - **uniqueness** — each ``(kind, name, def_file)`` homes in exactly one
        ``.rs`` (two ``.rs`` claiming it = a duplicate Rust definition).
      - **deps DAG** — ``depends_on`` has no cycle (Rust forbids crate cycles).
      - **well-formedness** — every ``depends_on`` names a defined crate.
    """
    errors: list[str] = []
    crates = doc.get("crates") or {}

    seen: dict[tuple, str] = {}
    for crate, c in crates.items():
        for mod, m in (c.get("modules") or {}).items():
            for rs, r in (m.get("rs") or {}).items():
                df = r.get("def_file")
                for kind, names in (r.get("members") or {}).items():
                    for nm in names or []:
                        key = (kind, nm, df)
                        if key in seen:
                            errors.append(
                                f"duplicate: {kind} {nm!r} (def_file={df}) in "
                                f"{seen[key]} AND {crate}/{mod}/{rs}")
                        else:
                            seen[key] = f"{crate}/{mod}/{rs}"

    names = set(crates)
    adj: dict[str, list[str]] = {}
    for crate, c in crates.items():
        deps = c.get("depends_on") or []
        for d in deps:
            if d not in names:
                errors.append(f"{crate}.depends_on names undefined crate {d!r}")
        adj[crate] = [d for d in deps if d in names]
    cyc = _find_cycle(adj)
    if cyc:
        errors.append("dependency cycle: " + " -> ".join(cyc))

    return errors


def _find_cycle(adj: dict[str, list[str]]) -> list[str] | None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        color[n] = GRAY
        stack.append(n)
        for m in adj.get(n, []):
            if color.get(m, WHITE) == GRAY:
                return stack[stack.index(m):] + [m]
            if color.get(m, WHITE) == WHITE:
                r = dfs(m)
                if r:
                    return r
        stack.pop()
        color[n] = BLACK
        return None

    for n in adj:
        if color[n] == WHITE:
            r = dfs(n)
            if r:
                return r
    return None
