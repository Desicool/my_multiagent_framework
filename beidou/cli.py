"""Beidou CLI entry point."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

load_dotenv()

console = Console()


def _ensure_db() -> None:
    from beidou.db import init_db

    init_db()


# ------------------------------------------------------------------ #
# CLI group                                                            #
# ------------------------------------------------------------------ #

@click.group()
def main() -> None:
    """Beidou (北斗) — autonomous multi-agent system."""


# ------------------------------------------------------------------ #
# init                                                                 #
# ------------------------------------------------------------------ #

@main.command()
def init() -> None:
    """Initialize ~/.beidou/stats.db."""
    from beidou.db import DB_PATH, init_db

    init_db()
    console.print(f"[green]Initialized[/green] {DB_PATH}")


# ------------------------------------------------------------------ #
# run                                                                  #
# ------------------------------------------------------------------ #

@main.command()
@click.argument("task")
@click.option("--workspace", "workspace", required=True,
              type=click.Path(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
              help="Project workspace directory. Beidou writes team scratch under {WORKSPACE}/.beidou/tasks/{task_id}/teams/, and agents read shared files from this directory via absolute paths.")
@click.option("--model", "-m", default="claude-sonnet-4-6", show_default=True, help="Anthropic model ID.")
@click.option("--skill", "-s", default="orchestrator", show_default=True, help="Agent skill name (e.g. orchestrator).")
@click.option("--template", "-t", default=None, hidden=True, help="Deprecated alias for --skill. Use --skill instead.")
@click.option("--base-url", default=None, help="Override Anthropic API base URL (also reads ANTHROPIC_BASE_URL env var).")
@click.option("--gateway", "-g", default="terminal", show_default=True,
              help="Question gateway(s), comma-separated: terminal, web, tui")
@click.option("--web-host", default="127.0.0.1", show_default=True,
              help="Host for web gateway (only used with --gateway web).")
@click.option("--web-port", default=7777, show_default=True, type=int,
              help="Port for web gateway (only used with --gateway web).")
@click.option("--open", "open_browser", is_flag=True, default=False,
              help="Auto-open browser when using --gateway web.")
def run(task: str, workspace: str, model: str, skill: str, template: str | None, base_url: str | None,
        gateway: str, web_host: str, web_port: int,
        open_browser: bool) -> None:
    """Run an agent on TASK."""
    _ensure_db()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        Console(stderr=True).print("[red]Error:[/red] ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    # Deprecated --template alias: warn and forward to --skill.
    if template is not None:
        console.print(
            f"[yellow]Warning:[/yellow] --template is deprecated; use --skill instead. "
            f"Forwarding {template!r} as the skill name."
        )
        skill = template

    asyncio.run(_run_task(task=task, workspace=Path(workspace), model=model, skill=skill,
                          base_url=base_url,
                          gateway=gateway, web_host=web_host, web_port=web_port,
                          open_browser=open_browser))


def _build_gateway(
    gateway_spec: str,
    broker: "QuestionBroker",
    task_id: str,
    web_host: str,
    web_port: int,
    open_browser: bool,
    console: "Console",
) -> "BaseGateway":
    """Parse comma-separated gateway spec and build gateway instance(s)."""
    from beidou.gateways.composite import CompositeGateway
    from beidou.gateways.terminal import TerminalGateway

    names = [n.strip().lower() for n in gateway_spec.split(",") if n.strip()]
    gateways = []
    for name in names:
        if name == "terminal":
            gateways.append(TerminalGateway(console=console))
        elif name == "web":
            from beidou.gateways.web import WebGateway
            gateways.append(WebGateway(
                broker=broker, task_id=task_id,
                host=web_host, port=web_port,
                auto_open=open_browser,
            ))
        elif name == "tui":
            from beidou.gateways.tui import TUIGateway
            gateways.append(TUIGateway(task_id=task_id))
        else:
            console.print(f"[yellow]Warning:[/yellow] unknown gateway '{name}', skipping.")

    if not gateways:
        return TerminalGateway(console=console)
    if len(gateways) == 1:
        return gateways[0]
    return CompositeGateway(gateways)


class _GatewayAdapter:
    """Bridges :class:`~beidou.gateways.base.BaseGateway` to the orchestrator's
    ``gateway_ask_user(caller_id, question, context) -> str`` and
    ``gateway_ask_user_structured(caller_id, questions, context) -> dict``
    contracts.

    ``ask()`` — legacy string-only path; used by ``sdk_agent.py``'s
    ``AskUserQuestion`` hook (and ``gateway_ask_user``).  Builds its own
    ``Question`` directly so that it can return a bare string.

    ``ask_structured()`` — unified path for ``gateway_ask_user_structured``;
    delegates to ``QuestionBroker.ask()`` so both the MCP ``ask_user``
    primitive and the orchestrator-internal watchdog/review escalations land at
    the same ``broker._pending`` entry, emit ``question_asked`` / ``question_answered``
    events, and persist in the DB.
    """

    def __init__(
        self,
        gateway: "BaseGateway",
        broker: "QuestionBroker",
        emitter: Any,
        task_id: str,
    ) -> None:
        self._gw = gateway
        self._broker = broker
        self._emitter = emitter
        self._task_id = task_id

    async def ask(self, caller_id: str, question: str, context: str | None) -> str:
        """Legacy string-only path.  Builds a Question without going through
        broker.ask() so it can return a plain string to sdk_agent.py."""
        import asyncio as _asyncio
        from beidou.inbox import Question, _new_qid

        loop = _asyncio.get_running_loop()
        q = Question(
            qid=_new_qid(),
            asker_agent_id=caller_id,
            current_holder_agent_id=None,
            chain=[caller_id, "USER"],
            questions=[{"question": question, "header": "", "multiSelect": False, "options": []}],
            context_hint=context,
            state="at_user",
            future=loop.create_future(),
        )
        self._broker._pending[q.qid] = q
        # Surface the question to the human via the registered gateway.
        await self._gw.surface_question(q, self._broker)
        try:
            result = await q.future
            # result is {"answers": [...], "answer_text": "..."} from resolve_answer
            if isinstance(result, dict):
                return result.get("answer_text", "")
            # Fallback for legacy future.set_result(str) paths still in the wild.
            return str(result)
        finally:
            self._broker._pending.pop(q.qid, None)

    async def ask_structured(
        self, caller_id: str, questions: list[dict], context: str | None
    ) -> dict:
        """Unified structured path.  Delegates to ``QuestionBroker.ask()`` so
        all asks go through the same pending-question object, DB write, and
        event sequence as MCP ``ask_user`` calls.

        Returns ``{"answers": [...], "answer_text": "..."}`` from the broker.
        """

        class _BrokerCtx:
            """Minimal duck-typed context satisfying ``QuestionBroker.ask``."""

            def __init__(
                self,
                agent_id: str,
                emitter: Any,
                task_id: str,
            ) -> None:
                self.agent_id = agent_id
                # parent=None means the question goes straight to the user
                # (no leader-chain escalation), matching current semantics of
                # gateway_ask_user / watchdog escalations.
                self.parent = None
                self._kv: dict[str, Any] = {
                    "emitter": emitter,
                    "task_id": task_id,
                }

            def get(self, k: str, default: Any = None) -> Any:
                return self._kv.get(k, default)

        ctx = _BrokerCtx(
            agent_id=caller_id,
            emitter=self._emitter,
            task_id=self._task_id,
        )
        return await self._broker.ask(ctx, questions, context_hint=context)


async def _run_task(
    task: str,
    workspace: Path,
    model: str,
    skill: str,
    base_url: str | None = None,
    gateway: str = "terminal",
    web_host: str = "127.0.0.1",
    web_port: int = 7777,
    open_browser: bool = False,
) -> None:
    import sys as _sys
    from beidou.db import complete_task, upsert_task
    from beidou.events import EventEmitter
    from beidou.inbox import QuestionBroker
    from beidou.orchestrator import Orchestrator

    task_id = f"tsk_{uuid.uuid4().hex[:8]}"
    emitter = EventEmitter(task_id)

    # Skill root: the beidou/skills/ directory shipped with the package.
    # Mirrors _bundled_skills_dir() in beidou/skills/loader.py (PyInstaller-aware).
    if getattr(_sys, "frozen", False):
        skill_root = Path(_sys._MEIPASS) / "beidou" / "skills"  # type: ignore[attr-defined]
    else:
        skill_root = Path(__file__).parent / "skills"

    # Gateway + broker setup.
    broker = QuestionBroker()
    gw = _build_gateway(gateway, broker, task_id, web_host, web_port, open_browser, console)
    broker.set_gateway(gw)
    gateway_adapter = _GatewayAdapter(gw, broker, emitter=emitter, task_id=task_id)

    orch = Orchestrator(
        task_id=task_id,
        emitter=emitter,
        skill_root=skill_root,
        gateway=gateway_adapter,
        default_model=model,
        project_workspace=workspace,
    )

    # Record task start.  agent_id is a placeholder here because the
    # orchestrator has not yet assigned the root agent id.  task_completed
    # and agent_started/agent_completed events emitted by the orchestrator
    # use the real agent id (orch._root_id), so they will correlate correctly
    # in the events table.
    upsert_task(task_id=task_id, description=task, model=model, skill=skill, started_at=time.time())
    await emitter.emit("task_started", agent_id="", model=model, skill=skill, task=task)

    console.rule(f"[bold cyan]Beidou[/bold cyan] task [yellow]{task_id}[/yellow]")
    console.print(f"[dim]model:[/dim] {model}  [dim]skill:[/dim] {skill}")
    console.print(f"[bold]Task:[/bold] {task}\n")

    # Inject the orchestrator into any WebGateway instances so the /send
    # endpoint has access to inbox_put.  Do this before gw.start() so the
    # orch reference is available when create_app() is called inside start().
    _has_web = "web" in gateway.lower()
    if _has_web:
        from beidou.gateways.web import WebGateway as _WebGateway
        from beidou.gateways.composite import CompositeGateway as _CompositeGateway
        _candidates = gw._gateways if isinstance(gw, _CompositeGateway) else [gw]
        for _g in _candidates:
            if isinstance(_g, _WebGateway):
                _g.orch = orch
    await gw.start()
    try:
        result = await orch.run_root(root_skill=skill, root_task=task, model=model)
        # Use the real root agent id for the task_completed event so it correlates
        # with agent_started / agent_completed events in the observability log.
        root_id = orch._root_id or ""
        complete_task(task_id=task_id, ended_at=time.time(), status="completed")
        await emitter.emit("task_completed", agent_id=root_id, status="completed")
        console.rule("[green]Done[/green]")
        console.print(result.final_text)
        console.print(f"\n[dim]task_id: {task_id} — run `beidou stats {task_id}` for details[/dim]")
        if _has_web:
            console.print("[dim]Web UI staying up — Ctrl+C to exit.[/dim]")
            await asyncio.get_running_loop().create_future()  # blocks until Ctrl+C
    except (KeyboardInterrupt, asyncio.CancelledError):
        await orch.shutdown()
    except Exception as exc:
        root_id = orch._root_id or ""
        complete_task(task_id=task_id, ended_at=time.time(), status="failed")
        await emitter.emit("task_completed", agent_id=root_id, status="failed", error=str(exc))
        Console(stderr=True).print(f"[red]Failed:[/red] {exc}")
        raise
    finally:
        await gw.stop()


# ------------------------------------------------------------------ #
# status                                                               #
# ------------------------------------------------------------------ #

@main.command()
@click.argument("task_id", required=False)
def status(task_id: str | None) -> None:
    """List tasks or show detail for TASK_ID."""
    _ensure_db()

    from beidou.db import get_task, get_tasks

    if task_id:
        task = get_task(task_id)
        if not task:
            console.print(f"[red]Not found:[/red] {task_id}")
            return
        _print_task_detail(task)
    else:
        tasks = get_tasks(20)
        if not tasks:
            console.print("[dim]No tasks yet. Run `beidou run` to start.[/dim]")
            return
        table = Table(title="Recent Tasks", show_header=True)
        table.add_column("task_id", style="yellow")
        table.add_column("model")
        table.add_column("skill")
        table.add_column("status")
        table.add_column("cost_usd", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("description")
        for t in tasks:
            status_style = "green" if t["status"] == "completed" else ("red" if t["status"] == "failed" else "cyan")
            table.add_row(
                t["task_id"][:16],
                (t["model"] or "?").replace("claude-", ""),
                t.get("skill") or t.get("template") or "?",
                f"[{status_style}]{t['status']}[/{status_style}]",
                f"${t['total_cost_usd']:.4f}" if t["total_cost_usd"] else "$0.0000",
                str(t["total_tokens"] or 0),
                (t["description"] or "")[:40],
            )
        console.print(table)


def _print_task_detail(task: dict) -> None:
    from beidou.db import get_agents, get_teams

    console.rule(f"Task [yellow]{task['task_id']}[/yellow]")
    console.print(f"[bold]Description:[/bold] {task['description']}")
    console.print(f"[bold]Model:[/bold] {task['model']}  [bold]Skill:[/bold] {task.get('skill') or task.get('template') or '?'}")
    console.print(f"[bold]Status:[/bold] {task['status']}")
    console.print(f"[bold]Cost:[/bold] ${task['total_cost_usd']:.4f}  [bold]Tokens:[/bold] {task['total_tokens']}")

    teams = get_teams(task["task_id"])
    if teams:
        console.print(f"\n[bold]Teams ({len(teams)}):[/bold]")
        for tm in teams:
            console.print(f"  [cyan]{tm['team_id']}[/cyan] — {tm['name']}")

    agents = get_agents(task["task_id"])
    if agents:
        console.print(f"\n[bold]Agents ({len(agents)}):[/bold]")
        for ag in agents:
            console.print(f"  [yellow]{ag['agent_id']}[/yellow] [{ag['role']}] calls={ag['tool_calls']} cost=${ag['cost_usd']:.4f}")


# ------------------------------------------------------------------ #
# teams                                                                #
# ------------------------------------------------------------------ #

@main.command()
@click.argument("task_id")
def teams(task_id: str) -> None:
    """Show team hierarchy for TASK_ID."""
    _ensure_db()

    from beidou.db import get_agents, get_teams

    teams_list = get_teams(task_id)
    if not teams_list:
        console.print("[dim]No teams created for this task.[/dim]")
        return

    # Build parent → children map
    children: dict[str | None, list[dict]] = {}
    for tm in teams_list:
        pid = tm["parent_team_id"]
        children.setdefault(pid, []).append(tm)

    tree = Tree(f"[bold]Task[/bold] [yellow]{task_id}[/yellow]")

    def _add_children(node, parent_id: str | None) -> None:
        for tm in children.get(parent_id, []):
            agents = get_agents(team_id=tm["team_id"])
            label = f"[cyan]{tm['team_id']}[/cyan] — [bold]{tm['name']}[/bold]  ({len(agents)} agents)"
            child_node = node.add(label)
            for ag in agents:
                child_node.add(f"[yellow]{ag['agent_id']}[/yellow] [{ag['role']}]")
            _add_children(child_node, tm["team_id"])

    _add_children(tree, None)
    console.print(tree)


# ------------------------------------------------------------------ #
# events                                                               #
# ------------------------------------------------------------------ #

@main.command()
@click.option("--task", "task_id", default=None, help="Filter by task ID.")
@click.option("--team", "team_id", default=None, help="Filter by team ID.")
@click.option("--agent", "agent_id", default=None, help="Filter by agent ID.")
@click.option("--follow", "-f", is_flag=True, help="Tail the JSONL file in real-time.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON lines.")
@click.option("--limit", default=50, show_default=True, help="Max events to show.")
def events(
    task_id: str | None,
    team_id: str | None,
    agent_id: str | None,
    follow: bool,
    as_json: bool,
    limit: int,
) -> None:
    """Stream or query events."""

    if follow and task_id:
        _tail_events(task_id, as_json)
        return

    # Read events from JSONL files
    events_dir = Path.home() / ".beidou" / "events"
    rows: list[dict] = []
    if task_id:
        event_files = [events_dir / f"{task_id}.jsonl"]
    else:
        event_files = sorted(events_dir.glob("*.jsonl"))

    for fp in event_files:
        if not fp.exists():
            continue
        with fp.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if team_id and ev.get("team_id") != team_id:
                    continue
                if agent_id and ev.get("agent_id") != agent_id:
                    continue
                rows.append(ev)

    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    rows = rows[:limit]
    rows.reverse()  # chronological

    if as_json:
        for r in rows:
            click.echo(json.dumps(r))
        return

    if not rows:
        if task_id:
            fp = events_dir / f"{task_id}.jsonl"
            if fp.exists():
                console.print("[dim]No matching events found.[/dim]")
            else:
                console.print(f"[red]No event log for task:[/red] {task_id}")
        else:
            console.print("[dim]No events found.[/dim]")
        return

    table = Table(show_header=True, show_lines=False)
    table.add_column("ts", style="dim", width=10)
    table.add_column("event", width=18)
    table.add_column("agent_id", style="yellow", width=12)
    table.add_column("team_id", style="cyan", width=12)
    table.add_column("tool")
    table.add_column("dur_ms", justify="right")
    table.add_column("cost_usd", justify="right")

    for r in rows:
        ts_str = time.strftime("%H:%M:%S", time.localtime(r["ts"])) if r.get("ts") else "?"
        dur = f"{r['duration_ms']:.0f}" if r.get("duration_ms") else "—"
        cost = f"${r['cost_usd']:.4f}" if r.get("cost_usd") else "—"
        table.add_row(
            ts_str,
            r.get("event") or "?",
            (r.get("agent_id") or "")[-10:],
            (r.get("team_id") or "")[-10:] if r.get("team_id") else "—",
            r.get("tool") or "—",
            dur,
            cost,
        )
    console.print(table)


def _tail_events(task_id: str, as_json: bool) -> None:
    import time

    events_path = Path.home() / ".beidou" / "events" / f"{task_id}.jsonl"
    console.print(f"[dim]Tailing {events_path} (Ctrl-C to stop)[/dim]")

    try:
        with events_path.open() as f:
            f.seek(0, 2)  # seek to end
            while True:
                line = f.readline()
                if line:
                    if as_json:
                        click.echo(line.rstrip())
                    else:
                        try:
                            ev = json.loads(line)
                            ts = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
                            click.echo(
                                f"{ts}  {ev.get('event','?'):<20} {ev.get('agent_id','')[-10:]:<10} "
                                f"{ev.get('tool','')}"
                            )
                        except json.JSONDecodeError:
                            click.echo(line.rstrip())
                else:
                    time.sleep(0.1)
    except FileNotFoundError:
        console.print(f"[red]No event log for task:[/red] {task_id}")
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ #
# serve                                                                #
# ------------------------------------------------------------------ #

@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind (use 0.0.0.0 to expose on LAN).")
@click.option("--port", default=7777, show_default=True, help="Port to bind.")
def serve(host: str, port: int) -> None:
    """Run the Beidou web UI (dashboard + live agent view)."""
    _ensure_db()
    import uvicorn
    from beidou.web.app import create_app

    if host not in ("127.0.0.1", "localhost"):
        console.print(f"[yellow]Warning:[/yellow] binding {host}:{port} — web UI has no auth; do not expose to untrusted networks.")

    console.print(f"[bold cyan]Beidou web UI[/bold cyan] → http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


# ------------------------------------------------------------------ #
# stats                                                                #
# ------------------------------------------------------------------ #

@main.command()
@click.argument("task_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output JSON.")
def stats(task_id: str | None, as_json: bool) -> None:
    """Show aggregated stats for TASK_ID (or latest task)."""
    _ensure_db()

    from beidou.db import get_stats, get_tasks

    if not task_id:
        recent = get_tasks(1)
        if not recent:
            console.print("[dim]No tasks yet.[/dim]")
            return
        task_id = recent[0]["task_id"]

    data = get_stats(task_id)
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    task = data["task"]
    console.rule(f"Stats for [yellow]{task_id}[/yellow]")
    console.print(f"[bold]Description:[/bold] {task.get('description','?')}")
    console.print(f"[bold]Status:[/bold]      {task.get('status','?')}")
    console.print(f"[bold]Teams:[/bold]       {data['team_count']}")
    console.print(f"[bold]Agents:[/bold]      {data['agent_count']}")
    console.print(f"[bold]LLM calls:[/bold]   {data['total_llm_calls']}")
    console.print(f"[bold]Tool calls:[/bold]  {data['total_tool_calls']}")
    console.print(f"[bold]Total cost:[/bold]  ${task.get('total_cost_usd', 0):.4f}")
    console.print(f"[bold]Tokens:[/bold]      {task.get('total_tokens', 0)}")

    if data["top_tools"]:
        table = Table(title="Top Tools", show_header=True)
        table.add_column("tool")
        table.add_column("calls", justify="right")
        table.add_column("total_ms", justify="right")
        for r in data["top_tools"]:
            table.add_row(r["tool_name"], str(r["cnt"]), f"{r['total_ms']:.0f}" if r["total_ms"] else "0")
        console.print(table)


if __name__ == "__main__":
    main()
