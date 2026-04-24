"""CompositeGateway — fans out surface_question to multiple gateways; first answer wins."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from beidou.gateways.base import BaseGateway

if TYPE_CHECKING:
    from beidou.inbox import Question, QuestionBroker


class CompositeGateway(BaseGateway):
    def __init__(self, gateways: list[BaseGateway]) -> None:
        self._gateways = gateways

    async def start(self) -> None:
        await asyncio.gather(*[g.start() for g in self._gateways])

    async def stop(self) -> None:
        await asyncio.gather(*[g.stop() for g in self._gateways], return_exceptions=True)

    async def surface_question(self, q: "Question", broker: "QuestionBroker") -> None:
        await asyncio.gather(*[g.surface_question(q, broker) for g in self._gateways])
