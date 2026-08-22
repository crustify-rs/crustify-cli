from pathlib import Path


class ArtifactStore:
    """Thin accessor for one crustify artifact directory.

    Constructed with the **exact** directory it manages (resolved via
    ``crustify.layout.Layout``) — the repo-tier root ``crustify/`` or a
    active ``crustify/campaigns/<campaign>/``. No path magic here.

    Stage completion is data-driven: agents check for the existence of
    their output artifacts (files or directories). There is no
    ``state.json`` — each stage's done-ness is the existence of its
    on-disk artifact, and an empty/missing artifact means the stage
    hasn't run.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def artifact_exists(self, filename: str) -> bool:
        """True iff the named artifact (file or directory) exists here."""
        return (self.root / filename).exists()

    def path(self, *parts: str) -> Path:
        """Construct a path under this directory."""
        return self.root.joinpath(*parts)
