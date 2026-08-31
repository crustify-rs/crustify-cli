"""Translation-agent execution seams over a precomputed wave."""
from __future__ import annotations

from pathlib import Path


def _check_ffi_crates(layout, linked: set[str]) -> None:
    """Require the orchestrator-authored ``-sys`` shell for each link unit."""
    missing = sorted(
        lib for lib in linked
        if lib and not (layout.rust / f"{lib}-sys" / "Cargo.toml").exists()
    )
    if missing:
        raise SystemExit(
            f"translate: orchestrator-authored -sys crate missing for "
            f"{missing!r}. Author and build it before scheduling these units.")


def batch_objective(_batch, objective: str, _scope_of=None) -> str:
    """The executor-supplied objective is handed through unchanged."""
    return objective


def _translate_emit(target: Path, layout, *, max_syms: int,
                    objective: str = "wrap", scope_of=None,
                    prompt_capabilities: tuple[str, ...] | None = None):
    """Build the agent invocation for one wave batch."""
    from crustify.agents.translate import TranslateAgent
    if prompt_capabilities is None:
        prompt_capabilities = TranslateAgent.configured_capabilities(layout)

    def emit(batch) -> None:
        obj = batch_objective(batch, objective, scope_of)
        type_units = [unit for unit in batch.units if unit.kind == "type"]
        if type_units:
            TranslateAgent(
                target, batch_kind="type",
                tags=[unit.node.id for unit in type_units],
                kinds=[unit.node.subkind for unit in type_units],
                entry_files=[unit.node.defined_in for unit in type_units],
                objective=obj, prompt_capabilities=prompt_capabilities,
                repo_root=layout.repo_root,
            ).run()
            return
        syms = [{"name": member.id, "defined_in": member.defined_in}
                for member in batch.members]
        TranslateAgent(
            target, batch_kind="syms", syms=syms, objective=obj,
            prompt_capabilities=prompt_capabilities, repo_root=layout.repo_root,
        ).run()
    return emit


LIFETIME_TIERS = ("void", "string")


def lifetime_objective(objective: str) -> str:
    """Lifetime markers wrap discovered primitives, or preserve review."""
    return "review" if objective == "review" else "wrap"


def translate_lifetime_for(target: Path, spec: str, *, objective: str = "wrap",
                           dry_run: bool = False) -> None:
    """Execute the single raw-lifetime item represented by a wave."""
    if spec not in LIFETIME_TIERS:
        raise SystemExit(
            f"translate raw-lifetime: expected {' or '.join(LIFETIME_TIERS)}, "
            f"got {spec!r}")
    import crustify._schedule as schedule
    from crustify.agents.translate import TranslateAgent
    from crustify.layout import Layout

    effective_objective = lifetime_objective(objective)
    if dry_run:
        policy = ("explicit --objective review" if effective_objective == "review"
                  else "raw-lifetime route normalizes to wrap")
        print(f"[translate dry-run] --lifetime-for {spec}: one agent, "
              f"objective {effective_objective} ({policy}), no composed "
              f"worklist (the agent discovers the primitives).")
        return

    layout = Layout.discover(target)
    capabilities = TranslateAgent.configured_capabilities(layout)

    def factory(target_, layout_):
        def emit(_batch) -> None:
            TranslateAgent(
                target_, batch_kind="syms", lifetime_for=spec,
                objective=effective_objective,
                campaign_objective=objective,
                prompt_capabilities=capabilities,
                repo_root=layout_.repo_root,
            ).run()
        return emit

    batch = schedule.Batch(file=f"lifetime-for-{spec}")
    stage = schedule.Stage(
        verb=effective_objective, in_scope=lambda _node: True,
        emit_fn=lambda _batch: None, max_syms=1, emit_factory=factory,
        target=target, layout=layout,
    )
    failures = schedule._isolated_step([batch], stage, 1)
    if failures:
        raise SystemExit(
            f"translate raw-lifetime {spec}: agent failed: {failures[0][1]}")
    print("[crustify translate] done.")
