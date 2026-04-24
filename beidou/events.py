"""EventEmitter — writes JSONL to disk and upserts into SQLite.

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
        await asyncio.to_thread(self._upsert_sqlite, event, agent_id, team_id, payload)

    def _upsert_sqlite(self, event: str, agent_id: str, team_id: str | None, p: dict) -> None:
        ts = p["ts"]
        db.insert_event(
            task_id=self.task_id,
            team_id=team_id,
            agent_id=agent_id,
            event_type=event,
            ts=ts,
            tool_name=p.get("tool"),
            duration_ms=p.get("duration_ms"),
            tokens_in=p.get("tokens_in"),
            tokens_out=p.get("tokens_out"),
            cost_usd=p.get("cost_usd"),
        )
        if event == "agent_started":
            db.upsert_agent(
                agent_id=agent_id,
                task_id=self.task_id,
                team_id=team_id,
                model=p.get("model", ""),
                role=p.get("role", "member"),
                started_at=ts,
                template=p.get("template"),
                tools_json=json.dumps(p.get("tools")) if p.get("tools") is not None else None,
                skills_json=json.dumps(p.get("skills")) if p.get("skills") is not None else None,
                system_prompt=p.get("system_prompt"),
            )
        elif event == "agent_completed":
            db.complete_agent(agent_id=agent_id, ended_at=ts)
        elif event == "llm_response":
            db.increment_agent_stats(
                agent_id=agent_id,
                llm_calls=1,
                tokens_in=p.get("tokens_in", 0),
                tokens_out=p.get("tokens_out", 0),
                cost_usd=p.get("cost_usd", 0.0),
            )
        elif event == "tool_called":
            db.increment_agent_stats(agent_id=agent_id, tool_calls=1)
        elif event == "team_created":
            db.upsert_team(
                team_id=p.get("new_team_id", ""),
                task_id=self.task_id,
                parent_team_id=team_id,
                name=p.get("team_name", ""),
                leader_agent_id=p.get("leader_agent_id", agent_id),
                workspace_path=p.get("workspace_path", ""),
                created_at=ts,
            )
