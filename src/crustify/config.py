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
``campaigns/logs/<SESSION_ID>/`` -- see :mod:`crustify.agentlog` for
what each agent writes there.

The timestamp keeps sessions chronologically sortable; the trailing 4-hex
random token disambiguates crustify processes launched within the same second
(e.g. parallel model-comparison runs on separate branches), so their per-session log
dirs never collide and clobber each other's ``<stage>.log``.
"""


# ---------------------------------------------------------------------------
# Agent execution — model, backend, billing
# ---------------------------------------------------------------------------

MODEL_OVERRIDE: str | None = None
"""Set by the CLI ``--model`` flag. When non-None, every agent runs against
this model instead of its hard-coded per-agent default. None => each
agent's own default.

Named ``<provider>/<model>`` — see :mod:`crustify.models`."""

BILLING: str = "subscription"
"""How the provider CLI authenticates (set by the CLI ``--billing`` flag):

  - ``subscription`` -- the CLI's own logged-in account.
  - ``api`` -- an API key from the environment.

The distinction is not cosmetic: it selects a different auth path per CLI,
and for Claude it is not switchable by environment variable alone."""

OVERRIDE_BASE_PROMPT: bool = False
"""Whether to replace the provider CLI's own base/system prompt with
crustify's (set by ``--override-base-prompt`` / ``--no-override-base-prompt``).

crustify's stage prompt is delivered as the user message either way; this
only controls whether the provider's own instructions survive underneath it.

Defaults to KEEPING the provider's prompt. Replacing it is markedly cheaper per
invocation, and that is why it was the default — but the v3 arm changed exactly
this and the wrappers improved, so the saving was buying worse output. The
provider's own instructions carry the tool-use and code-editing discipline the
stage prompt assumes rather than restates."""

LOG_TO_CONSOLE: bool = True
"""When ``False``, suppress live console output from agents."""

LOG_TO_FILE: bool = True
"""When ``False``, disable per-agent log files under
``campaigns/logs/<SESSION_ID>/``."""


SESSION_BASE: str = ""
"""The wave's integration BRANCH (``crustify/session/<verb>-<SESSION_ID>``), set
by the scheduler for the duration of a worktree-isolated wave (see
:mod:`crustify.worktree`). Exposed to every prompt as ``{git_base}``: an agent
lands on it with ``git push <git-common-dir> HEAD:refs/heads/{git_base}`` and
rebases onto it on rejection, so it needs the ref name -- not a path -- and only
the scheduler knows it. Empty outside a wave."""
