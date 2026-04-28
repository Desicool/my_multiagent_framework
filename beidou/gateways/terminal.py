"""TerminalGateway — routes questions to stdin/stdout."""
from __future__ import annotations

import asyncio
from typing import Any

from beidou.gateways.base import BaseGateway


class TerminalGateway(BaseGateway):
    def __init__(self, console=None) -> None:
        self._console = console
        # Serialize stdin so concurrent escalations don't interleave.
        self._lock = asyncio.Lock()
        self.orch: Any = None  # set after orchestrator is created, before start()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def surface_question(self, qid: str, body: str, questions: list[dict]) -> None:
        asyncio.create_task(self._do_surface(qid, body, questions))

    async def _do_surface(self, qid: str, body: str, questions: list[dict]) -> None:
        async with self._lock:
            # Check if already answered (race between gateway surface and resolve).
            registry = getattr(self.orch, "_questions", None) if self.orch else None
            if registry is not None:
                pq = registry.get(qid)
                if pq is None or pq.future.done():
                    return
            loop = asyncio.get_running_loop()
            prompt_txt = f"\n{body}\n[bold green]Answer:[/] "
            try:
                if self._console is not None:
                    answer = await loop.run_in_executor(
                        None, lambda: self._console.input(prompt_txt)
                    )
                else:
                    answer = await loop.run_in_executor(None, lambda: input(prompt_txt))
            except Exception as exc:
                # If the future is still pending, propagate the exception.
                if registry is not None:
                    pq = registry.get(qid)
                    if pq is not None and not pq.future.done():
                        pq.future.set_exception(exc)
                return
            if self.orch is not None:
                self.orch.resolve_question(
                    qid,
                    [{"selected_labels": [], "text": answer} for _ in questions],
                )
