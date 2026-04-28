"""TUIGateway — full-screen Textual TUI with agent tree, event feed, and question panel."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from beidou.gateways.base import BaseGateway

# Guard against importing textual at module level (optional dep)
_textual_available = False
try:
    import textual  # noqa: F401
    _textual_available = True
except ImportError:
    pass


class TUIGateway(BaseGateway):
    def __init__(self, task_id: str) -> None:
        self._task_id = task_id
        self._question_queue: asyncio.Queue = asyncio.Queue()
        self._app = None
        self._tui_task: asyncio.Task | None = None
        self.orch: Any = None  # set after orchestrator is created, before start()

    async def start(self) -> None:
        if not _textual_available:
            raise RuntimeError(
                "textual is not installed. Run: pip install 'beidou[tui]'  "
                "or: pip install textual>=0.60.0"
            )
        app = BeidouTUIApp(
            task_id=self._task_id,
            question_queue=self._question_queue,
            orch=self.orch,
        )
        self._app = app
        self._tui_task = asyncio.create_task(app.run_async())

    async def stop(self) -> None:
        if self._app is not None:
            try:
                self._app.exit()
            except Exception:
                pass
        if self._tui_task is not None:
            try:
                await asyncio.wait_for(self._tui_task, timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def surface_question(self, qid: str, body: str, questions: list[dict]) -> None:
        await self._question_queue.put((qid, body, questions))


if _textual_available:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import Footer, Header, Input, Label, RichLog, Static

    class AgentTreeWidget(Static):
        """Live agent tree — rebuilt each refresh from the event log."""

        DEFAULT_CSS = """
        AgentTreeWidget {
            width: 1fr;
            height: 1fr;
            overflow-y: auto;
            padding: 1;
        }
        """

        def __init__(self, task_id: str, **kwargs):
            super().__init__(**kwargs)
            self._task_id = task_id
            self._agents: dict[str, dict] = {}

        def on_agent_event(self, event: dict) -> None:
            aid = event.get("agent_id", "")
            if not aid:
                return
            if aid not in self._agents:
                self._agents[aid] = {
                    "role": event.get("role", "agent"),
                    "model": event.get("model", ""),
                    "status": "running",
                    "tool_calls": 0,
                }
            ag = self._agents[aid]
            if event.get("event") == "agent_completed":
                ag["status"] = "done"
            elif event.get("event") == "tool_called":
                ag["tool_calls"] = ag.get("tool_calls", 0) + 1
            self._refresh_tree()

        def _refresh_tree(self) -> None:
            lines = ["[bold cyan]Agents[/bold cyan]\n"]
            for aid, ag in self._agents.items():
                status_icon = "\U0001f7e2" if ag["status"] == "running" else "✅"
                lines.append(
                    f"  {status_icon} [{aid[-6:]}] {ag['role']}"
                    f"  [dim]{ag.get('model','').replace('claude-','')[:12]}[/dim]"
                    f"  [dim]tools:{ag.get('tool_calls',0)}[/dim]"
                )
            self.update("\n".join(lines))

    class EventFeedWidget(RichLog):
        """Scrolling event feed."""

        DEFAULT_CSS = """
        EventFeedWidget {
            width: 2fr;
            height: 1fr;
            border: solid $primary;
        }
        """

        def __init__(self, **kwargs):
            super().__init__(highlight=True, markup=True, wrap=False, **kwargs)

        def on_agent_event(self, event: dict) -> None:
            ev = event.get("event", "")
            aid = (event.get("agent_id") or "")[-6:]
            ts_str = ""
            ts = event.get("ts")
            if ts:
                import datetime
                ts_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

            if ev == "llm_response":
                self.write(f"[dim]{ts_str}[/dim] [cyan]{aid}[/cyan] llm ← {event.get('tokens_out','?')}tok")
            elif ev == "tool_called":
                self.write(f"[dim]{ts_str}[/dim] [yellow]{aid}[/yellow] tool: {event.get('tool_name','?')}")
            elif ev == "agent_started":
                self.write(f"[dim]{ts_str}[/dim] [green]{aid}[/green] started ({event.get('role','?')})")
            elif ev == "agent_completed":
                self.write(f"[dim]{ts_str}[/dim] [dim]{aid}[/dim] done")
            elif ev == "question_asked":
                self.write(f"[dim]{ts_str}[/dim] [bold magenta]{aid}[/bold magenta] ❓ {event.get('prompt','')[:60]}")
            else:
                self.write(f"[dim]{ts_str}[/dim] {ev}")

    class QuestionPanel(Static):
        """Bottom panel for answering questions. Hidden when no question pending."""

        DEFAULT_CSS = """
        QuestionPanel {
            height: 7;
            padding: 1;
            border: solid $warning;
            display: none;
        }
        QuestionPanel.visible {
            display: block;
        }
        """

        def compose(self) -> ComposeResult:
            yield Label("", id="q_prompt")
            yield Input(placeholder="Type your answer and press Enter…", id="q_input")

        def show_question(self, prompt: str) -> None:
            self.query_one("#q_prompt", Label).update(f"❓ {prompt[:120]}")
            self.add_class("visible")
            self.query_one("#q_input", Input).focus()

        def hide(self) -> None:
            self.remove_class("visible")
            self.query_one("#q_input", Input).value = ""

    class BeidouTUIApp(App):
        """Full-screen Beidou TUI."""

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("ctrl+c", "quit", "Quit"),
        ]

        CSS = """
        Screen {
            layout: vertical;
        }
        #main_area {
            layout: horizontal;
            height: 1fr;
        }
        """

        def __init__(self, task_id: str, question_queue: asyncio.Queue, orch=None, **kwargs):
            super().__init__(**kwargs)
            self._task_id = task_id
            self._question_queue = question_queue
            self._orch = orch
            self._current_qid: str | None = None
            self._current_questions: list[dict] = []

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="main_area"):
                yield AgentTreeWidget(task_id=self._task_id, id="agent_tree")
                yield EventFeedWidget(id="event_feed")
            yield QuestionPanel(id="q_panel")
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"北斗 Beidou — {self._task_id}"
            self.set_interval(0.5, self._poll_events)
            self.set_interval(0.1, self._poll_questions)

        def _poll_events(self) -> None:
            """Tail JSONL event file and update widgets."""
            log_path = Path.home() / ".beidou" / "events" / f"{self._task_id}.jsonl"
            if not log_path.exists():
                return
            # Simple approach: read all lines each tick (file is small for single task)
            try:
                lines = log_path.read_text().splitlines()
            except Exception:
                return
            tree = self.query_one("#agent_tree", AgentTreeWidget)
            feed = self.query_one("#event_feed", EventFeedWidget)
            # Track how many we've processed
            processed = getattr(self, "_processed_lines", 0)
            new_lines = lines[processed:]
            self._processed_lines = len(lines)
            for line in new_lines:
                try:
                    evt = json.loads(line)
                    tree.on_agent_event(evt)
                    feed.on_agent_event(evt)
                except Exception:
                    pass

        def _poll_questions(self) -> None:
            """Check question queue and show panel."""
            if self._current_qid is not None:
                return  # Already showing a question
            try:
                qid, body, questions = self._question_queue.get_nowait()
                self._current_qid = qid
                self._current_questions = questions
                panel = self.query_one("#q_panel", QuestionPanel)
                panel.show_question(body[:120])
            except asyncio.QueueEmpty:
                pass

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id != "q_input":
                return
            answer = event.value.strip()
            if not answer or self._current_qid is None:
                return
            qid = self._current_qid
            questions = self._current_questions
            self._current_qid = None
            self._current_questions = []
            # Route through the shared resolver so DB writes and question_answered
            # event are consistent with the web/terminal paths.
            if self._orch is not None:
                self._orch.resolve_question(
                    qid,
                    [{"selected_labels": [], "text": answer} for _ in questions],
                )
            panel = self.query_one("#q_panel", QuestionPanel)
            panel.hide()

        def action_quit(self) -> None:
            self.exit()
