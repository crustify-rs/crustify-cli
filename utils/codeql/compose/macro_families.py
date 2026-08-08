"""macro_families.py — template-by-macro families, and the synthetic type that
represents each one.

A macro emitting a whole `typedef struct {…} name;` mints a family of
same-shaped types that C links in no way at all: no cast, no common tag, no
base (see `edges/macro_generated_types.ql` for why the relation has to be
extracted rather than inferred). Downstream that means each instance is wrapped
as an unrelated Rust type, when the family wants ONE generic plus aliases.

This module turns the extracted relation into a node the rest of the pipeline
can already handle: one **synthetic type per family**, carrying

    kind:            "macro_generator"   <- names no C type; has no layout
    macro_generator: "<MACRO>"           <- the same marker, explicit
    generates:       [instance tags]
    fields:          []                  <- deliberately empty

and `generated_by: "<MACRO>"` on each instance.

`generated_by` / `generates` is a DIRECTED relation, unlike `casted`. A cast
says nothing about which side depends on which, so `deps_dag` has to recover
direction with a cast-centrality heuristic and a strict `>` guard; an instance
always depends on its generator, so the edge is a fact and needs no inference
(and cannot invert when an instance happens to carry genuine casts of its own).

**Threshold.** A macro that mints one type is a definition, not a generator, so
a family needs >= 2 members. This is an explicit filter rather than an emergent
property of some degree comparison: an invisible guard is a worse guard.

**Scope** is the macro's own — the family is Rust we write, homed where the
macro is defined, not C we bind.
"""
from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

try:
    from . import scope as _scope
except ImportError:                       # script execution
    import scope as _scope                # type: ignore

#: The `kind` a synthetic generator carries. Not a C aggregate — a consumer
#: that reaches for its layout must find nothing rather than something wrong.
GENERATOR_KIND = "macro_generator"

#: A macro minting fewer than this many types is a definition, not a template.
MIN_MEMBERS = 2

_CSV = "macro_generated_types.csv"


def load(codeql_dir: Path) -> dict[str, dict[str, Any]]:
    """``{macro: {"def_file", "members": [(tag, def_file)]}}`` for families of
    at least :data:`MIN_MEMBERS`. Empty when the table is absent — the relation
    is additive, so a tree extracted before the query existed simply has no
    families rather than failing."""
    p = Path(codeql_dir) / "t2" / _CSV
    if not p.is_file():
        return {}
    fams: dict[str, dict[str, Any]] = {}
    members: dict[str, list] = collections.defaultdict(list)
    gen_file: dict[str, str] = {}
    for r in _scope.load_csv(p):
        m = r.get("generator_macro")
        t = r.get("type_name")
        if not (m and t):
            continue
        members[m].append((t, r.get("type_def_file") or ""))
        gen_file.setdefault(m, r.get("generator_def_file") or "")
    for m, mem in members.items():
        uniq = sorted(set(mem))
        if len(uniq) < MIN_MEMBERS:
            continue
        fams[m] = {"def_file": gen_file.get(m, ""), "members": uniq}
    return fams


def synthetic_type_rows(fams: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """The families as `types.csv`-shaped rows.

    Every consumer that builds its type universe from `types.csv` — the scope
    manifest, the type manifest, the dag's own index — has to see the synthetic
    or the family is invisible to it. `deps_dag` in particular drops a dep whose
    target is not in its node registry, so a generator missing from the universe
    silently loses every ordering edge into it.

    `def_file` is the MACRO's defining file: that is where the generic is homed,
    and it is what gives the synthetic a scope (a tag with no file classifies as
    neither port nor wrap, which is exactly the state that makes an entity
    unschedulable).
    """
    return [
        {"name": m, "kind": GENERATOR_KIND, "def_file": f["def_file"],
         "decl_files": f["def_file"], "aliases": "",
         "unaliased_kind": GENERATOR_KIND}
        for m, f in sorted(fams.items())
    ]


def generated_by(fams: dict[str, dict[str, Any]]) -> dict[tuple, str]:
    """``{(tag, def_file): macro}`` — the reverse index, for stamping instances."""
    out: dict[tuple, str] = {}
    for m, f in fams.items():
        for tag, df in f["members"]:
            out[(tag, df)] = m
    return out
