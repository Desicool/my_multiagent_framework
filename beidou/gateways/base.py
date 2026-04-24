"""BaseGateway — ABC for all question-routing channels."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beidou.inbox import Question, QuestionBroker


class BaseGateway(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Start the gateway (e.g., poll bot, open server). Called before agent.run()."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the gateway. Called in finally block after agent completes."""
        ...

    @abstractmethod
    async def surface_question(self, q: "Question", broker: "QuestionBroker") -> None:
        """Present question to the channel. MUST return immediately (use asyncio.create_task).
        Call broker.answer(q.qid, text) when the user provides an answer."""
        ...
