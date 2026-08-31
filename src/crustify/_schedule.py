"""Batch execution for objective-neutral oracle wave documents.

Selection, dependency ordering and packing live in wavefront. This module
owns only the stateful half: placement checks, TODO insertion, isolated
worktrees, bounded concurrency, and agent invocation.
"""
from __future__ import annotations

import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

NodeKey = tuple[str, str | None]


@dataclass
class Node:
    id: str
    node_kind: str
    subkind: str
    defined_in: str | None
    layer: int
    dep_types: list[NodeKey]
    dep_syms: list[NodeKey]
    fallback: list[NodeKey] = field(default_factory=list)
    back_fill: list[NodeKey] = field(default_factory=list)
    loc: int = 0
    generates: list[str] = field(default_factory=list)

    @property
    def key(self) -> NodeKey:
        return self.id, self.defined_in


@dataclass
class Unit:
    kind: str
    node: Node
    ops: list[Node] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    @property
    def members(self) -> list[Node]:
        return [self.node, *self.ops]

    def label(self) -> str:
        return self.node.id


@dataclass
class Batch:
    file: str | None
    units: list[Unit] = field(default_factory=list)
    members: list[Node] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    op_range: tuple[int, int] | None = None
    field_range: tuple[int, int] | None = None
    field_anchors: dict[str, list[str]] | None = None

    def label(self) -> str:
        head = self.units[0].label() if self.units else "?"
        source = Path(self.file).name if self.file else "*"
        tail = f" +{len(self.units) - 1}" if len(self.units) > 1 else ""
        return f"{source}: {head}{tail}"


EmitFn = Callable[[Batch], None]


@dataclass
class Stage:
    verb: str
    in_scope: Callable[[Node], bool]
    emit_fn: EmitFn
    max_syms: int
    emit_factory: Callable[[Path, Any], EmitFn] | None = None
    target: Path | None = None
    layout: Any = None
    shared_artifact_fn: Callable[[], None] | None = None


def resolve_path(node: Node, doc: dict, layout) -> Path | None:
    from crustify import crates
    hit = (crates.lookup(doc, node.id, file=node.defined_in or None)
           or crates.lookup(doc, node.id))
    if not hit:
        return None
    path = crates.full_rs(layout, hit["crate_path"], hit["rs"])
    return path if path.exists() else None


def bundle_deps(unit: Unit) -> tuple[list[NodeKey], list[NodeKey]]:
    inside = {member.key for member in unit.members}
    types, symbols = set(), set()
    for member in unit.members:
        types.update(member.dep_types)
        symbols.update(member.dep_syms)
    key = lambda item: (item[0], item[1] or "")
    return (sorted(types, key=key),
            sorted((item for item in symbols if item not in inside), key=key))


def _scope_label(key: NodeKey, by_key, in_scope) -> str:
    node = by_key.get(key)
    return "ext" if node is None else ("wrap" if in_scope(node) else "port")


def show_plan(units: list[Unit], batches: list[Batch], by_key, in_scope,
              verb: str) -> None:
    print(f"\nAbout to {verb}:")
    for unit in units:
        print(f"  • {unit.label()}")

    types, symbols = set(), set()
    for unit in units:
        dep_types, dep_symbols = bundle_deps(unit)
        types.update(dep_types)
        symbols.update(dep_symbols)
    inside = {member.key for unit in units for member in unit.members}
    key = lambda item: (item[0], item[1] or "")
    types = sorted((item for item in types if item not in inside), key=key)
    symbols = sorted((item for item in symbols if item not in inside), key=key)
    if types:
        print("\nFirst-layer TYPE deps (emit these first, in your order):")
        for name, home in types:
            where = f" [{home}]" if home else ""
            print(f"  - {name}{where} ({_scope_label((name, home), by_key, in_scope)})")
    if symbols:
        tally: dict[str, int] = {}
        for item in symbols:
            label = _scope_label(item, by_key, in_scope)
            tally[label] = tally.get(label, 0) + 1
        summary = ", ".join(f"{count} {label}" for label, count in sorted(tally.items()))
        print(f"\n+ {len(symbols)} symbol dep(s) ({summary}) — the C/FFI bridge "
              "covers any not-yet-emitted.")
    print(f"\n{len(batches)} batch(es) across "
          f"{len({batch.file for batch in batches})} file(s).")


def _place_batch_anchors(batch: Batch, layout, target, stage: Stage) -> None:
    from crustify.anchors import place_anchors
    names = [unit.node.id for unit in batch.units]
    if not names:
        return
    review = stage.verb == "review"
    _inserted, unanchored = place_anchors(
        layout, target, names, fields=batch.field_anchors or {}, emit=not review)
    if unanchored:
        action = "have no anchor — nothing emitted to review" if review else (
            "have no home in crates.json and were left unanchored")
        print(f"[crustify {stage.verb}] {len(unanchored)} item(s) {action}: "
              + ", ".join(sorted(unanchored)[:8])
              + (" …" if len(unanchored) > 8 else ""))


def run(batches: list[Batch], stage: Stage, *, parallel_max: int) \
        -> list[tuple[Batch, BaseException]]:
    """Run at most ``parallel_max`` isolated agents for one topological step."""
    if parallel_max < 1:
        raise ValueError("parallel_max must be >= 1")
    if (stage.emit_factory is None or stage.target is None
            or stage.layout is None):
        failures = []
        for batch in batches:
            try:
                stage.emit_fn(batch)
            except BaseException as exc:  # noqa: BLE001
                failures.append((batch, exc))
        return failures
    return _isolated_step(batches, stage, parallel_max)


def _isolated_step(batches: list[Batch], stage: Stage,
                   parallel_max: int) -> list[tuple[Batch, BaseException]]:
    from crustify import config
    from crustify import worktree
    from crustify.layout import Layout

    repo = Path(stage.layout.repo_root)
    rel = stage.layout.rel_target(stage.target)
    base = worktree.session_base(repo, f"{stage.verb}-{config.SESSION_ID}")
    lock = threading.Lock()
    made = 0

    def fork(index: int, batch: Batch) -> Path:
        nonlocal made
        stem = Path(batch.file).stem if batch.file else "batch"
        slug = (f"{stage.verb}-{config.SESSION_ID}-{index:02d}-"
                + re.sub(r"[^A-Za-z0-9]+", "_", stem)[:24]
                + "-" + secrets.token_hex(4))
        with lock:
            tree = worktree.add_worktree(repo, base.branch, slug)
            worktree.link_shared(tree, repo)
            made += 1
        return tree

    def execute(index: int, batch: Batch):
        try:
            tree = fork(index, batch)
            work_layout = Layout(tree)
            _place_batch_anchors(batch, work_layout, tree / rel, stage)
            stage.emit_factory(tree / rel, work_layout)(batch)
            return None
        except BaseException as exc:  # noqa: BLE001
            return batch, exc

    config.SESSION_BASE = base.branch
    failures = []
    try:
        with ThreadPoolExecutor(max_workers=parallel_max) as executor:
            futures = [executor.submit(execute, index, batch)
                       for index, batch in enumerate(batches)]
            for future in as_completed(futures):
                failure = future.result()
                if failure:
                    failures.append(failure)
    finally:
        config.SESSION_BASE = ""

    print(f"[{stage.verb}] {made} agent worktree(s) forked under "
          f"{repo / worktree._WT_DIR}; session branch {base.branch}")
    if stage.shared_artifact_fn is not None:
        stage.shared_artifact_fn()
    return failures
