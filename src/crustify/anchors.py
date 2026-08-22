"""Scheduler-local translation anchors.

The read-only ``crates`` command never writes Rust source. The translate
scheduler still lays each batch's TODO anchors after forking its worktree so an
agent sees only the placeholders it owns.
"""

from __future__ import annotations

import re
from pathlib import Path


TODO = "// crustify:todo"


def _todo_anchor(item: str) -> str:
    return f"{TODO}: {item}"


def _anchor_re(name: str) -> "re.Pattern[str]":
    quoted = re.escape(name)
    return re.compile(
        rf"(?m)^\s*(?://+\s*(?:Replaces|Wraps):\s*{quoted}(?:\s|$)"
        rf"|{re.escape(TODO)}:\s*{quoted}\s*$)")


def _has_field_anchor(text: str, tag: str, field: str) -> bool:
    quoted = rf"{re.escape(tag)}\.{re.escape(field)}"
    return re.search(
        rf"(?m)^\s*(?://+\s*(?:Field|Wraps|Replaces):\s*{quoted}(?:\s|$)"
        rf"|{re.escape(TODO)}:\s*{quoted}\s*$)",
        text) is not None


def _port_touched(layout, target) -> dict[str, set] | None:
    """Return type fields touched by targeted-section code.

    ``None`` preserves the pre-analysis fallback in which every declared field
    is eligible for an anchor.
    """
    from crustify import scope
    from crustify.query import scope_touched_index

    try:
        scope.build(layout, target, stage="translate")
    except SystemExit:
        return None

    from compose import scope as compose_scope
    index = scope_touched_index(layout, target, compose_scope.TARGETED)
    return {tag: {field for fields in by_file.values() for field in fields}
            for tag, by_file in index.items()} or None


def field_map(layout, target=None) -> dict[str, list[str]]:
    """Map each type tag to the field TODOs its scheduled batch should receive."""
    if target is None:
        return {}

    from crustify import manifests

    touched = _port_touched(layout, target)
    out: dict[str, list[str]] = {}
    for entry in manifests.entries(layout, target, "types", stage="translate"):
        tag = entry.get("name") or entry.get("type")
        if not tag:
            continue
        names = [field["name"] for field in (entry.get("fields") or [])
                 if isinstance(field, dict) and field.get("name")]
        if touched is not None:
            keep = touched.get(tag, set())
            names = [name for name in names if name in keep]
        out[tag] = names
    return out


def place_anchors(
    layout,
    target: Path,
    names: list[str],
    *,
    fields: dict[str, list[str]] | None = None,
    emit: bool = True,
) -> tuple[int, list[str]]:
    """Insert this batch's missing TODO anchors in its existing Rust homes."""
    from crustify import crates

    fields = fields or {}
    doc = crates.load(layout)
    homes: dict[Path, list[str]] = {}
    unanchored: list[str] = []

    for name in names:
        entries, missing = crates.entries_for_names(doc, [name])
        if missing:
            unanchored.append(name)
            continue
        for entry in entries:
            path = crates.full_rs(layout, entry["crate_path"], entry["rs"])
            homes.setdefault(path, []).append(name)

    inserted = 0
    for rs_path, items in homes.items():
        if not rs_path.exists():
            unanchored += items
            continue
        text = rs_path.read_text()
        additions: list[str] = []
        for name in items:
            wanted = ([(name, None)]
                      + [(f"{name}.{field}", field)
                         for field in fields.get(name, ())])
            for item, field in wanted:
                anchored = (_has_field_anchor(text, name, field) if field
                            else _anchor_re(item).search(text))
                if anchored or _todo_anchor(item) in additions:
                    continue
                if not emit:
                    unanchored.append(item)
                    continue
                additions += [_todo_anchor(item), ""]
        if additions:
            separator = ("" if text.endswith("\n\n") else
                         "\n" if text.endswith("\n") else "\n\n")
            rs_path.write_text(text + separator + "\n".join(additions) + "\n")
            inserted += sum(1 for line in additions if line)

    return inserted, unanchored
