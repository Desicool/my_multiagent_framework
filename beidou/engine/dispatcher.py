"""Internal event bus for eval handler subscriptions.

This is NOT the JSONL/SQLite ``EventEmitter`` (``beidou/events.py``). It is a
lightweight in-memory pub/sub used by the agent loop to notify eval handlers
at turn boundaries and event subscribers on named Beidou events.

Separate from ``beidou/events.py`` to keep the two concerns decoupled:
 - ``beidou/events.py``: JSONL append + SQLite upsert (durable observability).
 - ``beidou/engine/dispatcher.py``: in-process fire-and-forget notifications.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

# Handler signature: async (event_type: str, payload: dict) -> None
EvalHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class Dispatcher:
    """Simple async event bus for eval handler subscriptions.

    Subscribe/unsubscribe handlers by event type. Dispatch runs all matching
    handlers concurrently and logs exceptions (fail-open semantics).
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EvalHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EvalHandler) -> None:
        """Register a handler for a specific event type."""
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: EvalHandler) -> None:
        """Remove a handler for a specific event type."""
        try:
            self._subscribers[event_type].remove(handler)
        except ValueError:
            pass

    async def dispatch(self, event_type: str, payload: dict[str, Any]) -> None:
        """Fire all handlers subscribed to ``event_type`` concurrently.

        Fail-open: exceptions in individual handlers are logged but do not
        prevent other handlers from running.
        """
        handlers = self._subscribers.get(event_type, [])
        if not handlers:
            return

        results = await asyncio.gather(
            *[handler(event_type, payload) for handler in handlers],
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                _log.warning(
                    "Dispatcher handler %s for event %r raised: %s",
                    getattr(handlers[i], "__name__", handlers[i]),
                    event_type,
                    result,
                )


__all__ = ["Dispatcher"]
