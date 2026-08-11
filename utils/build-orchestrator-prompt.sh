#!/usr/bin/env bash
# Render prompts/orchestrator.md into a self-contained prompt to paste into
# whatever assistant is driving a crustify port.
#
#   utils/build-orchestrator-prompt.sh <crustify-prim-checkout> [-o OUT]
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
# principles.md stays a pointer, resolved to an absolute path: the orchestrator
# reads it from disk, unlike a translate agent, which is handed it in its
# system prompt. The path is this checkout's, so it is openable the moment the
# prompt is pasted -- before a target repo has been named.
#
# WHERE SKILLS COME FROM. Content is read from each skill's SOURCE, never from
# a deployed copy: this repo's own skills/ for the ones it owns, plus the
# crustify-prim checkout named on the command line, since that skill lives in
# its own repo. Reading a target's .claude/skills/ instead would source a
# generator from its own deployed derivative, so a stale copy there would
# silently yield a stale prompt.
#
# The PATHS written into the output are the deployed ones, `.claude/skills/
# <name>/SKILL.md`, because that is where the orchestrator opens them at
# runtime. Content from truth, references to where the reader can reach it --
# and since those are repo-relative, one generated prompt serves every target.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
OUT=""

usage() {
    echo "usage: ${BASH_SOURCE[0]##*/} <crustify-prim-checkout> [-o OUT]" >&2
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
    echo "no $PRIM/SKILL.md -- expected a crustify-prim checkout" >&2
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

sources = sorted((repo / "skills").glob("*/SKILL.md")) + [prim / "SKILL.md"]

blocks = []
for skill in sources:
    name, desc, _bin = _skill_meta(skill)
    blocks.append((name, f"- {name} — {desc}\n  read in full: "
                         f".claude/skills/{name}/SKILL.md"))
if not blocks:
    sys.exit(f"no SKILL.md found under {repo / 'skills'}")

index = (
    "## Skills\n\n"
    "Reusable how-to guides for recurring decisions. If a skill's "
    "`description` below matches what you're doing, **read that skill's file "
    "in full** before proceeding - the description is the routing signal; the "
    "body is the procedure. Paths are relative to the target repo root.\n\n"
    + "\n".join(b for _n, b in sorted(blocks))
)

principles = prompts / "principles.md"
if not principles.is_file():
    sys.exit(f"no {principles}")

for marker, value in (("<!-- PRINCIPLES_PATH -->", f"`{principles}`"),
                      ("<!-- SKILLS -->", index)):
    if marker not in template:
        sys.exit(f"orchestrator.md no longer carries {marker}")
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
