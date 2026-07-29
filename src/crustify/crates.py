"""Access layer for ``crates.json`` — the whole-repo crate/module decomposition.

``crates.json`` is the placement oracle: it maps every in-scope C symbol/type
to the unique Rust ``.rs`` that homes it. It is authored outside crustify — by
hand, or by an orchestrator driving ``prompts/scaffolder.md`` — against the
layout in ``templates/crates.json``; this module is the consumer-side read /
lookup / validate API the ``scaffold`` command uses against it. Schema
authority: ``templates/crates.json``.

Shape (eliding ``_comment`` keys)::

    crates.<crate> = {
      kind, in_tree, crate_path, sys_crate?, depends_on: [crate, ...],
      modules.<module> = {
        rust_path,
        rs.<path> = {                 # single source of truth; the module's
          tu: str | None,             # source + header surface derive from these
          headers: [str, ...],
          members: {functions: [], types: [], callbacks: [],
                    macros: [], globals: []},
        },
      },
    }

``tu`` is the ONE translation unit the ``.rs`` mirrors (null when there is no
in-tree definition site — a callback, an extern, an opaque struct, or any
entity of an out-of-tree library); ``headers`` are the headers its members are
declared or defined through. Scalar ``tu`` + plural ``headers`` is what lets a
same-stem ``foo.c``/``foo.h`` pair be ONE Rust module instead of colliding.

Field meaning: ``docs/schemas/crates.md``. Layout: ``templates/crates.json``.
"""

from __future__ import annotations

import json
from typing import Optional

# Member buckets. `macros` is homed for library attribution only — bindgen
# owns a macro's whole surface, so nothing anchors or wraps one.
_KINDS = ("functions", "types", "callbacks", "macros", "globals")


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

def lookup(doc: dict, name: str, *, file: str | None = None) -> Optional[dict]:
    """Resolve an entity to its home, or ``None`` on a miss. Returns
    ``{crate, module, rs, crate_path}``.

    Matches a member ``name`` in some ``.rs``; ``file`` narrows by provenance,
    matching EITHER the ``tu`` or any of the ``headers``. One qualifier rather
    than two because an entity is reached by whichever file the caller happens
    to hold — a header-defined struct is looked up by its header, a TU function
    by its ``.c``, and both may name the same ``.rs``.

    With no qualifier the first ``.rs`` containing ``name`` wins — pass ``file``
    to disambiguate a name collision (file-local statics in different TUs)."""
    for crate, c in (doc.get("crates") or {}).items():
        for mod, m in (c.get("modules") or {}).items():
            for rs, r in (m.get("rs") or {}).items():
                members = r.get("members") or {}
                if not any(name in (members.get(k) or []) for k in _KINDS):
                    continue
                if file is not None and file != r.get("tu") and file not in (
                        r.get("headers") or []):
                    continue
                return {"crate": crate, "module": mod, "rs": rs,
                        "crate_path": c.get("crate_path")}
    return None


# ----------------------------------------------------------------- validate

def validate(doc: dict) -> list[str]:
    """Return a list of error strings (``[]`` = valid):

      - **uniqueness** — each ``(kind, name, tu)`` homes in exactly one ``.rs``
        (two ``.rs`` claiming it = a duplicate Rust definition). The ``tu``
        component keeps same-named file-local statics in different TUs apart.
      - **deps DAG** — ``depends_on`` has no cycle (Rust forbids crate cycles).
      - **well-formedness** — every ``depends_on`` names a defined crate.
    """
    errors: list[str] = []
    crates = doc.get("crates") or {}

    seen: dict[tuple, str] = {}
    for crate, c in crates.items():
        for mod, m in (c.get("modules") or {}).items():
            for rs, r in (m.get("rs") or {}).items():
                tu = r.get("tu")
                for kind, names in (r.get("members") or {}).items():
                    for nm in names or []:
                        key = (kind, nm, tu)
                        if key in seen:
                            errors.append(
                                f"duplicate: {kind} {nm!r} (tu={tu}) in "
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
