"""Beidou (北斗) — autonomous multi-agent system."""
__version__ = "0.1.0"

from beidou.orchestrator import Orchestrator
from beidou.agent.loop import RunResult, SpawnSpec, run_agent

__all__ = ["Orchestrator", "RunResult", "SpawnSpec", "run_agent"]
