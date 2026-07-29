"""Tunable knobs for the crustify pipeline.
"""

import secrets as _secrets
import time as _time

# ---------------------------------------------------------------------------
# Session identity — generated once per CLI invocation
# ---------------------------------------------------------------------------

SESSION_ID: str = f"{_time.strftime('%Y-%m-%d_%H-%M-%S')}_{_secrets.token_hex(2)}"
"""Timestamp label shared by all agents in a single ``crustify`` run.

Used to group per-session log files under
``targets/<target>/logs/<SESSION_ID>/`` -- see :mod:`crustify.agentlog` for
what each agent writes there.

The timestamp keeps sessions chronologically sortable; the trailing 4-hex
random token disambiguates crustify processes launched within the same second
(e.g. parallel ``--out-suffix`` model-comparison runs), so their per-session log
dirs never collide and clobber each other's ``<stage>.log``.
"""


# ---------------------------------------------------------------------------
# Wrap stage — per-type-wrapper effort budget
# ---------------------------------------------------------------------------
# Used by the wrap orchestrator (``crustify.wrap``) to cap the workload handed
# to a single ``CrustifyWrap`` type-wrapper agent. The orchestrator slices each
# selected type's surface deterministically and passes the agent a *fixed*
# worklist; surface beyond the budget is recorded in the wrapper's roadmap
# comment and left for a follow-up pass. This guards against "god objects"
# (a type with hundreds of fields / ops) blowing a single agent's context.

WRAP_MAX_FIELDS: int = 50
"""Maximum number of ``fields[]`` entries handed to one type-wrapper agent
(manifest order; the first N are wrapped, the rest deferred)."""

WRAP_MAX_SYMS: int = 50
"""Per-batch symbol budget for the wrap stage — wired as the scheduler's
``max_syms``. It bounds BOTH a type's op-chunking (lifecycle ctors/dtor/up_ref/
clone/locking + method ``ops[]`` counted together, lifecycle-first so the
shape-bearing surface is never dropped) AND the free-symbol pooling per file
(how many wrap-scope free syms ride one ``wrap syms`` agent). Was
``WRAP_MAX_OPS`` — renamed to reflect its true dual role now that op sets are
small."""


# ---------------------------------------------------------------------------
# Port stage — per-batch effort budget
# ---------------------------------------------------------------------------
# Used by the port orchestrator (``crustify.port``) to bound the working set
# handed to a single ``CrustifyPort`` agent. The orchestrator bin-packs
# port-scope DAG nodes within a dependency layer under these caps; a scheduled
# type folds its op-set (ctors/dtor/up_ref/clone/ops) into the symbol count, so
# a type and its methods ride one batch. The agent receives a fixed working set
# and is unaware it is capped.
#
# Two budgets bind together (whichever is hit first closes a batch): a COUNT cap
# (``PORT_MAX_SYMS``) guards the many-tiny-symbols case, and a LINES-OF-CODE cap
# (``PORT_MAX_LOC``) guards the few-huge-functions case. Per-symbol LoC is the
# CodeQL body line-span (functions.csv `loc` column); globals count as 1 and
# macros as 0 (we don't port macros). When `loc` is absent (un-re-extracted
# functions.csv), a symbol contributes 0 and the count cap binds alone.

PORT_MAX_SYMS: int = 20
"""Maximum number of port-scope symbols (functions / globals / macros, plus a
scheduled type's folded ops) in a single ``CrustifyPort`` batch."""

PORT_MAX_LOC: int = 500
"""Maximum total lines-of-code (summed per-symbol body span) in a single
``CrustifyPort`` batch — binds together with ``PORT_MAX_SYMS`` (first cap hit
closes the batch). Functions contribute their body line-span; globals 1; macros
0. Keeps a batch of a few large functions from blowing the agent's context even
when the symbol *count* is well under ``PORT_MAX_SYMS``."""


# ---------------------------------------------------------------------------
# Agent execution — model, backend, billing
# ---------------------------------------------------------------------------

MODEL_OVERRIDE: str | None = None
"""Set by the CLI ``--model`` flag. When non-None, every agent runs against
this model instead of its hard-coded per-agent default. None => each
agent's own default.

Named ``<provider>/<model>`` — see :mod:`crustify.models`."""

BACKEND: str | None = None
"""Force a specific agent backend (CLI ``--backend``), overriding the one
:mod:`crustify.models` derives from the model name.

None (the default) means derive: the model decides, since a Claude model
can only be driven by the claude CLI and an OpenAI one only by codex. Set
this only to force a pairing. See :mod:`crustify.agents.backends`."""

BILLING: str = "subscription"
"""How the provider CLI authenticates (set by the CLI ``--billing`` flag):

  - ``subscription`` -- the CLI's own logged-in account.
  - ``api`` -- an API key from the environment.

The distinction is not cosmetic: it selects a different auth path per CLI,
and for Claude it is not switchable by environment variable alone."""

OVERRIDE_BASE_PROMPT: bool = True
"""Whether to replace the provider CLI's own base/system prompt with
crustify's (set by ``--override-base-prompt`` / ``--no-override-base-prompt``).

crustify's stage prompt is delivered as the user message either way; this
only controls whether the provider's own instructions survive underneath it.
Replacing them is markedly cheaper per invocation."""

LOG_TO_CONSOLE: bool = True
"""When ``False``, suppress live console output from agents."""

LOG_TO_FILE: bool = True
"""When ``False``, disable per-agent log files under
``targets/<target>/logs/<SESSION_ID>/``."""


ISOLATED_WAVE: bool = False
"""Set True by the scheduler for the duration of a parallel, worktree-isolated
wave (see :mod:`crustify.worktree`). While set, wrap/port agents run inside their
own git worktree and are asked to commit their work there (base agent appends a
commit footer). Reset to False before the merge agent runs (which must NOT
commit — it leaves the merged result uncommitted in the main tree)."""
