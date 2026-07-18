"""Union-by-key merge with field-level additions for repo-root manifests.

Per-source-file manifests at `<repo_root>/.crustify/analysis/<dir>/<stem>/`
accumulate entries across target invocations: each target's composer
run only reaches a slice of each manifest dir, and the union of those
slices grows over time as new targets are introduced.

Across-target evolution is handled by **field-level merge** rather
than the older "existing entry wins, ignore new" semantic:

  - **New keys** (entries not in the existing manifest) are appended.
  - **Composer-owned values are refreshed** (a cheap `--compose-only` is a
    deterministic structural refresh): a type entry's composer-owned top-level
    keys (`_TYPE_COMPOSER_KEYS` — ``kind`` / ``declared_in`` / ``defined_in`` /
    ``casted`` / footprints) are overwritten from the new run, and each field's
    composer-owned structure (``name`` / ``type`` / ``ref`` / ``array`` + the
    *presence* of ``ptr``) is overlaid by `_merge_fields` → `_overlay_field`.
    This is what lets a composer fix land without a full `--reset` (e.g. a
    typedef'd function pointer that used to collapse to a bare scalar now
    surfaces as a proper pointer field).
  - **Agent-owned values are preserved**: the ``ptr`` ownership *block*
    contents on each field, and the entry-level ``lifetime`` / ``ops`` —
    `_overlay_field` keeps the existing ``ptr`` when the field is still a
    pointer, and the composer never emits ``lifetime`` / ``ops`` values, so the
    add-missing rule leaves them untouched.
  - **Grow-only composer sets** are set-UNIONED rather than frozen, so a
    record accumulates across runs (and across a wrap→port promotion, whose
    port re-emit is strictly richer): ``fields[]`` on type entries (by field
    name) and ``used_by`` / ``depends_on`` on symbol entries
    (`_merge_used_by` / `_merge_depends_on`). Symbol composer keys
    (``ptr_args`` / ``ptr_ret``) stay under the add-missing rule as before.
  - **A full reset** (drop agent annotations too) remains the explicit
    `--reset`, which deletes matching entries before the compose.

This module is intentionally narrow — no hashing, no staleness
detection beyond the field-level merge above.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from . import scope
except ImportError:  # pragma: no cover - runtime path shim
    import scope  # type: ignore

Key = tuple[str, str]
Entry = dict[str, Any]
KeyFn = Callable[[Entry], Key]


def symbol_key(entry: Entry) -> Key:
    """Stable identity for symbol entries: ``(name, defined_in or "", variant)``.

    ``variant`` (default 0) keys apart agent-created **callback forks** — a
    function-pointer typedef whose invokers realize genuinely different
    ownership contracts is split into several ``kind:"callback"`` entries with
    the SAME ``name``/``type`` but distinct ``ptr_args``/``ptr_ret`` (one per
    Rust wrapper). The composer only ever emits the primary (variant 0), so on
    re-compose the primary updates in place while forks (variant>=1) survive the
    merge untouched."""
    return (entry["name"], entry.get("defined_in") or "", entry.get("variant") or 0)


def type_key(entry: Entry) -> Key:
    """Stable identity for type entries: ``(name, defined_in or "")`` (the
    record identifier, `type` on un-migrated records)."""
    return (scope.entry_tag(entry), entry.get("defined_in") or "")


def _overlay_field(existing: dict, incoming: dict) -> dict:
    """Overlay a field's **composer-owned structure** from `incoming` while
    preserving the **agent-owned** ``ptr`` ownership block from `existing`.

    Composer owns the structural shape — ``name`` / ``type`` / ``ref`` /
    (top-level) ``array`` and the *presence* of the ``ptr`` skeleton; the agent
    owns the ``ptr`` block *contents* (``owned`` / ``borrowed`` / ``string`` /
    ``mutable`` / ``container`` / ``owned_elem`` / ``note`` / …). So a
    ``--compose-only`` refresh re-derives structure — picking up composer fixes
    (e.g. a typedef'd function pointer that used to collapse to a bare scalar
    now surfaces as a pointer) — without discarding ownership analysis:

      - take composer structure verbatim from ``incoming``;
      - ``ptr``: keep the agent-filled block from ``existing`` when the field is
        *still* a pointer; seed ``incoming``'s null skeleton when it *became*
        one (agent fills later); drop it when it *stopped* being a pointer.
    """
    out = dict(incoming)                       # composer structure authoritative
    if "ptr" in incoming and "ptr" in existing:
        out["ptr"] = existing["ptr"]           # preserve agent-filled ownership
    return out


def _merge_fields(existing_fields: list, incoming_fields: list) -> list:
    """Merge a type entry's ``fields[]`` array **by field name**, refreshing
    composer-owned structure while preserving each field's agent ``ptr`` block.

      - field present in both → ``_overlay_field`` (composer structure from
        incoming, agent ``ptr`` from existing);
      - field only in incoming → append it (composer skeleton, null ``ptr``);
      - field only in existing → keep it (composer didn't re-emit — e.g. a
        wrap-scope narrowing after a fuller port-scope run; grow-only).

    Order follows the incoming (composer) declaration order, with any
    existing-only fields appended after — deterministic and stable.
    """
    if not isinstance(existing_fields, list) or not isinstance(incoming_fields, list):
        return existing_fields
    by_name = {
        f["name"]: f for f in existing_fields
        if isinstance(f, dict) and "name" in f
    }
    merged: list = []
    seen: set = set()
    for inc in incoming_fields:
        if not isinstance(inc, dict) or "name" not in inc:
            merged.append(inc)
            continue
        name = inc["name"]
        ex = by_name.get(name)
        merged.append(_overlay_field(ex, inc) if isinstance(ex, dict) else inc)
        seen.add(name)
    # Preserve any existing fields the composer didn't re-emit this run.
    for ex in existing_fields:
        if isinstance(ex, dict) and ex.get("name") not in seen:
            merged.append(ex)
    return merged


def _append_new(existing: list, incoming: list) -> list:
    """Order-preserving union of a string list: keep `existing` verbatim,
    append items in `incoming` not already present (incoming order). When
    ``incoming ⊆ existing`` the result is `existing` unchanged — the
    grow-only, churn-free property that keeps a re-compose diff signal-only.
    Either argument may be ``None`` (treated as empty)."""
    out = list(existing or [])
    seen = set(out)
    for x in (incoming or []):
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def _union_str_list(a: Any, b: Any) -> Any:
    """Order-preserving union of two string lists. Either side may be ``None``
    (a global's ``used_by.call`` is null — it has accessors, not callers);
    ``None ∪ None`` stays ``None``, ``None ∪ [..]`` collapses to the list."""
    if a is None and b is None:
        return None
    return _append_new(a, b)


def _merge_used_by(existing: dict, incoming: dict) -> dict:
    """Union a symbol's ``used_by`` (``{call, ref}``) across runs. Both are
    whole-codebase sets (scope-agnostic), so for one DB this is idempotent;
    the union matters across target invocations and across a wrap→port
    promotion (a wrap function has no ``used_by`` at all — handled by the
    add-missing path in `_merge_entry`; once present it only grows).
    Order-preserving so an idempotent re-run produces no churn."""
    out = dict(existing)
    for k in ("call", "ref"):
        if k in existing or k in incoming:
            out[k] = _union_str_list(existing.get(k), incoming.get(k))
    return out


def _merge_dep_syms(existing: list, incoming: list) -> list:
    """Union ``depends_on.syms`` (``[{name, defined_in, declared_in}]``) by
    ``(name, defined_in)``, **preserving existing order** and appending
    new-only callees in incoming order; ``declared_in`` grows append-only on
    collision. ``incoming ⊆ existing`` ⇒ unchanged (the composer emits this
    list already sorted, so first emission is sorted and stays so)."""
    out: list = []
    by_key: dict[tuple[str, str], dict] = {}
    for s in (existing or []):
        if not isinstance(s, dict) or "name" not in s:
            out.append(s)
            continue
        k = (s["name"], s.get("defined_in") or "")
        by_key[k] = s
        out.append(s)
    for s in (incoming or []):
        if not isinstance(s, dict) or "name" not in s:
            continue
        k = (s["name"], s.get("defined_in") or "")
        prev = by_key.get(k)
        if prev is None:
            entry = {
                "name": s["name"],
                "defined_in": s.get("defined_in"),
                "declared_in": list(s.get("declared_in") or []),
            }
            by_key[k] = entry
            out.append(entry)
        else:
            prev["declared_in"] = _append_new(
                prev.get("declared_in"), s.get("declared_in"),
            )
    return out


def _merge_dep_types(existing: list, incoming: list) -> list:
    """Union ``depends_on.types`` (``[{type, fields}]``) by ``type`` tag,
    **preserving existing (source-faithful first-encounter) order** and
    appending new-only tags in incoming order; each tag's ``fields`` (the body
    field-reach list) grows append-only. This is what upgrades a wrap
    function's SIGNATURE-only deps to a port function's SIGNATURE+BODY deps on
    re-compose (the wrap entry carries the signature types with empty
    ``fields``; the port re-emit adds the body-reach fields and the body-only
    types). ``incoming ⊆ existing`` ⇒ unchanged."""
    out: list = []
    by_tag: dict[str, dict] = {}
    for t in (existing or []):
        if not isinstance(t, dict) or not scope.entry_tag(t):
            out.append(t)
            continue
        by_tag[scope.entry_tag(t)] = t
        out.append(t)
    for t in (incoming or []):
        if not isinstance(t, dict) or not scope.entry_tag(t):
            continue
        prev = by_tag.get(scope.entry_tag(t))
        if prev is None:
            entry = {"name": scope.entry_tag(t), "fields": list(t.get("fields") or [])}
            by_tag[scope.entry_tag(t)] = entry
            out.append(entry)
        else:
            prev["fields"] = _append_new(prev.get("fields"), t.get("fields"))
    return out


def _merge_depends_on(existing: dict, incoming: dict) -> dict:
    """Union a symbol's ``depends_on`` (``{syms, types}``) across runs.

    Monotonic growth, mirroring the type ``fields[]`` merge: a symbol first
    seen wrap-scope (signature-only deps) that a later run re-emits port-scope
    (signature + body deps) gains the body edges instead of having them
    discarded by the plain existing-wins rule. The edge sets are
    composer-authored and scope-agnostic — no agent annotation lives here, so
    union is always safe."""
    out = dict(existing)
    out["syms"] = _merge_dep_syms(existing.get("syms") or [], incoming.get("syms") or [])
    out["types"] = _merge_dep_types(existing.get("types") or [], incoming.get("types") or [])
    return out


# Composer-owned top-level keys on a **type** entry: deterministic, re-derived
# from CodeQL every run, so a re-compose overlays them (picking up composer
# fixes / cast-graph or footprint updates). The agent-owned keys — ``lifetime``
# and ``ops`` — are absent here and thus preserved. Symbol entries lack these
# keys, so the overlay is a no-op for syms (their composer-owned ``ptr_args`` /
# ``ptr_ret`` stay under the add-missing rule as before).
_TYPE_COMPOSER_KEYS = (
    "typedef", "kind", "declared_in", "defined_in",
    "casted", "opaque_in", "non_opaque_in",
)


def _merge_entry(existing: Entry, incoming: Entry) -> Entry:
    """Merge `incoming` into `existing`, refreshing composer-owned values while
    preserving agent-owned ones. Concretely:

      - ``fields[]`` (type entries) — ``_merge_fields``: composer structure
        overlaid per field, agent ``ptr`` blocks preserved;
      - ``used_by`` / ``depends_on`` (symbol entries) — set-unioned
        (`_merge_used_by` / `_merge_depends_on`), so a wrap→port promotion
        upgrades signature-only deps to signature+body deps in place;
      - composer-owned type-entry keys (`_TYPE_COMPOSER_KEYS`) — overwritten
        from ``incoming`` (deterministic re-derivation);
      - every other existing key (agent-owned ``lifetime`` / ``ops``, and any
        symbol composer key) — kept; incoming-only keys are added.

    Returns the updated `existing` dict (mutated in-place for clarity).
    """
    if isinstance(existing.get("fields"), list) and isinstance(incoming.get("fields"), list):
        existing["fields"] = _merge_fields(existing["fields"], incoming["fields"])
    if isinstance(existing.get("used_by"), dict) and isinstance(incoming.get("used_by"), dict):
        existing["used_by"] = _merge_used_by(existing["used_by"], incoming["used_by"])
    if isinstance(existing.get("depends_on"), dict) and isinstance(incoming.get("depends_on"), dict):
        existing["depends_on"] = _merge_depends_on(existing["depends_on"], incoming["depends_on"])
    # Refresh composer-owned type-entry structure (no-op for symbol entries).
    for k in _TYPE_COMPOSER_KEYS:
        if k in incoming:
            existing[k] = incoming[k]
    for k, v in incoming.items():
        if k not in existing:
            existing[k] = v
    return existing


def merge_entries(
    existing: list[Entry],
    new: Iterable[Entry],
    *,
    key: KeyFn,
) -> tuple[list[Entry], int, int]:
    """Field-level union of `new` into `existing`.

    Returns ``(merged_list, added_count, updated_count)`` where
    `added_count` is the number of entirely new entries appended and
    `updated_count` is the number of existing entries gained new
    fields.
    """
    by_key: dict[Key, Entry] = {key(e): e for e in existing}
    added = 0
    updated = 0
    for entry in new:
        k = key(entry)
        if k in by_key:
            before = len(by_key[k])
            _merge_entry(by_key[k], entry)
            if len(by_key[k]) != before:
                updated += 1
        else:
            by_key[k] = dict(entry)
            existing.append(by_key[k])
            added += 1
    return existing, added, updated


def merge_manifest_file(
    path: Path,
    new_manifest: dict[str, Any],
    *,
    entries_key: str,
    key: KeyFn,
    comment_keys: Iterable[str] = ("_comment",),
) -> tuple[int, int, int, int]:
    """Merge `new_manifest` into the manifest at `path`.

    `entries_key` selects which top-level key holds the entry list
    (``"symbols"`` / ``"types"``). `key` is the entry
    identity function (use ``symbol_key`` or ``type_key``).
    Top-level ``_comment*`` keys are taken from
    ``new_manifest`` when present, otherwise inherited from the
    existing file. Parent directories are created if missing.

    Returns ``(existing_count, added_count, updated_count, total_count)``.

    The read-merge-write is serialized against concurrent writers — other
    composer runs AND the oracle's ``--update``/``--create`` path — by the same
    discipline as ``query._locked_update``: an exclusive ``flock`` on the
    manifest's PARENT DIRECTORY fd (a stable inode ``os.replace`` never moves),
    with the existing file (re-)read only AFTER the lock is taken and the merge
    committed by an atomic temp-file ``os.replace``. Without this a process-level
    fan-out that lands two writers on one stem dir would lose updates or read a
    torn file (bare ``write_text`` is neither locked nor atomic).
    """
    import fcntl
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    dirfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        # Read INSIDE the lock so we merge against the latest committed content.
        if path.exists():
            existing_doc = json.loads(path.read_text())
            existing_entries: list[Entry] = list(existing_doc.get(entries_key, []))
        else:
            existing_doc = {}
            existing_entries = []

        pre_existing = len(existing_entries)
        new_entries = list(new_manifest.get(entries_key, []))
        merged, added, updated = merge_entries(existing_entries, new_entries, key=key)

        out: dict[str, Any] = {}
        for ck in comment_keys:
            if ck in new_manifest:
                out[ck] = new_manifest[ck]
            elif ck in existing_doc:
                out[ck] = existing_doc[ck]
        out[entries_key] = merged

        blob = json.dumps(out, indent=2) + "\n"
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(path.parent), delete=False)
        try:
            tmp.write(blob)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)
    finally:
        fcntl.flock(dirfd, fcntl.LOCK_UN)
        os.close(dirfd)
    return pre_existing, added, updated, len(merged)
