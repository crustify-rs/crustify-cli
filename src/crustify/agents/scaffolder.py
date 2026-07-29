from __future__ import annotations

import json
from pathlib import Path

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyScaffolder(CrustifyAgent):
    """The ``crates.json`` author.

    Fills ``templates/crates.json``'s layout for a target: identifies the
    in-scope crates, decomposes them into modules, and places every in-scope
    symbol/type into the unique ``.rs`` that mirrors its C TU — all by reasoning
    over ``crustify query`` + ``build.json`` + the codebase (never ``linked_in``,
    never raw ``scope.json``).

    Two seed modes:

      - **whole-target** (``seeds`` empty / ``"all"``) — decompose and place
        every in-scope entity. The ``scaffold --all`` path.
      - **per-seed** (``seeds`` a name list) — place just the named entities into
        the existing ``crates.json``, extending it. The lazy miss-fill triggered
        by a ``scaffold --name/--file/--dir`` lookup miss.

    ``crates.json`` is repo-root tier and cumulative across targets: the agent
    reads the existing file and ADDS, never clobbering prior placements.
    """

    name = "CrustifyScaffolder"
    model = "anthropic/claude-opus-4-8"
    stage = "scaffolder"
    tier = "repo_root"

    # `output` left None — crates.json is cumulative and demand-driven, so its
    # existence is not a done-signal. Idempotency is the additive fill (an entity
    # already placed is left untouched) plus the per-seed selector.

    def __init__(self, target: Path, *, seeds: list[str] | None = None) -> None:
        super().__init__(target)
        self._seeds = seeds or []

    def _arguments(self) -> dict:
        crustify_root = _PKG_ROOT.parent.parent
        root_dir = self.root_store.root
        return {
            "target":           self.target_rel,
            "repo_root":        str(self.repo_root),

            # Seed selector: a JSON name list (per-seed miss-fill) or the
            # sentinel "all" (whole-target decompose + place).
            "seeds":            json.dumps(self._seeds) if self._seeds else "all",

            # Where to write (cumulative) + the schema/layout to fill.
            "crates_json_path": str(self.layout.crates_json),
            "crates_template":  str(crustify_root / "templates" / "crates.json"),

            # Crate-shell source.
            "build_json_path":  str(root_dir / "build.json"),
        }
