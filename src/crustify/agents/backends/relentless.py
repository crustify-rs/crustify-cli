"""The original kiss ``RelentlessAgent`` path, behind the Backend protocol.

This preserves the pre-backend behaviour verbatim: the same four kiss
``UsefulTools`` callables, the same ``RelentlessAgent.run`` invocation. Kept
as a selectable fallback while the Agents SDK backend is the default.
"""
from __future__ import annotations

import threading

from kiss.agents.sorcar.useful_tools import UsefulTools
from kiss.core.printer import Printer
from kiss.core.relentless_agent import RelentlessAgent


class RelentlessBackend:
    def run(
        self,
        *,
        name: str,
        model: str,
        prompt_template: str,
        arguments: dict,
        work_dir: str,
        printer: Printer | None,
    ) -> None:
        useful = UsefulTools(stop_event=threading.Event())
        agent = RelentlessAgent(name)
        agent.run(
            model_name=model,
            prompt_template=prompt_template,
            arguments=arguments,
            tools=[useful.Bash, useful.Read, useful.Edit, useful.Write],
            work_dir=work_dir,
            printer=printer,
            verbose=False,  # output is managed via the printer
        )
