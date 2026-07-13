from __future__ import annotations

from crustify.agents.base import CrustifyAgent, _PKG_ROOT


class CrustifyBuildPropose(CrustifyAgent):
    """Phase 1a of `build`: survey the build system at the repository
    root and emit `<repo_root>/.crustify/build.json` — structured
    metadata downstream consumers treat as authoritative for library
    partitioning, link topology, feature gating, and build invocation.

    Lives at the **repo-root tier** (`tier = "repo_root"`): the
    emitted artifact describes the whole repository's build and is
    target-independent. The user-authored target-tier `config.json`
    (read here only to resolve `repo_root`) is consulted via the base
    class's repo-root resolution.

    Replaces the prior `BUILD.md` emission; `build.json` is the sole
    authoritative source after this stage.
    """

    name = "CrustifyBuildPropose"
    model = "claude-sonnet-4-6"
    stage = "build_propose"
    output = "build.json"
    tier = "repo_root"

    def _arguments(self) -> dict:
        return {
            "target": self.target_rel,
            "repo_root": str(self.repo_root),
            "crustify_root": str(_PKG_ROOT.parent.parent),
            "config_path": str(
                self.layout.config(self.target)
            ),
            "build_json_path": str(
                self.layout.build_json
            ),
            "build_template": str(
                _PKG_ROOT.parent.parent / "templates" / "build.json"
            ),
        }


class CrustifyBuildExecute(CrustifyAgent):
    """Phase 1b of `build`: read the human-reviewed
    `<repo_root>/.crustify/build.json`, run configure + build under
    CodeQL trace + baseline tests. Done when the CodeQL database
    directory exists at `<repo_root>/.crustify/codeql/db/`.

    Lives at the **repo-root tier** (`tier = "repo_root"`): both the
    input (`build.json`) and the output (`codeql/db/`) are project-wide
    artifacts. Reads `build_commands.configure`, `build_commands.build`,
    and `build_commands.test` from `build.json` instead of grepping
    `BUILD.md` §3 / §1.
    """

    name = "CrustifyBuildExecute"
    model = "claude-sonnet-4-6"
    stage = "build_execute"
    # `output` left None — build_execute ALWAYS runs the full pipeline
    # (configure + build + codeql extract + tests). configure is not
    # incremental, CodeQL extraction is not incremental, and tests are
    # by-design always-on. Users requesting idempotent skip should
    # check `codeql/db/` existence themselves and pass `--reset` to
    # force re-extraction.
    tier = "repo_root"

    def _arguments(self) -> dict:
        return {
            "target": self.target_rel,
            "repo_root": str(self.repo_root),
            "crustify_root": str(_PKG_ROOT.parent.parent),
            "build_json_path": str(
                self.layout.build_json
            ),
            "codeql_db_path": str(
                self.layout.codeql_db
            ),
        }
