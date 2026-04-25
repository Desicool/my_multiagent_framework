"""ReadFileTool, WriteFileTool — async file I/O."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles

from beidou.tools.base import BaseTool

if TYPE_CHECKING:
    from beidou.context import AgentContext


class ReadFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "file_read"

    def schema(self) -> dict:
        return {
            "name": "file_read",
            "description": "Read the contents of a file at the given path.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path."},
                },
                "required": ["path"],
            },
        }

    async def execute(self, ctx: "AgentContext", path: str, **_: Any) -> dict:
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        async with aiofiles.open(p) as f:
            content = await f.read()
        return {"path": str(p.resolve()), "content": content}


class WriteFileTool(BaseTool):
    @property
    def name(self) -> str:
        return "file_write"

    def schema(self) -> dict:
        return {
            "name": "file_write",
            "description": "Write content to a file, creating parent directories as needed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path."},
                    "content": {"type": "string", "description": "Text content to write."},
                    "append": {
                        "type": "boolean",
                        "description": "Append to file instead of overwriting. Default false.",
                        "default": False,
                    },
                },
                "required": ["path", "content"],
            },
        }

    async def execute(self, ctx: "AgentContext", path: str, content: str, append: bool = False, **_: Any) -> dict:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        async with aiofiles.open(p, mode) as f:
            await f.write(content)
        return {"path": str(p.resolve()), "bytes_written": len(content.encode())}
