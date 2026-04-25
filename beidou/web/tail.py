"""Async tail of JSONL event files under ~/.beidou/events/{task_id}.jsonl."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

EVENTS_DIR = Path.home() / ".beidou" / "events"

# How many bytes to read from the end of the file when seeking the last line.
_TAIL_CHUNK = 4096


def current_cursor(task_id: str) -> float:
    """Return the max ``ts`` currently written to the JSONL file.

    Reads the last few kilobytes from the end of the file (efficient seek-from-end).
    Returns 0.0 when the file is absent, empty, or contains no parseable last line.
    """
    path = EVENTS_DIR / f"{task_id}.jsonl"
    if not path.exists():
        return 0.0
    try:
        size = path.stat().st_size
        if size == 0:
            return 0.0
        with path.open("rb") as fh:
            # Read the last _TAIL_CHUNK bytes (or entire file if smaller).
            read_size = min(_TAIL_CHUNK, size)
            fh.seek(-read_size, 2)
            chunk = fh.read(read_size)
        # Split into lines and find the last non-empty parseable one.
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                ts = evt.get("ts")
                if ts is not None:
                    return float(ts)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    except OSError:
        pass
    return 0.0


async def tail_events(
    task_id: str, since_ts: float | None = None
) -> AsyncIterator[dict]:
    """Async generator that yields events from the JSONL file.

    If *since_ts* is provided, replays all events with ``ts > since_ts``
    (strict greater-than) and then continues tailing new lines.

    If *since_ts* is ``None``, seeks to EOF immediately and yields only
    new lines written after this call.

    The yielded shape is a plain ``dict`` per event — unchanged from before.
    """
    path = EVENTS_DIR / f"{task_id}.jsonl"
    while not path.exists():
        await asyncio.sleep(0.1)
    with path.open("r", encoding="utf-8") as fh:
        if since_ts is None:
            fh.seek(0, 2)  # EOF — live-only mode
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
                # Strict greater-than: skip events at or before the cursor.
                if ts is None or ts <= since_ts:
                    continue
            yield evt
