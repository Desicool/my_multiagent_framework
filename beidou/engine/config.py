"""Engine configuration dataclasses — the engine/agent data boundary.

``AgentConfig`` is PURE DATA only: no runtime objects cross this boundary,
only paths, strings, lists, and optional scalars. The engine creates an
``AgentConfig`` per spawn; the agent loop consumes it.

``EngineConfig`` captures the immutable settings the orchestrator needs at
construction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class EngineConfig:
    """Immutable run-level settings passed to the orchestrator at construction.

    These are the non-runtime values the orchestrator carries; the runtime
    state (registries, locks, queues, background tasks) is built lazily.
    """

    task_id: str
    emitter: object  # EventEmitter (avoiding import to keep engine SDK-agnostic)
    skill_root: Path
    gateway: object | None  # BaseGateway | None
    default_model: str | None = None
    project_workspace: Path | None = None


@dataclass
class AgentConfig:
    """Single data boundary between engine and agent loop. Pure data only.

    The engine creates this per spawn; the agent loop consumes it.
    No runtime objects cross this boundary — only paths, strings, and lists.
    """

    agent_id: str
    team_id: str | None
    leader_id: str
    task_id: str

    # Skill (resolved by engine, passed as plain data)
    skill_name: str
    skill_version: str
    rendered_system_prompt: str       # fully substituted, ready to pass to SDK
    allowed_tools: list[str]          # namespaced, ready for ClaudeAgentOptions

    # Runtime config
    model: str | None
    cwd: str | None
    max_turns: int | None
    max_budget_usd: float | None
    resume_session_id: str | None

    # Module path (engine resolves this; agent loop loads hooks from it)
    module_path: str | None = None    # path to skill dir with module.toml, or None


__all__ = ["AgentConfig", "EngineConfig"]
