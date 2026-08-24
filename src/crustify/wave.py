"""Execute an objective-neutral wave document produced by crustify-oracle."""
from __future__ import annotations

import json
from pathlib import Path


def load(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text())
    except OSError as exc:
        raise SystemExit(f"translate: cannot read wave document at {path}: {exc}")
    except ValueError as exc:
        raise SystemExit(f"translate: invalid wave document at {path}: {exc}")

    if not isinstance(doc, dict):
        raise SystemExit("translate: wave document must be a JSON object")
    if (doc.get("schema_version") != 2
            or not isinstance(doc.get("steps"), list)):
        raise SystemExit("translate: unsupported wave document schema")

    required = {
        "oracle_target": str,
        "summary": dict,
        "plan_items": list,
        "dependency_nodes": list,
    }
    missing = [name for name, kind in required.items()
               if not isinstance(doc.get(name), kind)]
    if missing:
        raise SystemExit(
            "translate: invalid wave document; expected " + ", ".join(missing))
    summary_fields = ("unit_count", "layer_count", "batch_count")
    missing_summary = [name for name in summary_fields
                       if not isinstance(doc["summary"].get(name), int)]
    if missing_summary:
        raise SystemExit(
            "translate: invalid wave document summary; expected "
            + ", ".join(missing_summary))
    for step_index, step in enumerate(doc["steps"]):
        layers = step.get("layers") if isinstance(step, dict) else None
        if not (isinstance(step, dict)
                and isinstance(layers, list) and layers
                and all(isinstance(layer, int) for layer in layers)
                and isinstance(step.get("unit_count"), int)
                and isinstance(step.get("batches"), list)):
            raise SystemExit(
                f"translate: invalid wave document step at index {step_index}")
        for batch_index, batch in enumerate(step["batches"]):
            if not (isinstance(batch, dict)
                    and isinstance(batch.get("kind"), str)
                    and isinstance(batch.get("items"), list)):
                raise SystemExit(
                    "translate: invalid wave document batch at "
                    f"step {step_index}, index {batch_index}")
    return doc


def _node(item: dict):
    from crustify._schedule import Node
    def keys(side):
        return [(entry["name"], entry.get("defined_in"))
                for entry in item.get("deps", {}).get(side, [])]
    def refs(name):
        return [(entry["name"], entry.get("defined_in"))
                for entry in item.get(name, [])]
    return Node(
        id=item["name"],
        node_kind="type" if item.get("kind") == "type" else "symbol",
        subkind=item.get("source_kind") or "symbol",
        defined_in=item.get("defined_in"), layer=int(item.get("layer") or 0),
        dep_types=keys("types"), dep_syms=keys("symbols"),
        fallback=refs("fallback"), back_fill=refs("back_fill"),
        loc=int(item.get("loc") or 0), generates=list(item.get("generates") or ()),
    )


def _decode(doc: dict):
    import crustify._schedule as S
    steps = []
    for step in doc["steps"]:
        batches = []
        for raw in step.get("batches") or []:
            units = []
            anchors = {}
            for item in raw.get("items") or []:
                node = _node(item)
                units.append(S.Unit("type" if item.get("kind") == "type" else "sym",
                                    node, fields=list(item.get("field_anchors") or ())))
                if item.get("field_anchors"):
                    anchors[node.id] = list(item["field_anchors"])
            members = [unit.node for unit in units]
            batches.append(S.Batch(
                file=raw.get("source_file"), units=units, members=members,
                fields=[field for unit in units for field in unit.fields],
                field_anchors=anchors,
            ))
        steps.append((list(step.get("layers") or ()),
                      int(step.get("unit_count") or 0), batches))
    return steps


def _dry_run(doc: dict, objective: str) -> None:
    import crustify._schedule as S
    steps = _decode(doc)
    items = [_node(item) for item in doc.get("plan_items") or []]
    units = [S.Unit("type" if node.node_kind == "type" else "sym", node)
             for node in items]
    all_batches = [batch for _layers, _count, batches in steps for batch in batches]
    summary = doc["summary"]
    print(f"\n[{objective} dry-run] {summary['unit_count']} unit(s) across "
          f"{summary['layer_count']} dependency layer(s) (lower → higher):")
    for layers, count, batches in steps:
        label = str(layers[0]) if len(layers) == 1 else f"{layers[0]}-{layers[-1]}"
        merged = " (merged)" if len(layers) > 1 else ""
        print(f"  L{label}: {count} unit(s) → {len(batches)} batch(es)"
              f"{' (parallel)' if len(batches) > 1 else ''}{merged}")

    by_key = {unit.node.key: unit.node for unit in units}
    in_scope = set(by_key)
    for raw in doc.get("dependency_nodes") or []:
        node = _node(raw)
        by_key[node.key] = node
        if raw.get("in_scope"):
            in_scope.add(node.key)
    S.show_plan(units, all_batches, by_key, lambda node: node.key in in_scope,
                objective)


def _plan_index(doc: dict):
    from crustify import _schedule as S
    selected = [_node(item) for item in doc.get("plan_items") or []]
    by_key = {node.key: node for node in selected}
    in_scope = set(by_key)
    for raw in doc.get("dependency_nodes") or []:
        node = _node(raw)
        by_key[node.key] = node
        if raw.get("in_scope"):
            in_scope.add(node.key)
    units = {node.key: S.Unit(
        "type" if node.node_kind == "type" else "sym", node)
             for node in selected}
    return units, by_key, in_scope


def execute(target: Path, wave_path: Path, *, objective: str,
            parallel_max: int, dry_run: bool = False) -> None:
    import crustify._schedule as S
    from crustify import crates
    from crustify.agents.translate import TranslateAgent
    from crustify.layout import Layout
    from crustify.translate import _check_ffi_crates, _translate_emit

    doc = load(wave_path)
    layout = Layout.discover(target)
    expected = layout.rel_target(target)
    if doc.get("oracle_target") != expected:
        raise SystemExit(
            f"translate: wave targets {doc.get('oracle_target')!r}, not {expected!r}")
    from crustify import config
    from crustify.agentlog import open_session_log
    log_root = layout.logs(target) / config.SESSION_ID
    resolved_wave = Path(wave_path).resolve()
    raw_items = [item for step in doc["steps"]
                 for batch in step.get("batches") or []
                 if batch.get("kind") == "raw-lifetime"
                 for item in batch.get("items") or []]
    if raw_items:
        if len(raw_items) != 1:
            raise SystemExit("translate: raw-lifetime wave must contain one item")
        from crustify.translate import translate_lifetime_for
        if dry_run:
            translate_lifetime_for(
                target, raw_items[0]["name"], objective=objective,
                dry_run=True)
            return
        with open_session_log(log_root, objective) as session_log:
            session_log.line(f"[crustify] wave: {resolved_wave}")
            translate_lifetime_for(
                target, raw_items[0]["name"], objective=objective,
                dry_run=False)
        return
    steps = _decode(doc)

    placement = crates.load(layout)
    missing = set()
    linked = set()
    for _layers, _count, batches in steps:
        for batch in batches:
            for member in batch.members:
                hit = (crates.lookup(placement, member.id, file=member.defined_in)
                       or crates.lookup(placement, member.id))
                if hit:
                    linked.add(hit["crate"])
                if S.resolve_path(member, placement, layout) is None:
                    missing.add((member.id, member.defined_in or "?"))
    if missing:
        listing = "\n".join(f"  - {name}  ({home})" for name, home in sorted(missing))
        raise SystemExit(
            f"{objective}: {len(missing)} selected item(s) have no home `.rs` "
            f"on disk (no crates.json home, or the orchestrator has not created "
            f"the recorded file). Author the missing module(s) and retry:\n{listing}")
    _check_ffi_crates(layout, linked)
    if dry_run:
        _dry_run(doc, objective)
        return

    prompt_capabilities = TranslateAgent.configured_capabilities(layout)
    def factory(target_, layout_):
        return _translate_emit(target_, layout_, max_syms=doc.get("budgets", {}).get("max_syms", 50),
                               objective=objective,
                               prompt_capabilities=prompt_capabilities)
    stage = S.Stage(
        verb=objective, in_scope=lambda _node: True, emit_fn=lambda _batch: None,
        max_syms=doc.get("budgets", {}).get("max_syms", 50),
        emit_factory=factory, target=target, layout=layout,
    )
    failures = []
    planned, by_key, in_scope = _plan_index(doc)
    with open_session_log(log_root, objective) as session_log:
        session_log.line(f"[crustify] wave: {resolved_wave}")
        session_log.line(
            f"[crustify] {doc['summary']['unit_count']} unit(s), "
            f"{doc['summary']['layer_count']} layer(s), parallel_max={parallel_max}")
        for layers, count, batches in steps:
            if not batches:
                continue
            label = (str(layers[0]) if len(layers) == 1
                     else f"{layers[0]}-{layers[-1]}")
            if len(steps) > 1:
                print(f"\n[{objective}] dependency layer {label}: {count} unit(s) → "
                      f"{len(batches)} batch(es) (lower layers already landed)")
            layer_set = set(layers)
            wave_units = [unit for unit in planned.values()
                          if unit.node.layer in layer_set]
            S.show_plan(wave_units, batches, by_key,
                        lambda node: node.key in in_scope, objective)
            before = len(failures)
            failures += S.run(batches, stage, parallel_max=parallel_max)
            session_log.checkpoint(
                f"layer {label}: {count} unit(s), {len(batches)} batch(es), "
                f"{len(failures) - before} failure(s)")
        session_log.line(
            f"[crustify] {len(failures)} failure(s) over "
            f"{doc['summary']['batch_count']} batch(es)")
    if failures:
        lines = "\n".join(
            f"  - {batch.label()}: {type(exc).__name__}: {exc}"
            for batch, exc in failures)
        raise SystemExit(f"translate failed for {len(failures)} batch(es):\n{lines}")
    print("[crustify-cli translate] done.")
