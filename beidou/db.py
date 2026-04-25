"""SQLite aggregated stats — written in real-time by ObservabilityLayer.

SQLite is an aggregated cache only. JSONL (~/.beidou/events/{task_id}.jsonl) is
the authoritative event log. There is no per-event table here. Consumers that
need raw event history should tail the JSONL file directly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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
    team_id          TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    parent_team_id   TEXT,
    name             TEXT,
    leader_agent_id  TEXT,
    workspace_path   TEXT,
    created_at       REAL,
    PRIMARY KEY (team_id, task_id),
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

CREATE INDEX IF NOT EXISTS idx_agents_task    ON agents(task_id);
CREATE INDEX IF NOT EXISTS idx_teams_task     ON teams(task_id);

CREATE TABLE IF NOT EXISTS questions (
    qid           TEXT PRIMARY KEY,
    task_id       TEXT,
    asker_agent_id TEXT,
    prompt        TEXT,
    context_hint  TEXT,
    chain_json    TEXT,
    state         TEXT DEFAULT 'pending',
    created_at    REAL,
    answered_at   REAL,
    answer        TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_questions_task ON questions(task_id);
"""

# Migration: executed once at init_db() to clean up retired tables or tables
# whose schema has changed.  The teams table is dropped here so that existing
# single-PK DBs are replaced by the composite-PK version on first upgrade.
# SQLite is an aggregated cache (see docs/observability.md), so this is safe.
_MIGRATIONS = """
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS teams;
"""


def init_db() -> None:
    BEIDOU_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Drop the old per-event table before applying the current schema so that
    # any existing stats.db is cleaned up on first upgrade.
    conn.executescript(_MIGRATIONS)
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


def insert_question(
    qid: str,
    task_id: str,
    asker_agent_id: str,
    prompt: str,
    context_hint: str | None,
    chain_json: str,
    created_at: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO questions
              (qid, task_id, asker_agent_id, prompt, context_hint, chain_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (qid, task_id, asker_agent_id, prompt, context_hint, chain_json, created_at),
        )


def update_question_answered(qid: str, answer: str, answered_at: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE questions SET state='answered', answer=?, answered_at=? WHERE qid=?",
            (answer, answered_at, qid),
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
    """Stub — always returns [].

    The events table has been removed as part of the sink-demotion refactor
    (see docs/observability.md). Raw event history lives in JSONL:
        ~/.beidou/events/{task_id}.jsonl

    A future subagent will rewrite the web backend to tail JSONL directly via
    beidou/web/tail.py. Until then, callers of this function receive an empty
    list rather than crashing on a missing table.
    """
    _ = (task_id, team_id, agent_id, limit)  # suppress unused-arg warnings
    return []


def get_agent(agent_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    return dict(row) if row else None


def get_last_event_per_agent(task_id: str) -> dict[str, dict]:
    """Stub — always returns {}.

    Previously queried the now-dropped events table. A future subagent will
    reimplement this by reducing the JSONL stream for the task.
    See docs/observability.md for the JSONL schema.
    """
    _ = task_id  # suppress unused-arg warning
    return {}


def get_stats(task_id: str) -> dict:
    """Return aggregated stats from the tasks/teams/agents rollup tables.

    top_tools is now always [] — it previously queried the now-dropped events
    table. A future subagent will reimplement per-tool counts by reducing
    tool_result events from the JSONL stream.
    """
    with _connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        teams = conn.execute(
            "SELECT COUNT(*) as cnt FROM teams WHERE task_id=?", (task_id,)
        ).fetchone()
        agents = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(tool_calls) as tc, SUM(llm_calls) as lc FROM agents WHERE task_id=?",
            (task_id,),
        ).fetchone()
    return {
        "task": dict(task) if task else {},
        "team_count": teams["cnt"] if teams else 0,
        "agent_count": agents["cnt"] if agents else 0,
        "total_tool_calls": agents["tc"] or 0 if agents else 0,
        "total_llm_calls": agents["lc"] or 0 if agents else 0,
        # top_tools is empty until the web backend reads JSONL directly.
        # See docs/observability.md for the tool_result event schema.
        "top_tools": [],
    }


