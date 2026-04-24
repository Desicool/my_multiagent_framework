"""WebGateway — serves the Beidou web UI and routes questions to the browser."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from typing import TYPE_CHECKING

from beidou.gateways.base import BaseGateway

if TYPE_CHECKING:
    from beidou.inbox import Question, QuestionBroker


def _find_free_port(preferred: int) -> int:
    """Try preferred port first; fall back to any available port."""
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_browser(url: str) -> None:
    """Open a browser silently. Handles WSL2, native Linux, macOS."""
    if os.environ.get("WSL_DISTRO_NAME"):
        for cmd in (["wslview", url], ["cmd.exe", "/c", "start", url]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
        return
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        import contextlib
        import io
        import webbrowser
        with contextlib.redirect_stderr(io.StringIO()):
            webbrowser.open(url)


class WebGateway(BaseGateway):
    def __init__(
        self,
        broker: "QuestionBroker",
        task_id: str,
        host: str = "127.0.0.1",
        port: int = 7777,
        auto_open: bool = False,
    ) -> None:
        self._broker = broker
        self._task_id = task_id
        self._host = host
        self._port = port
        self._auto_open = auto_open
        self._server = None
        self._server_task: asyncio.Task | None = None
        self._url = ""

    async def start(self) -> None:
        import uvicorn
        from beidou.web.app import create_app

        actual_port = _find_free_port(self._port)
        app = create_app(broker=self._broker, task_id=self._task_id)
        config = uvicorn.Config(app, host=self._host, port=actual_port, log_level="warning")
        server = uvicorn.Server(config)
        self._server = server
        self._port = actual_port

        self._server_task = asyncio.create_task(server.serve())

        for _ in range(30):
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.1)

        self._url = f"http://{self._host}:{actual_port}"
        task_url = f"{self._url}#/tasks/{self._task_id}"

        try:
            from beidou.cli import console as _console
            _console.print(
                f"[bold cyan]Web UI:[/bold cyan] {task_url}"
                f"  [dim](open this URL — questions will appear there)[/dim]"
            )
        except Exception:
            print(f"Web UI: {task_url}")

        if self._auto_open:
            _open_browser(task_url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def surface_question(self, q: "Question", broker: "QuestionBroker") -> None:
        # Print a terminal notice so the user knows to check the browser.
        # The question is surfaced in the web UI via the JSONL WebSocket stream
        # (question_asked event) + /api/questions/pending polling.
        # The Future is resolved when the user submits via POST /api/questions/{qid}/answer.
        task_url = f"{self._url}#/tasks/{self._task_id}" if self._url else "the web UI"
        try:
            from beidou.cli import console as _console
            _console.print(
                f"\n[bold magenta]❓ Question from {q.asker_agent_id}[/bold magenta]"
                f" — answer in browser: [cyan]{task_url}[/cyan]\n"
                f"[dim]{q.prompt[:120]}[/dim]"
            )
        except Exception:
            print(f"\nQuestion waiting at: {task_url}\n{q.prompt[:120]}")
