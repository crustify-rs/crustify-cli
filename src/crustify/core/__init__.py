"""Harness shared by `crustify` (translation) and `crustify-audit`.

What lives here is what BOTH sides need and neither owns: how a model name
routes to a backend, and how a run's token counts become dollars. Everything
above that -- what an agent is asked to do, what an artifact tree looks like,
what a campaign or an audit even is -- stays in the tool that means it.

The line is drawn from experience rather than taste. `log_cost` existed on the
translation side for months while the audit side's `agentlog` docstring claimed
to follow it; nobody ported it, and three published campaigns priced themselves
from a rate card an agent recalled from memory -- every figure exactly 3x high.
Duplicated-but-drifted shared code is the specific failure this package exists
to prevent, so the test for belonging here is "would a second copy silently
disagree with the first", not "is it generic".
"""
