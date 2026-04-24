"""FastAPI backend for the Beidou web UI. Read-only wrt data stores."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class AnswerBody(BaseModel):
    answer: str

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


def _live_agent_count(task_id: str) -> int:
    from beidou.db import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE task_id=? AND ended_at IS NULL",
            (task_id,),
        )
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def _agent_status(agent: dict) -> str:
    return "running" if agent.get("ended_at") is None else "done"


def _parse_json_col(val: Any) -> list:
    if not val:
        return []
    try:
        parsed = json.loads(val)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _build_team_tree(teams: list[dict], agents: list[dict]) -> list[dict]:
    """Build a nested tree of teams + their agents. Synthesize a 'root' node
    holding task-level (team_id==None) agents so the frontend has a single root."""
    children_by_parent: dict[str | None, list[dict]] = {}
    for t in teams:
        children_by_parent.setdefault(t.get("parent_team_id"), []).append(t)

    agents_by_team: dict[str | None, list[str]] = {}
    for a in agents:
        agents_by_team.setdefault(a.get("team_id"), []).append(a["agent_id"])

    def walk(team_id: str | None) -> list[dict]:
        nodes = []
        for t in children_by_parent.get(team_id, []):
            node = {
                **t,
                "children": walk(t["team_id"]),
                "agents": agents_by_team.get(t["team_id"], []),
            }
            nodes.append(node)
        return nodes

    top_teams = walk(None)
    root_agents = agents_by_team.get(None, [])
    synthetic_root = {
        "team_id": None,
        "task_id": agents[0]["task_id"] if agents else None,
        "parent_team_id": None,
        "name": "root",
        "leader_agent_id": None,
        "workspace_path": None,
        "created_at": None,
        "children": top_teams,
        "agents": root_agents,
    }
    return [synthetic_root]


def create_app(broker=None, task_id=None) -> FastAPI:
    from beidou import db

    app = FastAPI(title="Beidou Web")

    @app.on_event("startup")
    async def _startup() -> None:
        db.init_db()

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/tasks")
    async def api_tasks() -> dict:
        tasks = db.get_tasks(limit=50)
        for t in tasks:
            t["live_agent_count"] = _live_agent_count(t["task_id"])
        return {"tasks": tasks}

    @app.get("/api/tasks/{task_id}")
    async def api_task(task_id: str) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        teams = db.get_teams(task_id)
        agents_raw = db.get_agents(task_id=task_id)
        last_event = db.get_last_event_per_agent(task_id)
        agents = []
        for a in agents_raw:
            aid = a["agent_id"]
            enriched = {
                **a,
                "status": _agent_status(a),
                "current_activity": last_event.get(aid),
            }
            agents.append(enriched)
        tree = _build_team_tree(teams, agents_raw)
        return {
            "task": task,
            "teams": teams,
            "team_tree": tree,
            "agents": agents,
            "stats": db.get_stats(task_id),
            "last_event_per_agent": last_event,
            "snapshot_ts": time.time(),
        }

    @app.get("/api/agents/{agent_id}")
    async def api_agent(agent_id: str) -> dict:
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        events = db.get_events(agent_id=agent_id, limit=200)
        events_asc = list(reversed(events))
        enriched = {
            **agent,
            "status": _agent_status(agent),
            "tools": _parse_json_col(agent.get("tools_json")),
            "skills": _parse_json_col(agent.get("skills_json")),
        }
        return {"agent": enriched, "events": events_asc}

    @app.get("/api/tasks/{task_id}/events")
    async def api_task_events(
        task_id: str, since: float | None = None, limit: int = 200
    ) -> dict:
        events = db.get_events(task_id=task_id, limit=limit)
        if since is not None:
            events = [e for e in events if (e.get("ts") or 0) > since]
        events_asc = sorted(events, key=lambda e: e.get("ts") or 0)
        return {"events": events_asc}

    @app.websocket("/ws/tasks/{task_id}")
    async def ws_task(ws: WebSocket, task_id: str) -> None:
        from beidou.web.tail import tail_events

        await ws.accept()
        since_param = ws.query_params.get("since")
        since_ts: float | None = None
        if since_param:
            try:
                since_ts = float(since_param)
            except ValueError:
                since_ts = None
        gen = tail_events(task_id, since_ts)
        try:
            async for evt in gen:
                await ws.send_json(evt)
        except WebSocketDisconnect:
            pass
        finally:
            aclose = getattr(gen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

    @app.get("/api/questions/pending")
    async def api_questions_pending():
        b = broker  # captured from create_app args
        if b is None:
            raise HTTPException(status_code=503, detail="no active gateway")
        questions = [
            {
                "qid": q.qid,
                "asker_agent_id": q.asker_agent_id,
                "prompt": q.prompt,
                "context_hint": q.context_hint,
                "chain": q.chain,
                "created_at": q.created_at,
            }
            for q in list(b._pending.values())
            if q.state == "at_user" and not q.future.done()
        ]
        return {"questions": questions}

    @app.post("/api/questions/{qid}/answer")
    async def api_questions_answer(qid: str, body: AnswerBody):
        b = broker  # captured from create_app args
        if b is None:
            raise HTTPException(status_code=503, detail="no active gateway")
        q = b._pending.get(qid)
        if q is None:
            raise HTTPException(status_code=404, detail="unknown_qid")
        if q.future.done():
            raise HTTPException(status_code=409, detail="already_answered")
        # Questions in at_user state bypass broker.answer() (which checks for "pending" state)
        # Set result directly on the future, then clean up
        q.state = "answered"
        b._pending.pop(qid, None)
        q.future.set_result(body.answer)
        return {"ok": True}

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
