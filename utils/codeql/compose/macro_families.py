"""macro_families.py — template-by-macro families.

A macro emitting a whole `typedef struct {…} name;` mints a family of
same-shaped types that C links in no way at all: no cast, no common tag, no
base (see `edges/macro_generated_types.ql` for why the relation has to be
extracted rather than inferred). Downstream that means each instance is wrapped
as an unrelated Rust type, when the family wants ONE generic plus aliases.

The generator needs no node of its own: the MACRO is already an entity. So the
family hangs off it as `generates`, with `generated_by` on each instance. An
earlier cut minted a synthetic type per family and collided with the macro's own
node on `(name, defined_in)` — `Node.key` carries no `node_kind`.

`generated_by` / `generates` is a DIRECTED relation, unlike `casted`. A cast
says nothing about which side depends on which, so `deps_dag` has to recover
direction with a cast-centrality heuristic and a strict `>` guard; an instance
always depends on its generator, so the edge is a fact and needs no inference
(and cannot invert when an instance happens to carry genuine casts of its own).

**No count threshold.** Whether a macro minted one type or twenty in the
extracted build says nothing about whether it is a template — conditional
compilation decides that. The relation is emitted for every minting macro and
the judgement is the agent's.

**Scope** is the macro's own. `import_closure` admits a generator regardless of
call-site reachability (it expands at file scope, never from a function body),
and `wrap._is_macro` exempts it from "macros are bindgen's" — the generic its
instances alias is Rust this stage writes.
"""
from __future__ import annotations

import collections
from pathlib import Path
from typing import Any

try:
    from . import scope as _scope
except ImportError:                       # script execution
    import scope as _scope                # type: ignore

#: Every minting macro is a generator, however many types this BUILD saw it
#: mint. A count threshold looked principled and is not: it is a function of the
#: extracted configuration, not of the C. `entry_short` / `entry_long`
#: (`src/libgit2/index.c`) each mint two types in source -- the SHA256 pair sits
#: behind `#ifdef GIT_EXPERIMENTAL_SHA256`, which this build does not define --
#: so a >= 2 rule called them definitions here and would call them generators
#: under another cmake flag. Emit the relation and let the wrapper agent decide
#: whether a family of one earns a generic.
MIN_MEMBERS = 1

_CSV = "macro_generated_types.csv"


def load(codeql_dir: Path) -> dict[str, dict[str, Any]]:
    """``{macro: {"def_file", "members": [(tag, def_file)]}}`` for every minting
    macro. Empty when the table is absent — the relation
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


def generated_by(fams: dict[str, dict[str, Any]]) -> dict[tuple, str]:
    """``{(tag, def_file): macro}`` — the reverse index, for stamping instances."""
    out: dict[tuple, str] = {}
    for m, f in fams.items():
        for tag, df in f["members"]:
            out[(tag, df)] = m
    return out
