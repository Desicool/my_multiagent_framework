"""Async tail of JSONL event files under ~/.beidou/events/{task_id}.jsonl."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

EVENTS_DIR = Path.home() / ".beidou" / "events"


async def tail_events(
    task_id: str, since_ts: float | None = None
) -> AsyncIterator[dict]:
    path = EVENTS_DIR / f"{task_id}.jsonl"
    while not path.exists():
        await asyncio.sleep(0.1)
    with path.open("r", encoding="utf-8") as fh:
        if since_ts is None:
            fh.seek(0, 2)  # EOF
        while True:
            line = fh.readline()
            if not line:
                await asyncio.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_ts is not None:
                ts = evt.get("ts")
                if ts is None or ts <= since_ts:
                    continue
            yield evt
