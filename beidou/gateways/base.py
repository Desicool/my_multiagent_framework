"""BaseGateway — ABC for all question-routing channels."""
from __future__ import annotations
from abc import ABC, abstractmethod


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
    async def surface_question(self, qid: str, body: str, questions: list[dict]) -> None:
        """Present a question to the channel. MUST return immediately (use asyncio.create_task).

        To resolve the question, call orch.resolve_question(qid, answers) where orch is
        the orchestrator the gateway has a reference to. No broker reference is needed.
        """
        ...
