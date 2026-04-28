"""EventEmitter — writes JSONL to disk and updates aggregated SQLite rollups.

JSONL (~/.beidou/events/{task_id}.jsonl) is the authoritative event log.
SQLite is an aggregated cache only — no per-event table. See docs/observability.md.

One EventEmitter per task. Thread-safe via asyncio lock.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from beidou import db


class EventEmitter:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._events_dir = Path.home() / ".beidou" / "events"
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._events_dir / f"{task_id}.jsonl"
        self._lock = asyncio.Lock()

    async def emit(self, event: str, agent_id: str, team_id: str | None = None, **kwargs) -> None:
        payload = {
            "ts": time.time(),
            "event": event,
            "task_id": self.task_id,
            "agent_id": agent_id,
            "team_id": team_id,
            **kwargs,
        }
        async with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        await asyncio.to_thread(self._update_rollups, event, agent_id, team_id, payload)

    def _update_rollups(self, event: str, agent_id: str, team_id: str | None, p: dict) -> None:
        """Update aggregated SQLite rollup tables.

        Only events that affect rollup columns trigger a DB write. Everything
        else is JSONL-only — no rollup branch needed.
        """
        ts = p["ts"]

        if event == "agent_started":
            db.upsert_agent(
                agent_id=agent_id,
                task_id=self.task_id,
                team_id=team_id,
                model=p.get("model_requested") or p.get("model", ""),
                role=p.get("role", "member"),
                started_at=ts,
                skill=p.get("skill"),
                tools_json=json.dumps(p.get("tools")) if p.get("tools") is not None else None,
                skills_json=json.dumps(p.get("skills")) if p.get("skills") is not None else None,
                system_prompt=p.get("system_prompt"),
                name=p.get("name"),
            )

        elif event == "agent_completed":
            db.complete_agent(agent_id=agent_id, ended_at=ts)

        elif event == "turn.usage":
            # Increment per-turn token counters and llm_calls. USD is NOT
            # available per-turn (see docs/observability.md); it lands on
            # run.cost only. increment_agent_stats also rolls up into tasks.
            db.increment_agent_stats(
                agent_id=agent_id,
                llm_calls=1,
                tokens_in=p.get("input_tokens", 0),
                tokens_out=p.get("output_tokens", 0),
            )

        elif event == "tool_result":
            # Count on tool_result (not tool_called) so only completed
            # roundtrips are counted. duration_ms and is_error are available
            # here for future per-tool aggregation via JSONL.
            db.increment_agent_stats(agent_id=agent_id, tool_calls=1)

        elif event == "run.cost":
            # Terminal event per agent spawn. Carries authoritative USD cost.
            # increment_agent_stats rolls the cost up into tasks.total_cost_usd.
            db.increment_agent_stats(
                agent_id=agent_id,
                cost_usd=p.get("total_cost_usd", 0.0),
            )

        elif event == "team_created":
            db.upsert_team(
                team_id=p.get("new_team_id", ""),
                task_id=self.task_id,
                parent_team_id=p.get("parent_team_id", team_id),
                name=p.get("team_name", ""),
                leader_agent_id=p.get("leader_agent_id", agent_id),
                workspace_path=p.get("workspace_path", ""),
                created_at=ts,
            )

        # All other event types are JSONL-only (no SQLite rollup branch needed).
        #
        # Plan lifecycle events (emitted by orchestrator plan methods):
        #   plan_declared  {agent_id, plan_id, task_count, ts}
        #   plan_removed   {agent_id, plan_id, ts}
        #   task_spawned   {agent_id, plan_id, task_id, spawned_agent_id, team_id, ts}
        #   task_done      {plan_id, task_id, ts}
        #   task_failed    {plan_id, task_id, ts}
        #   task_ready     {plan_id, task_id, ts}   -- emitted per newly-unblocked dep
        #
        # Other JSONL-only: status, config_warning, agent_error,
        # tool_called, contract_violation, question_*
