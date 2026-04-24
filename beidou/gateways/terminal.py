"""TerminalGateway — routes questions to stdin/stdout. Preserves current behavior exactly."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from beidou.gateways.base import BaseGateway

if TYPE_CHECKING:
    from beidou.inbox import Question, QuestionBroker


class TerminalGateway(BaseGateway):
    def __init__(self, console=None) -> None:
        self._console = console
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def surface_question(self, q: "Question", broker: "QuestionBroker") -> None:
        asyncio.create_task(self._do_surface(q, broker))

    async def _do_surface(self, q: "Question", broker: "QuestionBroker") -> None:
        async with self._lock:
            if q.future.done():
                return
            loop = asyncio.get_running_loop()
            chain_str = " → ".join(q.chain)
            prompt_txt = (
                f"\n[bold yellow]Question from {q.asker_agent_id}[/]  "
                f"[dim](chain: {chain_str})[/dim]\n"
                f"{q.prompt}\n"
            )
            if q.context_hint:
                prompt_txt += f"[dim]context: {q.context_hint}[/dim]\n"
            prompt_txt += "[bold green]Answer:[/] "
            try:
                if self._console is not None:
                    answer = await loop.run_in_executor(
                        None, lambda: self._console.input(prompt_txt)
                    )
                else:
                    answer = await loop.run_in_executor(None, lambda: input(prompt_txt))
            except Exception as exc:
                if not q.future.done():
                    q.future.set_exception(exc)
                return
            if not q.future.done():
                q.future.set_result(answer)
