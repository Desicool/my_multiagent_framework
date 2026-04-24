"""Beidou CLI entry point."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

console = Console()


def _ensure_db() -> None:
    from beidou.db import DB_PATH, init_db

    if not DB_PATH.exists():
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
@click.option("--model", "-m", default="claude-sonnet-4-6", show_default=True, help="Anthropic model ID.")
@click.option("--template", "-t", default="default", show_default=True, help="Agent template (default/coder/researcher).")
@click.option("--base-url", default=None, help="Override Anthropic API base URL (also reads ANTHROPIC_BASE_URL env var).")
def run(task: str, model: str, template: str, base_url: str | None) -> None:
    """Run an agent on TASK."""
    _ensure_db()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]Error:[/red] ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(_run_task(task=task, model=model, template=template, base_url=base_url))


async def _run_task(task: str, model: str, template: str, base_url: str | None = None) -> None:
    from beidou.agent import Agent
    from beidou.context import AgentContext
    from beidou.db import complete_task, upsert_task
    from beidou.events import EventEmitter
    from beidou.layers.observability_layer import ObservabilityLayer
    from beidou.layers.tools_layer import ToolsLayer
    from beidou.layers.workspace_layer import WorkspaceLayer
    from beidou.team import _build_tools, _load_template

    task_id = f"tsk_{uuid.uuid4().hex[:8]}"
    emitter = EventEmitter(task_id)

    # Root agent workspace is wherever beidou was invoked
    workspace_path = Path.cwd()

    # Load template (may include pre-built SkillTool instances)
    tmpl = _load_template(template)
    tools = _build_tools(tmpl.get("tools", [])) + list(tmpl.get("_skill_tools") or [])

    system_prompt = tmpl.get("system_prompt", "").format(
        role="leader",
        role_description="Initial agent for this task",
        team_name="root",
        workspace_path=str(workspace_path),
    )

    obs_layer = ObservabilityLayer(
        emitter,
        model=model,
        role="leader",
        template=template,
        tools=tmpl.get("tools", []),
        skills=tmpl.get("_skill_names") or [],
        system_prompt=system_prompt,
    )
    tools_layer = ToolsLayer(tools)
    workspace_layer = WorkspaceLayer(workspace_path)

    ctx = AgentContext.root(task_id=task_id, layers=[tools_layer, workspace_layer, obs_layer])
    ctx._kv["model"] = model
    ctx._kv["emitter"] = emitter
    ctx._kv["cwd"] = workspace_path
    if base_url:
        ctx._kv["base_url"] = base_url

    # Record task start
    upsert_task(task_id=task_id, description=task, model=model, template=template, started_at=time.time())
    await emitter.emit("task_started", agent_id=ctx.agent_id, model=model, template=template, task=task)

    console.rule(f"[bold cyan]Beidou[/bold cyan] task [yellow]{task_id}[/yellow]")
    console.print(f"[dim]model:[/dim] {model}  [dim]template:[/dim] {template}")
    console.print(f"[bold]Task:[/bold] {task}\n")

    agent = Agent(model=model, role="leader", ctx=ctx, system_prompt=system_prompt)

    try:
        result = await agent.run(task)
        complete_task(task_id=task_id, ended_at=time.time(), status="completed")
        await emitter.emit("task_completed", agent_id=ctx.agent_id, status="completed")
        console.rule("[green]Done[/green]")
        console.print(result)
    except Exception as exc:
        complete_task(task_id=task_id, ended_at=time.time(), status="failed")
        await emitter.emit("task_completed", agent_id=ctx.agent_id, status="failed", error=str(exc))
        Console(stderr=True).print(f"[red]Failed:[/red] {exc}")
        raise

    console.print(f"\n[dim]task_id: {task_id} — run `beidou stats {task_id}` for details[/dim]")


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
        table.add_column("template")
        table.add_column("status")
        table.add_column("cost_usd", justify="right")
        table.add_column("tokens", justify="right")
        table.add_column("description")
        for t in tasks:
            status_style = "green" if t["status"] == "completed" else ("red" if t["status"] == "failed" else "cyan")
            table.add_row(
                t["task_id"][:16],
                (t["model"] or "?").replace("claude-", ""),
                t["template"] or "?",
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
    console.print(f"[bold]Model:[/bold] {task['model']}  [bold]Template:[/bold] {task['template']}")
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
    _ensure_db()

    if follow and task_id:
        _tail_events(task_id, as_json)
        return

    from beidou.db import get_events

    rows = get_events(task_id=task_id, team_id=team_id, agent_id=agent_id, limit=limit)
    rows.reverse()  # chronological

    if as_json:
        for r in rows:
            click.echo(json.dumps(r))
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
        ts_str = time.strftime("%H:%M:%S", time.localtime(r["ts"])) if r["ts"] else "?"
        dur = f"{r['duration_ms']:.0f}" if r["duration_ms"] else "—"
        cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] else "—"
        table.add_row(
            ts_str,
            r["event_type"] or "?",
            (r["agent_id"] or "")[-10:],
            (r["team_id"] or "")[-10:] if r["team_id"] else "—",
            r["tool_name"] or "—",
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
