"""ShellTool — run a shell command via asyncio subprocess."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from beidou.tools.base import BaseTool

if TYPE_CHECKING:
    from beidou.context import AgentContext

_TIMEOUT = 120  # seconds


class ShellTool(BaseTool):
    @property
    def name(self) -> str:
        return "bash"

    def schema(self) -> dict:
        return {
            "name": "bash",
            "description": (
                "Run a shell command and return stdout + stderr. "
                "Avoid interactive commands. Timeout: 120 s."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute."},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120).",
                        "default": 120,
                    },
                },
                "required": ["command"],
            },
        }

    async def execute(self, ctx: "AgentContext", command: str, timeout: int = _TIMEOUT, **_: Any) -> dict:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": f"Command timed out after {timeout}s", "returncode": -1}

        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }
