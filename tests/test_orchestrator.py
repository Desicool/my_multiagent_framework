"""Tests for ``beidou/orchestrator.py``.

These exercise the orchestrator LOGIC directly; ``beidou.sdk_agent.run_agent``
is monkeypatched with a fake so no real SDK subprocess is spawned. Tests
either construct an ``Orchestrator`` and seed its registry manually, or drive
it through ``run_root`` / ``spawn_team`` (which both launch asyncio tasks
that invoke the fake).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from beidou import orchestrator as orch_module
from beidou.orchestrator import (
    ROOT_TEAM_ID,
    TOKEN_CEILING,
    USER_SENTINEL,
    AgentRecord,
    Orchestrator,
    TeamRecord,
)
from beidou.primitives.core import (
    CONTRACT_STRIKES,
    INBOX_CAP,
    Message,
    PrimitiveError,
)
from beidou.sdk_agent import RunResult, SpawnSpec


# ---------------------------------------------------------------------------
# Fakes / helpers.
# ---------------------------------------------------------------------------


class _FakeEmitter:
    """Replaces EventEmitter so tests don't touch ~/.beidou or SQLite."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Optional[str], dict]] = []

    async def emit(self, event: str, agent_id: str, team_id=None, **kwargs) -> None:
        self.calls.append((event, agent_id, team_id, kwargs))


def _make_result(*, terminated: bool, stop_reason: str = "end_turn") -> RunResult:
    return RunResult(
        final_text="",
        total_cost_usd=0.0,
        total_usage={},
        num_turns=1,
        duration_ms=0,
        stop_reason=stop_reason,
        session_id=None,
        terminated=terminated,
        contract_violation=not terminated,
    )


def _make_orchestrator(tmp_path: Path) -> tuple[Orchestrator, _FakeEmitter]:
    emitter = _FakeEmitter()
    skill_root = tmp_path / "skills"
    skill_root.mkdir(exist_ok=True)
    o = Orchestrator(
        task_id="tsk_test",
        emitter=emitter,  # type: ignore[arg-type]
        skill_root=skill_root,
    )
    return o, emitter


def _seed_agent(
    o: Orchestrator,
    agent_id: str,
    team_id: str,
    *,
    role: str = "member",
    skill: str = "fake_skill",
) -> AgentRecord:
    rec = AgentRecord(
        agent_id=agent_id,
        task_id=o.task_id,
        team_id=team_id,
        role=role,
        skill_name=skill,
        model=None,
        inbox=asyncio.Queue(),
        create_team_lock=asyncio.Lock(),
    )
    o._agents[agent_id] = rec
    if team_id in o._teams and agent_id not in o._teams[team_id].member_ids:
        o._teams[team_id].member_ids.append(agent_id)
    return rec


def _seed_team(
    o: Orchestrator,
    team_id: str,
    leader_id: str,
    *,
    depth: int = 0,
    parent: Optional[str] = None,
) -> TeamRecord:
    t = TeamRecord(
        team_id=team_id,
        name=team_id,
        task="",
        leader_id=leader_id,
        depth=depth,
        member_ids=[],
        rules=[],
        parent_team_id=parent,
    )
    o._teams[team_id] = t
    return t


def run(coro) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# spawn_team
# ---------------------------------------------------------------------------


def test_spawn_team_sets_leader_depth_and_members(tmp_path, monkeypatch):
    o, _ = _make_orchestrator(tmp_path)
    # Seed a caller on a depth-0 team.
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "R", ROOT_TEAM_ID, role="root")

    # Skip the skill-loader check — no real SKILL.md on disk.
    monkeypatch.setattr(
        orch_module, "__name__", orch_module.__name__
    )  # no-op, keeps monkeypatch import live

    # Patch load_skill inside spawn_team to succeed.
    import beidou.skills.loader as loader_mod

    class _FakeSkill:
        name = "fake"

    def _fake_load_skill(root, name):
        return _FakeSkill()

    monkeypatch.setattr(loader_mod, "load_skill", _fake_load_skill)

    # Fake run_agent so launched tasks return immediately (terminated).
    fake_calls: list[str] = []

    async def fake_run(orch, spec):
        fake_calls.append(spec.caller_id)
        # Mark terminated so the policy loop returns cleanly.
        orch._agents[spec.caller_id].terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module.sdk_agent, "run_agent", fake_run)

    async def body():
        out = await o.spawn_team(
            leader_id="R",
            name="impl",
            task="build",
            roles=[
                {"role": "coder", "template": "fake", "description": "code"},
                {"role": "tester", "template": "fake", "description": "test"},
            ],
            rules=["rule1"],
        )
        team_id = out["team_id"]
        assert team_id.startswith("tm_")
        assert len(out["members"]) == 2
        # Self-lead invariant.
        assert o.leader_of(team_id) == "R"
        # Depth = parent.depth + 1.
        assert o.team_depth(team_id) == 1
        # Members are registered and belong to the new team.
        for m in out["members"]:
            assert o.agent_exists(m["agent_id"])
            assert o.agent_team(m["agent_id"]) == team_id
        # Each launched task ran the fake run_agent.
        await asyncio.gather(
            *[o._agents[m["agent_id"]].run_task for m in out["members"]]
        )
        assert set(fake_calls) == {m["agent_id"] for m in out["members"]}

    run(body())


def test_spawn_team_unknown_template_raises(tmp_path, monkeypatch):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "R", ROOT_TEAM_ID)

    import beidou.skills.loader as loader_mod

    def _boom(root, name):
        raise loader_mod.SkillNotFound(name, root)

    monkeypatch.setattr(loader_mod, "load_skill", _boom)

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await o.spawn_team(
                leader_id="R",
                name="impl",
                task="x",
                roles=[{"role": "r", "template": "nope"}],
                rules=[],
            )
        assert ei.value.code == "unknown_template"

    run(body())


# ---------------------------------------------------------------------------
# inbox_put / terminate sentinel bypass
# ---------------------------------------------------------------------------


def test_inbox_put_enforces_cap_for_non_beidou_senders(tmp_path):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id="R", depth=0)
    _seed_agent(o, "R", ROOT_TEAM_ID)
    rec = o._agents["R"]

    # Fill the inbox up to the cap.
    for _ in range(INBOX_CAP):
        rec.inbox.put_nowait(
            Message(from_id="peer", content="x", ts=0.0, message_id="m")
        )

    async def body():
        with pytest.raises(PrimitiveError) as ei:
            await o.inbox_put(
                "R",
                Message(from_id="peer", content="overflow", ts=0.0, message_id="mm"),
            )
        assert ei.value.code == "inbox_full"

    run(body())


def test_inbox_put_bypasses_cap_for_beidou_senders(tmp_path):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id="R", depth=0)
    _seed_agent(o, "R", ROOT_TEAM_ID)
    rec = o._agents["R"]

    for _ in range(INBOX_CAP):
        rec.inbox.put_nowait(
            Message(from_id="peer", content="x", ts=0.0, message_id="m")
        )

    async def body():
        # Should NOT raise despite being over the cap.
        await o.inbox_put(
            "R",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=0.0,
                message_id="t",
                kind="terminate",
            ),
        )
        assert o.inbox_size("R") == INBOX_CAP + 1

    run(body())


def test_inbox_get_one_flips_terminate_consumed(tmp_path):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id="R", depth=0)
    _seed_agent(o, "R", ROOT_TEAM_ID)

    async def body():
        assert o.was_terminated("R") is False
        await o.inbox_put(
            "R",
            Message(
                from_id="beidou",
                content="__terminate__",
                ts=0.0,
                message_id="t",
                kind="terminate",
            ),
        )
        msg = await o.inbox_get_one("R", None, timeout=1.0)
        assert msg is not None and msg.kind == "terminate"
        assert o.was_terminated("R") is True

    run(body())


# ---------------------------------------------------------------------------
# run_root + terminate_root
# ---------------------------------------------------------------------------


def test_terminate_root_posts_sentinel_and_fake_agent_exits(tmp_path, monkeypatch):
    o, _ = _make_orchestrator(tmp_path)

    # Patch skill loader.
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    async def fake_run(orch, spec):
        # Behave like a good persistent agent: block on inbox until a
        # terminate sentinel shows up.
        while True:
            msg = await orch.inbox_get_one(spec.caller_id, None, timeout=5.0)
            if msg is None:
                continue
            if msg.kind == "terminate":
                # was_terminated() is already True because inbox_get_one flipped it.
                return _make_result(terminated=True)

    monkeypatch.setattr(orch_module.sdk_agent, "run_agent", fake_run)

    async def body():
        # Kick off root in the background, then terminate it.
        root_task = asyncio.create_task(o.run_root("fake", "do work"))
        # Let the root task start running.
        await asyncio.sleep(0.05)
        assert o._root_id is not None
        await o.terminate_root()
        res = await asyncio.wait_for(root_task, timeout=2.0)
        assert res.terminated is True

    run(body())


# ---------------------------------------------------------------------------
# Cascade: leader receives terminate, terminates each member, exits last.
# ---------------------------------------------------------------------------


def test_cascade_leader_terminates_members_then_exits(tmp_path, monkeypatch):
    """Verify leaf-first / leader-last cascade ordering.

    The root fake-agent, on receiving a terminate sentinel, calls
    ``terminate_child`` on each member (via the orchestrator's primitive
    surface) then returns. Member fake-agents simply wait for terminate.
    """
    o, _ = _make_orchestrator(tmp_path)

    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    # Ordering trace: agent ids as they hit ``return``.
    exit_order: list[str] = []
    leaf_terminated = asyncio.Event()
    members_created_event = asyncio.Event()

    async def leader_behaviour(orch, spec):
        # Once: create a sub-team of two leaves.
        team_out = await orch.spawn_team(
            leader_id=spec.caller_id,
            name="impl",
            task="do",
            roles=[
                {"role": "m1", "template": "fake"},
                {"role": "m2", "template": "fake"},
            ],
            rules=[],
        )
        member_ids = [m["agent_id"] for m in team_out["members"]]
        members_created_event.set()

        # Block on inbox until terminate.
        while True:
            msg = await orch.inbox_get_one(spec.caller_id, None, timeout=5.0)
            if msg is None:
                continue
            if msg.kind == "terminate":
                # Cascade: terminate each member, await ack via sentinel receipt.
                from beidou.primitives.core import terminate_child
                for mid in member_ids:
                    await terminate_child(orch, caller_id=spec.caller_id, agent_id=mid)
                # Wait for leaves to exit.
                while any(
                    orch._agents[mid].run_task is not None
                    and not orch._agents[mid].run_task.done()
                    for mid in member_ids
                ):
                    await asyncio.sleep(0.01)
                exit_order.append(spec.caller_id)
                return _make_result(terminated=True)

    async def leaf_behaviour(orch, spec):
        while True:
            msg = await orch.inbox_get_one(spec.caller_id, None, timeout=5.0)
            if msg is None:
                continue
            if msg.kind == "terminate":
                exit_order.append(spec.caller_id)
                leaf_terminated.set()
                return _make_result(terminated=True)

    async def fake_run(orch, spec):
        # Distinguish root from leaf by the role in template_vars (leader is
        # the one we run_root'd with role="root").
        if (spec.template_vars or {}).get("role") == "root":
            return await leader_behaviour(orch, spec)
        return await leaf_behaviour(orch, spec)

    monkeypatch.setattr(orch_module.sdk_agent, "run_agent", fake_run)

    async def body():
        root_task = asyncio.create_task(o.run_root("fake", "lead"))
        # Wait until the sub-team has been spawned.
        await asyncio.wait_for(members_created_event.wait(), timeout=2.0)
        # Now fire the root terminate signal.
        await o.terminate_root()
        res = await asyncio.wait_for(root_task, timeout=3.0)
        assert res.terminated is True
        # Leaves appear before the leader in exit_order.
        assert exit_order[-1] == o._root_id
        assert len(exit_order) == 3  # 2 leaves + root

    run(body())


# ---------------------------------------------------------------------------
# Contract-violation recovery: transient.
# ---------------------------------------------------------------------------


def test_contract_violation_single_strike_recovers(tmp_path, monkeypatch):
    o, _ = _make_orchestrator(tmp_path)

    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    call_count = {"n": 0}

    async def fake_run(orch, spec):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_result(terminated=False)
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module.sdk_agent, "run_agent", fake_run)

    async def body():
        res = await o.run_root("fake", "work")
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert res.terminated is True
        # Two attempts: one violating, one clean.
        assert call_count["n"] == 2
        # Strike counter reflects the single violation (brief §test: "== 1",
        # below escalation threshold).
        rec = o._agents[o._root_id]
        assert rec.contract_strikes == 1
        # Exactly one contract_violation event was emitted.
        emitter_obj = o.emitter
        cv = [c for c in emitter_obj.calls if c[0] == "contract_violation"]
        assert len(cv) == 1

    run(body())


def test_contract_violation_three_strikes_escalates_to_leader(tmp_path, monkeypatch):
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    # Pre-seed a leader team with a non-root member we can drive directly.
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "LEADER", ROOT_TEAM_ID, role="leader")
    _seed_team(o, "tm_child", leader_id="LEADER", depth=1, parent=ROOT_TEAM_ID)
    rec = _seed_agent(o, "VIOLATOR", "tm_child", role="coder")

    async def always_violate(orch, spec):
        return _make_result(terminated=False, stop_reason="end_turn")

    monkeypatch.setattr(orch_module.sdk_agent, "run_agent", always_violate)

    async def body():
        spec = SpawnSpec(
            caller_id="VIOLATOR",
            skill_name="fake",
            skill_root=o.skill_root,
            task="go",
            model=None,
        )
        result = await o._run_agent_with_policy(rec, spec)
        # Drain background emitter tasks so emitter.calls is fully populated.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert rec.contract_strikes == CONTRACT_STRIKES
        # Leader received a recommendation message.
        leader_inbox = o._agents["LEADER"].inbox
        assert leader_inbox.qsize() == 1
        msg = leader_inbox.get_nowait()
        assert msg.from_id == "beidou"
        assert "VIOLATOR" in msg.content
        # contract_violation emitted exactly CONTRACT_STRIKES times.
        cv = [c for c in emitter.calls if c[0] == "contract_violation"]
        assert len(cv) == CONTRACT_STRIKES

    run(body())


# ---------------------------------------------------------------------------
# Token ceiling
# ---------------------------------------------------------------------------


def test_token_ceiling_recommends_to_leader(tmp_path):
    o, _ = _make_orchestrator(tmp_path)
    # Leader at root.
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "LEADER", ROOT_TEAM_ID, role="leader")
    # Child team led by LEADER with one member.
    _seed_team(o, "tm_kid", leader_id="LEADER", depth=1, parent=ROOT_TEAM_ID)
    rec = _seed_agent(o, "CHILD", "tm_kid")

    async def body():
        # Half + half = at the ceiling.
        o.emit_event("turn.usage", {
            "caller_id": "CHILD",
            "message_id": "m1",
            "input_tokens": TOKEN_CEILING // 2,
            "output_tokens": TOKEN_CEILING // 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })
        # Let the scheduled inbox_put coroutine run.
        await asyncio.sleep(0.05)
        assert rec.total_tokens >= TOKEN_CEILING
        leader_inbox = o._agents["LEADER"].inbox
        assert leader_inbox.qsize() == 1
        msg = leader_inbox.get_nowait()
        assert msg.from_id == "beidou"
        assert "CHILD" in msg.content
        assert "token" in msg.content.lower()

    run(body())


def test_token_ceiling_root_emits_event(tmp_path, monkeypatch):
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "ROOT", ROOT_TEAM_ID, role="root")
    o._root_id = "ROOT"

    async def body():
        o.emit_event("turn.usage", {
            "caller_id": "ROOT",
            "input_tokens": TOKEN_CEILING,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })
        await asyncio.sleep(0.05)
        names = [c[0] for c in emitter.calls]
        assert "root_token_ceiling_reached" in names

    run(body())


# ---------------------------------------------------------------------------
# record_status / liveness
# ---------------------------------------------------------------------------


def test_record_status_updates_and_emits(tmp_path):
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "A", ROOT_TEAM_ID)

    async def body():
        o.record_status("A", "done", "shipped")
        assert rec.last_status == "done"
        assert rec.last_status_detail == "shipped"
        await asyncio.sleep(0.05)
        names = [c[0] for c in emitter.calls]
        assert "liveness_check" in names

    run(body())


# ---------------------------------------------------------------------------
# peer_snapshot
# ---------------------------------------------------------------------------


def test_peer_snapshot_scopes(tmp_path):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, ROOT_TEAM_ID, leader_id="R", depth=0)
    _seed_agent(o, "R", ROOT_TEAM_ID, role="leader")
    _seed_agent(o, "A", ROOT_TEAM_ID)
    _seed_agent(o, "B", ROOT_TEAM_ID)
    _seed_team(o, "tm_sub", leader_id="A", depth=1, parent=ROOT_TEAM_ID)
    _seed_agent(o, "C", "tm_sub")

    team = {p.agent_id for p in o.peer_snapshot("A", "team")}
    assert team == {"R", "B"}

    children = {p.agent_id for p in o.peer_snapshot("A", "children")}
    assert children == {"C"}

    allp = {p.agent_id for p in o.peer_snapshot("A", "all")}
    assert allp == {"R", "B", "C"}
