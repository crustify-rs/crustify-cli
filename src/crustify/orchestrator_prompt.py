"""Render the standalone Crustify orchestrator prompt."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crustify.agents.base import _skill_meta


def _skill_block(skill: Path) -> tuple[str, str]:
    name, description, _binary, doc = _skill_meta(skill)
    if not description:
        raise SystemExit(f"skill has no description: {skill}")
    block = f"- {name} — {description}"
    if skill.read_text().startswith("---"):
        block += f"\n  read in full: {skill.resolve()}"
    elif doc:
        block += f"\n  read in full: {doc}"
    return name, block


def render(oracle: Path, audit: Path) -> str:
    """Render from the three source-owned skill definitions."""
    root = Path(__file__).resolve().parents[2]
    prompts = root / "src" / "crustify" / "prompts"
    template = (prompts / "orchestrator.md").read_text()

    sources = (
        prompts / "skill-orchestrator.md",
        oracle.resolve() / "SKILL.md",
        audit.resolve() / "SKILL.md",
    )
    for skill, owner in ((sources[1], "crustify-oracle"),
                         (sources[2], "crustify-audit")):
        if not skill.is_file():
            raise SystemExit(f"no {skill} — expected a {owner} checkout")

    blocks = sorted(_skill_block(skill) for skill in sources)
    index = (
        "## Skills\n\n"
        "Reusable how-to guides for recurring decisions. If a skill's "
        "`description` below matches what you're doing, **read that skill's "
        "file in full** before proceeding—the description is the routing "
        "signal and the body is the procedure.\n\n"
        + "\n".join(block for _name, block in blocks)
    )

    docs = root / "docs"
    for doc in ("conventions.md", "orchestrator-playbook.md"):
        if not (docs / doc).is_file():
            raise SystemExit(f"no {docs / doc}")

    for marker, value in (
        ("<!-- CONVENTIONS_PATH -->", f"`{docs / 'conventions.md'}`"),
        ("<!-- SKILLS -->", index),
    ):
        if marker not in template:
            raise SystemExit(f"orchestrator.md no longer carries {marker}")
        template = template.replace(marker, value)
    return template.rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crustify-orchestrator-prompt",
        description="Render the prompt used to start a Crustify orchestrator.",
    )
    parser.add_argument("oracle_checkout", type=Path)
    parser.add_argument("audit_checkout", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args(argv)

    text = render(args.oracle_checkout, args.audit_checkout)
    if args.output:
        args.output.write_text(text)
        print(
            f"wrote {args.output} ({len(text.splitlines())} lines)",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
