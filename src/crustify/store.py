"""store.py — `ownership-store.json`, the authored half of the analysis.

Holds only what no composer can infer: ownership (`ptr`), lifecycle
(`lifetime`), refcount and locking bindings, the agent's notes, and enough to
key each record. A field's `type` / `ref` / `array`, an argument's `position` /
`const` / `depth`, `kind`, `declared_in`, footprints and `casted` are composed
on demand and merged in at read time by :mod:`crustify.manifests`, so a
consumer sees a whole record.

    types[]    name, defined_in                    <- key
               _comment_agent?
               fields[]  name                      <- key
                         ptr? refcount? locked_by? _comment_agent?

    symbols[]  name, defined_in, variant?          <- key
               lifetime? forks? callsites? _comment_agent?
               ptr_args[]  name                    <- key
                           ptr
               ptr_ret?    ptr

A pointer argument keys on `name`, not `position`: a signature edit shifts
positions and would re-attach an ownership block to the wrong argument.

Repo-tier: an ownership judgement is a fact about the C, not about which
target is building.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

_COMMENT = (
    "Authored analysis: ownership, lifecycle, refcount and locking judgements "
    "that no composer can derive, keyed to the C entities they describe. The "
    "structural half of each record (field layout, signatures, footprints, "
    "casts) is composed from the CodeQL tables on demand and merged in at read "
    "time, so a re-extract rewrites nothing here. Written only by "
    "`crustify-oracle <target> query {types,symbols} --update`; read through "
    "crustify.manifests. Hand-edit at your own risk: "
    "`--update` validates a submission against the composed skeleton (unknown "
    "field, wrong kind, ptr-invariant violation) and this file does not."
)

#: Agent-owned keys on a type's field, mirroring `query._FIELD_AGENT_KEYS`
#: plus the free-text note.
FIELD_KEYS = ("ptr", "refcount", "locked_by", "_comment_agent")
#: Agent-owned keys at a symbol entry's top level.
SYM_KEYS = ("lifetime", "forks", "ptr", "locked_by", "_comment_agent")
#: A callback FORK additionally records `callsites`: the invokers that realize
#: THAT variant's contract. The composer sees one C declaration and puts every
#: invoker on the primary, so this is authored; it is replayed into the
#: materialized entry's `used_by.call`.
FORK_KEYS = ("callsites",)

TypeKey = tuple[str, str]
SymKey = tuple[str, str, int]


def path(layout) -> Path:
    return layout.root / "ownership-store.json"


def type_key(rec: dict) -> TypeKey:
    return (rec.get("name") or rec.get("type"), rec.get("defined_in") or "")


def sym_key(rec: dict) -> SymKey:
    return (rec["name"], rec.get("defined_in") or "", rec.get("variant") or 0)


def empty() -> dict:
    return {"_comment": _COMMENT, "types": [], "symbols": []}


def load(layout) -> dict:
    """The store, or an empty one when it does not exist yet. A missing store
    is the legitimate state of a fresh repo -- nothing has been analysed."""
    p = path(layout)
    if not p.is_file():
        return empty()
    try:
        doc = json.loads(p.read_text())
    except ValueError as ex:
        raise SystemExit(f"ownership-store: {p} is not valid JSON: {ex}")
    doc.setdefault("types", [])
    doc.setdefault("symbols", [])
    return doc


def normalize(doc: dict) -> dict:
    """Canonical ordering: records by key, nested lists by name. The overlay is
    name-keyed, so list order carries no meaning."""
    doc["types"] = sorted(doc.get("types") or [], key=type_key)
    doc["symbols"] = sorted(doc.get("symbols") or [], key=sym_key)
    for r in doc["types"]:
        if r.get("fields"):
            r["fields"] = sorted(r["fields"], key=lambda x: x.get("name") or "")
    for r in doc["symbols"]:
        if r.get("ptr_args"):
            r["ptr_args"] = sorted(r["ptr_args"], key=lambda x: x.get("name") or "")
    return doc


def index(doc: dict) -> tuple[dict[TypeKey, dict], dict[SymKey, dict]]:
    """Key -> record, for overlaying onto a composed skeleton."""
    return ({type_key(r): r for r in doc.get("types") or []},
            {sym_key(r): r for r in doc.get("symbols") or []})


# ------------------------------------------------------------------- overlay

def overlay_type(entry: dict, rec: dict | None) -> dict:
    """Merge a stored type record onto its composed skeleton entry, in place.

    The skeleton is authoritative for structure; the store contributes only its
    own keys.
    """
    if not rec:
        return entry
    if rec.get("_comment_agent"):
        entry["_comment_agent"] = rec["_comment_agent"]
    by_name = {f.get("name"): f for f in (entry.get("fields") or [])
               if isinstance(f, dict)}
    for sf in rec.get("fields") or []:
        dst = by_name.get(sf.get("name"))
        if dst is None:
            continue
        for k in FIELD_KEYS:
            if k in sf:
                dst[k] = sf[k]
    return entry


def overlay_sym(entry: dict, rec: dict | None) -> dict:
    """Merge a stored symbol record onto its composed skeleton entry, in place.
    Pointer arguments are keyed on NAME."""
    if not rec:
        return entry
    for k in SYM_KEYS:
        if k in rec:
            entry[k] = rec[k]
    args = {a.get("name"): a for a in (entry.get("ptr_args") or [])
            if isinstance(a, dict)}
    for sa in rec.get("ptr_args") or []:
        dst = args.get(sa.get("name"))
        if dst is not None:
            dst["ptr"] = sa.get("ptr")
    if rec.get("ptr_ret") and isinstance(entry.get("ptr_ret"), dict):
        entry["ptr_ret"]["ptr"] = rec["ptr_ret"].get("ptr")
    return entry


# --------------------------------------------------------------------- write

def update(layout, apply: Callable[[dict], Any]) -> None:
    """Serialize a read-modify-write of the store against concurrent
    ``--update`` processes, then install the result atomically.

    The exclusive lock is held on the PARENT DIRECTORY fd, not the data file:
    the commit is an ``os.replace``, which swaps in a new inode, so a lock on
    the data file's own fd would not serialize a writer that opens it fresh.
    The directory inode never moves. The store is (re-)read only after the lock
    is held. ``apply(doc)`` mutates the doc in place or raises ``SystemExit`` to
    reject, applying nothing.

    One lock for the whole repo. A read-modify-write measures 0.7 ms against
    submissions that arrive a handful of times per agent run.
    """
    import fcntl

    from crustify.cache import atomic_write

    p = path(layout)
    p.parent.mkdir(parents=True, exist_ok=True)
    dirfd = os.open(str(p.parent), os.O_RDONLY)
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        doc = load(layout)
        apply(doc)
        atomic_write(p, json.dumps(normalize(doc), indent=1) + "\n")
    finally:
        fcntl.flock(dirfd, fcntl.LOCK_UN)
        os.close(dirfd)


def upsert_type(doc: dict, name: str, defined_in: str | None) -> dict:
    """The store record for a type, created empty if absent."""
    k = (name, defined_in or "")
    for r in doc["types"]:
        if type_key(r) == k:
            return r
    r = {"name": name, "defined_in": defined_in}
    doc["types"].append(r)
    return r


def upsert_sym(doc: dict, name: str, defined_in: str | None,
               variant: int = 0) -> dict:
    """The store record for a symbol (or callback fork), created if absent."""
    k = (name, defined_in or "", variant or 0)
    for r in doc["symbols"]:
        if sym_key(r) == k:
            return r
    r = {"name": name, "defined_in": defined_in}
    if variant:
        r["variant"] = variant
    doc["symbols"].append(r)
    return r
