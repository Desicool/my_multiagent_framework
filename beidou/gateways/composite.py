"""CompositeGateway — fans out surface_question to multiple gateways; first answer wins."""
from __future__ import annotations

import asyncio

from beidou.gateways.base import BaseGateway


class CompositeGateway(BaseGateway):
    def __init__(self, gateways: list[BaseGateway]) -> None:
        self._gateways = gateways

    async def start(self) -> None:
        await asyncio.gather(*[g.start() for g in self._gateways])

    async def stop(self) -> None:
        await asyncio.gather(*[g.stop() for g in self._gateways], return_exceptions=True)

    async def surface_question(self, qid: str, body: str, questions: list[dict]) -> None:
        await asyncio.gather(*[g.surface_question(qid, body, questions) for g in self._gateways])
