#!/usr/bin/env bash
# Render prompts/orchestrator.md into a self-contained prompt to paste into
# whatever assistant is driving a crustify port.
#
#   utils/build-orchestrator-prompt.sh <ffibox-checkout> [-o OUT]
#
# Writes to stdout unless -o is given. Progress and warnings go to stderr, so
# the prompt can be piped or redirected without picking them up.
#
# WHY A BUILD STEP. A translate agent gets its skill index from crustify at
# spawn time, which owns that agent's system prompt. Nothing spawns an
# orchestrator, so there is no runtime to hook: this script is that runtime,
# run once by hand.
#
# The output inlines the skill index rather than pointing at it. That is only
# safe because it is DERIVED -- regenerate and the copy is current.
# Hand-copying the same text would be the drift this avoids.
#
# docs/principles.md and docs/playbook.md stay POINTERS, each resolved to an
# absolute path: the orchestrator reads them from disk, unlike a translate
# agent, which is handed principles in its system prompt. The paths are this
# checkout's, so both are openable the moment the prompt is pasted -- before a
# target repo has been named. Inlining the playbook instead would put ~280
# lines of setup procedure in front of every wave-planning turn.
#
# WHERE SKILLS COME FROM. Content is read from each skill's SOURCE, never from
# a deployed copy: prompts/skill-oracle.md here, plus the ffibox
# checkout named on the command line, since that skill lives in its own repo.
# Reading a target's .claude/skills/ instead would source a generator from its
# own deployed derivative, so a stale copy there would silently yield a stale
# prompt. The orchestrator's own playbook is not in this list -- it IS this
# document, appended below the template, so routing to it would be circular.
#
# A skill WITH a body also gets a path, and that path is the deployed one
# (`.claude/skills/<name>/SKILL.md`), because that is where the orchestrator
# opens it at runtime. Content from truth, references to where the reader can
# reach it -- and since those are repo-relative, one prompt serves every
# target. A metadata-only skill is inlined whole and gets no path.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
OUT=""

usage() {
    echo "usage: ${BASH_SOURCE[0]##*/} <ffibox-checkout> [-o OUT]" >&2
    echo "       writes the rendered prompt to stdout unless -o is given" >&2
    exit 2
}

[ $# -ge 1 ] || usage
PRIM="$1"; shift
while [ $# -gt 0 ]; do
    case "$1" in
        -o) [ $# -ge 2 ] || usage; OUT="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unexpected argument: $1" >&2; usage ;;
    esac
done

[ -f "$PRIM/SKILL.md" ] || {
    echo "no $PRIM/SKILL.md -- expected a ffibox checkout" >&2
    exit 1
}
PRIM="$(cd "$PRIM" && pwd)"

RENDERED="$(
    REPO="$REPO" PRIM="$PRIM" "$PYTHON" - <<'PY'
import os
import sys
from pathlib import Path

repo = Path(os.environ["REPO"])
prim = Path(os.environ["PRIM"])
sys.path.insert(0, str(repo / "src"))

# The frontmatter parser is imported, never reimplemented: descriptions stay
# single-sourced from each SKILL.md, so this script cannot drift from them.
from crustify.agents.base import _skill_meta

prompts = repo / "src" / "crustify" / "prompts"
template = (prompts / "orchestrator.md").read_text()

# Mirrors `CrustifyAgent.SKILLS`. The oracle entry is metadata only, so it
# lives under prompts/ as plain markdown; ffibox keeps frontmatter and a
# real body.
# `CrustifyAgent.SKILLS` plus the playbook, which is orchestrator-facing and
# deliberately absent from that tuple: a translate agent never authors config
# or plans a wave.
sources = [repo / "src" / "crustify" / "prompts" / "skill-oracle.md",
           repo / "src" / "crustify" / "prompts" / "skill-playbook.md",
           prim / "SKILL.md"]

blocks = []
for skill in sources:
    name, desc, _bin, doc = _skill_meta(skill)
    block = f"- {name} — {desc}"
    # Same rule as `CrustifyAgent._render_skills`: point at whatever carries the
    # procedure. A frontmatter skill carries its own, and is read from where it
    # is DEPLOYED in the target. A metadata-only one names its doc (`Doc path`),
    # which lives in the crustify or ffibox checkout, so it is absolute.
    if skill.read_text().startswith("---"):
        block += f"\n  read in full: .claude/skills/{name}/SKILL.md"
    elif doc:
        block += f"\n  read in full: {doc}"
    blocks.append((name, block))
if not blocks:
    sys.exit("no skill sources resolved")

index = (
    "## Skills\n\n"
    "Reusable how-to guides for recurring decisions. If a skill's "
    "`description` below matches what you're doing, **read that skill's file "
    "in full** before proceeding - the description is the routing signal; the "
    "body is the procedure. Paths are relative to the target repo root.\n\n"
    + "\n".join(b for _n, b in sorted(blocks))
)

docs = repo / "docs"
for doc in ("principles.md", "playbook.md"):
    if not (docs / doc).is_file():
        sys.exit(f"no {docs / doc}")

# Order matters: SKILLS lands the playbook's description into the text, and
# PLAYBOOK_PATH then resolves the marker that arrived with it.
for marker, value, required_in_template in (
        ("<!-- PRINCIPLES_PATH -->", f"`{docs / 'principles.md'}`", True),
        ("<!-- SKILLS -->", index, True),
        ("<!-- PLAYBOOK_PATH -->", f"`{docs / 'playbook.md'}`", False)):
    if required_in_template and marker not in template:
        sys.exit(f"orchestrator.md no longer carries {marker}")
    if marker not in template:
        sys.exit(f"skill-playbook.md no longer carries {marker}")
    template = template.replace(marker, value)

sys.stdout.write(template.rstrip() + "\n")
PY
)"

if [ -n "$OUT" ]; then
    printf '%s\n' "$RENDERED" > "$OUT"
    echo "wrote $OUT ($(printf '%s' "$RENDERED" | wc -l) lines)" >&2
else
    printf '%s\n' "$RENDERED"
fi
