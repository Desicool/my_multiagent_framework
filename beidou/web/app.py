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


class StructuredAnswer(BaseModel):
    selected_labels: list[str] = []
    text: str | None = None


class AnswerBody(BaseModel):
    answers: list[StructuredAnswer]


class SendBody(BaseModel):
    content: str

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)

# Path to the JSONL event directory — used for the questions endpoint.
_EVENTS_DIR = Path.home() / ".beidou" / "events"


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


def _read_jsonl_events(path: Path) -> list[dict]:
    """Read all events from a JSONL file; returns [] if absent or unreadable."""
    events: list[dict] = []
    if not path.exists():
        return events
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def create_app(broker=None, orch=None, task_id=None) -> "FastAPI":
    from beidou import db
    from beidou.web.tail import current_cursor

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
        # Flat team list — root team is the one where parent_team_id is null.
        # The client builds the tree; the server does not synthesize a root node.
        teams = db.get_teams(task_id)
        agents_raw = db.get_agents(task_id=task_id)
        agents = [
            {
                **a,
                "status": _agent_status(a),
                "tools": _parse_json_col(a.get("tools_json")),
                "skills": _parse_json_col(a.get("skills_json")),
            }
            for a in agents_raw
        ]
        # cursor = wall-clock ts of the last event currently in JSONL.
        # If the file doesn't exist yet, cursor = 0.0.
        cursor = current_cursor(task_id)
        return {
            "task": task,
            "teams": teams,
            "agents": agents,
            "stats": db.get_stats(task_id),
            "cursor": cursor,
        }

    @app.get("/api/tasks/{task_id}/events")
    async def api_task_events(
        task_id: str, since: float = 0.0, limit: int = 200
    ) -> dict:
        """Fallback polling endpoint.

        Returns events with ``ts > since`` (strict greater-than — *since* is the
        last cursor the client already applied).  Also returns the new cursor so
        the client can advance its resume point.
        """
        path = _EVENTS_DIR / f"{task_id}.jsonl"
        all_events = _read_jsonl_events(path)
        # Strict greater-than filter.
        filtered = [e for e in all_events if (e.get("ts") or 0.0) > since]
        # Sort ascending by ts, then apply limit.
        filtered.sort(key=lambda e: e.get("ts") or 0.0)
        if len(filtered) > limit:
            filtered = filtered[-limit:]
        cursor = max((e.get("ts") or 0.0 for e in filtered), default=since)
        return {"events": filtered, "cursor": cursor}

    @app.get("/api/agents/{agent_id}")
    async def api_agent(agent_id: str) -> dict:
        """Thin wrapper — returns agent metadata; events come from the task stream."""
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")
        enriched = {
            **agent,
            "status": _agent_status(agent),
            "tools": _parse_json_col(agent.get("tools_json")),
            "skills": _parse_json_col(agent.get("skills_json")),
        }
        return {"agent": enriched, "events": []}

    @app.websocket("/ws/tasks/{task_id}")
    async def ws_task(ws: WebSocket, task_id: str) -> None:
        from beidou.web.tail import current_cursor, tail_events

        await ws.accept()
        since_param = ws.query_params.get("since")
        since_ts: float | None = None
        if since_param:
            try:
                since_ts = float(since_param)
            except ValueError:
                since_ts = None

        # Snapshot the replay boundary BEFORE starting the generator so we know
        # when the replay phase ends and the live phase begins.
        replay_boundary = current_cursor(task_id) if since_ts is not None else None

        gen = tail_events(task_id, since_ts)
        resumed_sent = (since_ts is None)  # live-only mode: no replay, skip marker
        try:
            async for evt in gen:
                evt_ts = evt.get("ts") or 0.0

                # Wrap event in the cursor-protocol envelope.
                await ws.send_json({
                    "type": "event",
                    "event": evt,
                    "cursor": evt_ts,
                })

                # Once we've delivered all replay events (ts <= replay_boundary),
                # send the "resumed" marker so the client knows it's caught up.
                if not resumed_sent and replay_boundary is not None and evt_ts > replay_boundary:
                    await ws.send_json({
                        "type": "resumed",
                        "cursor": replay_boundary,
                    })
                    resumed_sent = True

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
        """Return questions that are still awaiting a user answer.

        The broker's _pending dict is the canonical in-flight handle (needed to
        resolve the future).  We cross-reference with JSONL to filter out
        questions that appear answered in the event log but whose future hasn't
        been resolved yet (race window).  Questions with no broker entry are
        orphaned (agent died) and are NOT returned.
        """
        b = broker  # captured from create_app args
        if b is None:
            raise HTTPException(status_code=503, detail="no active gateway")

        # Build a set of qids that have a matching question_answered event in
        # any live task's JSONL so we can suppress already-answered questions.
        answered_qids: set[str] = set()
        try:
            for jsonl_path in _EVENTS_DIR.glob("*.jsonl"):
                for evt in _read_jsonl_events(jsonl_path):
                    if evt.get("event") == "question_answered":
                        qid = evt.get("qid")
                        if qid:
                            answered_qids.add(qid)
        except OSError:
            pass  # best-effort; broker state is still valid

        questions = []
        for q in list(b._pending.values()):
            if q.state != "at_user":
                continue
            if q.future.done():
                continue
            if q.qid in answered_qids:
                continue
            questions.append({
                "qid": q.qid,
                "asker_agent_id": q.asker_agent_id,
                "questions": q.questions,
                "prompt": q.prompt,
                "context_hint": q.context_hint,
                "chain": q.chain,
                "created_at": q.created_at,
            })
        return {"questions": questions}

    @app.post("/api/questions/{qid}/answer")
    async def api_questions_answer(qid: str, body: AnswerBody):
        b = broker  # captured from create_app args
        if b is None:
            raise HTTPException(status_code=503, detail="no active gateway")
        # Convert pydantic models to plain dicts for the broker
        answers = [{"selected_labels": a.selected_labels, "text": a.text} for a in body.answers]
        result = b.resolve_answer(qid, answers)
        if not result.get("ok"):
            reason = result.get("reason", "unknown")
            # Map known reasons to HTTP statuses
            if reason == "unknown_qid":
                raise HTTPException(status_code=404, detail="unknown_qid")
            if reason == "already_answered":
                raise HTTPException(status_code=409, detail="already_answered")
            if reason == "answer_count_mismatch":
                raise HTTPException(status_code=400, detail="answer_count_mismatch")
            raise HTTPException(status_code=400, detail=reason)
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/send")
    async def api_agents_send(agent_id: str, body: SendBody):
        if orch is None:
            raise HTTPException(status_code=503, detail="no active orchestrator")
        if not orch.agent_exists(agent_id):
            raise HTTPException(status_code=404, detail="unknown agent")
        import time as _time
        import uuid as _uuid
        from beidou.primitives.core import Message as _Message
        mid = _uuid.uuid4().hex
        msg = _Message(
            from_id="user",
            content=body.content,
            ts=_time.time(),
            message_id=mid,
            kind="user",
        )
        try:
            await orch.inbox_put(agent_id, msg)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        orch.emit_event(
            "send_message",
            {
                "ts": msg.ts,
                "caller_id": "user",
                "to": agent_id,
                "content": body.content,
                "message_id": mid,
            },
        )
        return {"ok": True, "message_id": mid}

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
