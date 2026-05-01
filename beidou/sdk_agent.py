"""DEPRECATED — re-export shim for backward compatibility.

This module used to contain the SDK agent spawn loop and built-in hooks.
That code now lives in ``beidou/agent/loop.py`` and ``beidou/agent/hooks.py``.

All imports from this module are still supported but new code should import
directly from ``beidou.agent``:

    from beidou.agent.loop import run_agent, SpawnSpec, RunResult
    from beidou.agent.hooks import build_hooks, HOOK_REVIEW_TIMEOUT_S

``query`` is re-exported from ``claude_agent_sdk`` so that tests can monkeypatch
``sdk_agent.query`` as they did before the refactor.

This file will be removed in a future release.
"""

# Re-export query so tests can monkeypatch sdk_agent.query.
from claude_agent_sdk import query  # noqa: F401

from beidou.agent.loop import RunResult, SpawnSpec, _resolve_skill, run_agent
from beidou.agent.hooks import HOOK_REVIEW_TIMEOUT_S, build_hooks

__all__ = [
    "HOOK_REVIEW_TIMEOUT_S",
    "RunResult",
    "SpawnSpec",
    "build_hooks",
    "query",
    "run_agent",
]
