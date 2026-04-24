"""SQLite aggregated stats — written in real-time by ObservabilityLayer."""
from __future__ import annotations

import sqlite3
from pathlib import Path

BEIDOU_DIR = Path.home() / ".beidou"
DB_PATH = BEIDOU_DIR / "stats.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    description   TEXT,
    model         TEXT,
    template      TEXT,
    started_at    REAL,
    ended_at      REAL,
    status        TEXT DEFAULT 'running',
    total_cost_usd REAL DEFAULT 0,
    total_tokens  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS teams (
    team_id          TEXT PRIMARY KEY,
    task_id          TEXT,
    parent_team_id   TEXT,
    name             TEXT,
    leader_agent_id  TEXT,
    workspace_path   TEXT,
    created_at       REAL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id     TEXT PRIMARY KEY,
    task_id      TEXT,
    team_id      TEXT,
    model        TEXT,
    role         TEXT,
    started_at   REAL,
    ended_at     REAL,
    tool_calls   INTEGER DEFAULT 0,
    llm_calls    INTEGER DEFAULT 0,
    cost_usd     REAL DEFAULT 0,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    template        TEXT,
    tools_json      TEXT,
    skills_json     TEXT,
    system_prompt   TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT,
    team_id      TEXT,
    agent_id     TEXT,
    event_type   TEXT,
    tool_name    TEXT,
    duration_ms  REAL,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    cost_usd     REAL,
    ts           REAL,
    extra        TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_task    ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_team    ON events(team_id);
CREATE INDEX IF NOT EXISTS idx_events_agent   ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_agents_task    ON agents(task_id);
CREATE INDEX IF NOT EXISTS idx_teams_task     ON teams(task_id);
"""


def init_db() -> None:
    BEIDOU_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    _migrate_agents_columns(conn)
    conn.close()


def _migrate_agents_columns(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE for pre-existing DBs that lack the new agent columns."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
    for col, decl in (
        ("template", "TEXT"),
        ("tools_json", "TEXT"),
        ("skills_json", "TEXT"),
        ("system_prompt", "TEXT"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {decl}")
    conn.commit()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------ #
# Write helpers (called by ObservabilityLayer)                         #
# ------------------------------------------------------------------ #

def upsert_task(
    task_id: str,
    description: str,
    model: str,
    template: str,
    started_at: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tasks (task_id, description, model, template, started_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, description, model, template, started_at),
        )


def complete_task(task_id: str, ended_at: float, status: str = "completed") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET ended_at=?, status=? WHERE task_id=?",
            (ended_at, status, task_id),
        )


def upsert_team(
    team_id: str,
    task_id: str,
    parent_team_id: str | None,
    name: str,
    leader_agent_id: str,
    workspace_path: str,
    created_at: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO teams
              (team_id, task_id, parent_team_id, name, leader_agent_id, workspace_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (team_id, task_id, parent_team_id, name, leader_agent_id, workspace_path, created_at),
        )


def upsert_agent(
    agent_id: str,
    task_id: str,
    team_id: str | None,
    model: str,
    role: str,
    started_at: float,
    template: str | None = None,
    tools_json: str | None = None,
    skills_json: str | None = None,
    system_prompt: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO agents
              (agent_id, task_id, team_id, model, role, started_at,
               template, tools_json, skills_json, system_prompt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_id, task_id, team_id, model, role, started_at,
             template, tools_json, skills_json, system_prompt),
        )


def complete_agent(agent_id: str, ended_at: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE agents SET ended_at=? WHERE agent_id=?",
            (ended_at, agent_id),
        )


def increment_agent_stats(
    agent_id: str,
    llm_calls: int = 0,
    tool_calls: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agents SET
                llm_calls  = llm_calls  + ?,
                tool_calls = tool_calls + ?,
                tokens_in  = tokens_in  + ?,
                tokens_out = tokens_out + ?,
                cost_usd   = cost_usd   + ?
            WHERE agent_id=?
            """,
            (llm_calls, tool_calls, tokens_in, tokens_out, cost_usd, agent_id),
        )
    # Also roll up into task totals
    with _connect() as conn:
        row = conn.execute("SELECT task_id FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if row:
            conn.execute(
                """
                UPDATE tasks SET
                    total_cost_usd = total_cost_usd + ?,
                    total_tokens   = total_tokens   + ?
                WHERE task_id=?
                """,
                (cost_usd, tokens_in + tokens_out, row["task_id"]),
            )


def insert_event(
    task_id: str,
    team_id: str | None,
    agent_id: str,
    event_type: str,
    ts: float,
    tool_name: str | None = None,
    duration_ms: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    extra: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO events
              (task_id, team_id, agent_id, event_type, tool_name, duration_ms,
               tokens_in, tokens_out, cost_usd, ts, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, team_id, agent_id, event_type, tool_name, duration_ms,
             tokens_in, tokens_out, cost_usd, ts, extra),
        )


# ------------------------------------------------------------------ #
# Read helpers (called by CLI)                                         #
# ------------------------------------------------------------------ #

def get_tasks(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_task(task_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def get_teams(task_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM teams WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_agents(task_id: str | None = None, team_id: str | None = None) -> list[dict]:
    with _connect() as conn:
        if task_id and team_id:
            rows = conn.execute(
                "SELECT * FROM agents WHERE task_id=? AND team_id=? ORDER BY started_at",
                (task_id, team_id),
            ).fetchall()
        elif task_id:
            rows = conn.execute(
                "SELECT * FROM agents WHERE task_id=? ORDER BY started_at", (task_id,)
            ).fetchall()
        elif team_id:
            rows = conn.execute(
                "SELECT * FROM agents WHERE team_id=? ORDER BY started_at", (team_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM agents ORDER BY started_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]


def get_events(
    task_id: str | None = None,
    team_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if team_id:
        clauses.append("team_id=?")
        params.append(team_id)
    if agent_id:
        clauses.append("agent_id=?")
        params.append(agent_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def get_agent(agent_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    return dict(row) if row else None


def get_last_event_per_agent(task_id: str) -> dict[str, dict]:
    """Return {agent_id: latest_event_row} for every agent in the task.

    One query: a correlated subquery that picks the max ts per agent.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.*
            FROM events e
            JOIN (
                SELECT agent_id, MAX(ts) AS max_ts
                FROM events
                WHERE task_id = ?
                GROUP BY agent_id
            ) latest
              ON e.agent_id = latest.agent_id
             AND e.ts       = latest.max_ts
            WHERE e.task_id = ?
            """,
            (task_id, task_id),
        ).fetchall()
    return {r["agent_id"]: dict(r) for r in rows if r["agent_id"]}


def get_stats(task_id: str) -> dict:
    with _connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        teams = conn.execute(
            "SELECT COUNT(*) as cnt FROM teams WHERE task_id=?", (task_id,)
        ).fetchone()
        agents = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(tool_calls) as tc, SUM(llm_calls) as lc FROM agents WHERE task_id=?",
            (task_id,),
        ).fetchone()
        top_tools = conn.execute(
            """
            SELECT tool_name, COUNT(*) as cnt, SUM(duration_ms) as total_ms
            FROM events
            WHERE task_id=? AND event_type='tool_called' AND tool_name IS NOT NULL
            GROUP BY tool_name ORDER BY cnt DESC LIMIT 10
            """,
            (task_id,),
        ).fetchall()
    return {
        "task": dict(task) if task else {},
        "team_count": teams["cnt"] if teams else 0,
        "agent_count": agents["cnt"] if agents else 0,
        "total_tool_calls": agents["tc"] or 0 if agents else 0,
        "total_llm_calls": agents["lc"] or 0 if agents else 0,
        "top_tools": [dict(r) for r in top_tools],
    }


from typing import Any  # noqa: E402 — placed after usage to satisfy F821
