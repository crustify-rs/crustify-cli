"""Execute an objective-neutral sub-campaign schedule produced by Wavefront."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load(path: Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text())
    except OSError as exc:
        raise SystemExit(f"translate: cannot read schedule at {path}: {exc}")
    except ValueError as exc:
        raise SystemExit(f"translate: invalid schedule at {path}: {exc}")

    if not isinstance(doc, dict):
        raise SystemExit("translate: schedule must be a JSON object")
    schema_version = doc.get("schema_version")
    schedules = doc.get("waves") if schema_version == 3 else doc.get("steps")
    if (schema_version not in (2, 3) or not isinstance(schedules, list)):
        raise SystemExit("translate: unsupported schedule schema")

    required = {"summary": dict}
    if schema_version == 2:
        required.update({"plan_items": list, "dependency_nodes": list})
    missing = [name for name, kind in required.items()
               if not isinstance(doc.get(name), kind)]
    if missing:
        raise SystemExit(
            "translate: invalid schedule; expected " + ", ".join(missing))
    legacy_target = doc.get("oracle_target")
    oracle_config = doc.get("oracle_config")
    valid_legacy = isinstance(legacy_target, str)
    valid_config = (
        isinstance(oracle_config, dict)
        and isinstance(oracle_config.get("path"), str)
        and isinstance(oracle_config.get("sha256"), str)
        and len(oracle_config["sha256"]) == 64
        and all(c in "0123456789abcdef" for c in oracle_config["sha256"])
    )
    if valid_legacy == valid_config:
        raise SystemExit(
            "translate: invalid schedule; expected exactly one of "
            "oracle_config or legacy oracle_target")
    summary_fields = ("unit_count", "layer_count", "batch_count")
    missing_summary = [name for name in summary_fields
                       if not isinstance(doc["summary"].get(name), int)]
    if missing_summary:
        raise SystemExit(
            "translate: invalid schedule summary; expected "
            + ", ".join(missing_summary))
    for wave_index, scheduled_wave in enumerate(schedules):
        if not (isinstance(scheduled_wave, dict)
                and isinstance(scheduled_wave.get("unit_count"), int)
                and isinstance(scheduled_wave.get("batches"), list)):
            raise SystemExit(
                f"translate: invalid scheduled wave at index {wave_index}")
        if schema_version == 2:
            layers = scheduled_wave.get("layers")
            if not (isinstance(layers, list) and layers
                    and all(isinstance(layer, int) for layer in layers)):
                raise SystemExit(
                    f"translate: invalid scheduled wave at index {wave_index}")
        for batch_index, batch in enumerate(scheduled_wave["batches"]):
            if not (isinstance(batch, dict)
                    and isinstance(batch.get("kind"), str)
                    and isinstance(batch.get("items"), list)):
                raise SystemExit(
                    "translate: invalid batch at "
                    f"wave {wave_index}, index {batch_index}")
            if schema_version == 3:
                for item in batch["items"]:
                    if not isinstance(item, dict):
                        raise SystemExit(
                            "translate: invalid item at "
                            f"wave {wave_index}, batch {batch_index}")
                    if not isinstance(item.get("layer"), int):
                        raise SystemExit(
                            "translate: invalid item layer at "
                            f"wave {wave_index}, batch {batch_index}")
                    for side in ("types", "symbols"):
                        dependencies = item.get("deps", {}).get(side, [])
                        if not all(
                            isinstance(dependency, dict)
                            and dependency.get("scope") in ("wrap", "port", "ext")
                            for dependency in dependencies
                        ):
                            raise SystemExit(
                                "translate: invalid dependency scope at "
                                f"wave {wave_index}, batch {batch_index}")
    if schema_version == 3:
        items = [
            item
            for scheduled_wave in schedules
            for batch in scheduled_wave["batches"]
            for item in batch["items"]
        ]
        identities = [
            (item.get("name"), item.get("defined_in"))
            for item in items if isinstance(item, dict)
        ]
        if (len(identities) != len(items)
                or len(set(identities)) != len(identities)
                or len(items) != doc["summary"]["unit_count"]
                or len({item["layer"] for item in items})
                != doc["summary"]["layer_count"]
                or sum(len(wave["batches"]) for wave in schedules)
                != doc["summary"]["batch_count"]):
            raise SystemExit(
                "translate: schedule summary or batched item identities disagree")
    return doc


def _waves(doc: dict) -> list[dict]:
    """Return canonical v3 waves or legacy v2 steps."""
    return doc["waves"] if doc.get("schema_version") == 3 else doc["steps"]


def _scheduled_items(doc: dict) -> list[dict]:
    return [
        item
        for scheduled_wave in _waves(doc)
        for batch in scheduled_wave.get("batches") or []
        for item in batch.get("items") or []
    ]


def _validate_oracle_provenance(doc: dict, layout, target: Path) -> None:
    """Reject a wave whose scheduling input no longer matches the repository."""
    if "oracle_target" in doc:
        expected = layout.rel_target(target)
        if doc["oracle_target"] != expected:
            raise SystemExit(
                f"translate: wave targets {doc['oracle_target']!r}, not {expected!r}")
        return

    provenance = doc["oracle_config"]
    config = Path(provenance["path"])
    if not config.is_absolute():
        config = layout.repo_root / config
    config = config.resolve()
    try:
        actual = hashlib.sha256(config.read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(
            f"translate: cannot read wave oracle config at {config}: {exc}") from exc
    if actual != provenance["sha256"]:
        raise SystemExit(
            "translate: wave oracle config changed since scheduling: "
            f"{config}")


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
    waves = []
    for scheduled_wave in _waves(doc):
        raw_items = [
            item
            for raw in scheduled_wave.get("batches") or []
            for item in raw.get("items") or []
        ]
        layers = (sorted({int(item["layer"]) for item in raw_items})
                  if doc.get("schema_version") == 3
                  else list(scheduled_wave.get("layers") or ()))
        batches = []
        for raw in scheduled_wave.get("batches") or []:
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
        waves.append((layers, int(scheduled_wave.get("unit_count") or 0),
                      batches))
    return waves


def _dry_run(doc: dict, objective: str) -> None:
    import crustify._schedule as S
    waves = _decode(doc)
    items = [_node(item) for item in _scheduled_items(doc)]
    units = [S.Unit("type" if node.node_kind == "type" else "sym", node)
             for node in items]
    all_batches = [batch for _layers, _count, batches in waves for batch in batches]
    summary = doc["summary"]
    print(f"\n[{objective} dry-run] {summary['unit_count']} unit(s) across "
          f"{summary['layer_count']} dependency layer(s) (lower → higher):")
    for wave_index, (layers, count, batches) in enumerate(waves, start=1):
        label = str(layers[0]) if len(layers) == 1 else f"{layers[0]}-{layers[-1]}"
        merged = " (merged)" if len(layers) > 1 else ""
        print(f"  Wave {wave_index} (L{label}): {count} unit(s) → "
              f"{len(batches)} batch(es)"
              f"{' (parallel)' if len(batches) > 1 else ''}{merged}")

    _planned, by_key, in_scope = _plan_index(doc)
    S.show_plan(units, all_batches, by_key, lambda node: node.key in in_scope,
                objective)


def _plan_index(doc: dict):
    from crustify import _schedule as S
    raw_selected = _scheduled_items(doc)
    selected = [_node(item) for item in raw_selected]
    by_key = {node.key: node for node in selected}
    in_scope = set(by_key)
    if doc.get("schema_version") == 2:
        for raw in doc.get("dependency_nodes") or []:
            node = _node(raw)
            by_key[node.key] = node
            if raw.get("in_scope"):
                in_scope.add(node.key)
    else:
        for raw in raw_selected:
            for side, node_kind in (("types", "type"), ("symbols", "symbol")):
                for dependency in raw.get("deps", {}).get(side, []):
                    key = (dependency["name"], dependency.get("defined_in"))
                    scope = dependency["scope"]
                    if scope != "ext" and key not in by_key:
                        by_key[key] = S.Node(
                            id=key[0], node_kind=node_kind, subkind=node_kind,
                            defined_in=key[1], layer=0,
                            dep_types=[], dep_syms=[],
                        )
                    if scope == "wrap":
                        in_scope.add(key)
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
    _validate_oracle_provenance(doc, layout, target)
    from crustify import config
    from crustify.agentlog import open_session_log
    log_root = layout.logs(target) / config.SESSION_ID
    resolved_wave = Path(wave_path).resolve()
    raw_items = [item for scheduled_wave in _waves(doc)
                 for batch in scheduled_wave.get("batches") or []
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
    waves = _decode(doc)

    placement = crates.load(layout)
    missing = set()
    linked = set()
    for _layers, _count, batches in waves:
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
        for wave_index, (layers, count, batches) in enumerate(waves, start=1):
            if not batches:
                continue
            label = (str(layers[0]) if len(layers) == 1
                     else f"{layers[0]}-{layers[-1]}")
            if len(waves) > 1:
                print(f"\n[{objective}] wave {wave_index} (dependency layer {label}): "
                      f"{count} unit(s) → "
                      f"{len(batches)} batch(es) (lower layers already landed)")
            layer_set = set(layers)
            wave_units = [unit for unit in planned.values()
                          if unit.node.layer in layer_set]
            S.show_plan(wave_units, batches, by_key,
                        lambda node: node.key in in_scope, objective)
            before = len(failures)
            failures += S.run(batches, stage, parallel_max=parallel_max)
            session_log.checkpoint(
                f"wave {wave_index}, layer {label}: {count} unit(s), "
                f"{len(batches)} batch(es), "
                f"{len(failures) - before} failure(s)")
        session_log.line(
            f"[crustify] {len(failures)} failure(s) over "
            f"{doc['summary']['batch_count']} batch(es)")
    if failures:
        lines = "\n".join(
            f"  - {batch.label()}: {type(exc).__name__}: {exc}"
            for batch, exc in failures)
        raise SystemExit(f"translate failed for {len(failures)} batch(es):\n{lines}")
    print("[crustify translate] done.")
