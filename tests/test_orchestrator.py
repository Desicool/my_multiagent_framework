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
    TERMINATE_GRACE_S,
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

# A generic top-level team id used in tests that need a named team but
# don't care about the root-agent teamless semantics.
_TM_TOP = "tm_top"


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
    team_id: str | None,
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
    if team_id is not None and team_id in o._teams and agent_id not in o._teams[team_id].member_ids:
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
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "R", _TM_TOP, role="root")

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

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

    async def body():
        out = await o.spawn_team(
            leader_id="R",
            name="impl",
            task="build",
            roles=[
                {"role": "coder", "skill": "fake", "description": "code"},
                {"role": "tester", "skill": "fake", "description": "test"},
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


def test_spawn_team_unknown_skill_raises(tmp_path, monkeypatch):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "R", _TM_TOP)

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
                roles=[{"role": "r", "skill": "nope"}],
                rules=[],
            )
        assert ei.value.code == "unknown_skill"

    run(body())


# ---------------------------------------------------------------------------
# inbox_put / terminate sentinel bypass
# ---------------------------------------------------------------------------


def test_inbox_put_enforces_cap_for_non_beidou_senders(tmp_path):
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id="R", depth=0)
    _seed_agent(o, "R", _TM_TOP)
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
    _seed_team(o, _TM_TOP, leader_id="R", depth=0)
    _seed_agent(o, "R", _TM_TOP)
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


def test_queue_for_returns_per_agent_inbox(tmp_path):
    """queue_for returns the same asyncio.Queue that inbox_put pushes onto."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id="R", depth=0)
    _seed_agent(o, "R", _TM_TOP)

    async def body():
        q = o.queue_for("R")
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
        assert q.qsize() == 1
        msg = q.get_nowait()
        assert msg.kind == "terminate"
        # Setting terminate_consumed on the record is now the outer loop's job;
        # verify the flag starts False and can be set directly.
        rec = o._agents["R"]
        assert rec.terminate_consumed is False
        rec.terminate_consumed = True
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
        # Behave like a good persistent agent: block on the per-agent queue
        # until a terminate sentinel shows up.
        q = orch.queue_for(spec.caller_id)
        while True:
            msg = await asyncio.wait_for(q.get(), timeout=5.0)
            if msg.kind == "terminate":
                # Mirror what input_stream does: set terminate_consumed on
                # the record so was_terminated() returns True.
                rec = orch._agents.get(spec.caller_id)
                if rec is not None:
                    rec.terminate_consumed = True
                return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

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
                {"role": "m1", "skill": "fake"},
                {"role": "m2", "skill": "fake"},
            ],
            rules=[],
        )
        member_ids = [m["agent_id"] for m in team_out["members"]]
        members_created_event.set()

        # Block on queue until terminate.
        q = orch.queue_for(spec.caller_id)
        while True:
            msg = await asyncio.wait_for(q.get(), timeout=5.0)
            if msg.kind == "terminate":
                # Cascade: terminate each member, await ack via sentinel receipt.
                # Use force=True because leaves haven't signalled completion
                # in this test — this models a user-signal-driven force-cascade, not the
                # standard approve path.
                from beidou.primitives.core import terminate_child
                for mid in member_ids:
                    await terminate_child(orch, caller_id=spec.caller_id, agent_id=mid, force=True)
                # Wait for leaves to exit.
                while any(
                    orch._agents[mid].run_task is not None
                    and not orch._agents[mid].run_task.done()
                    for mid in member_ids
                ):
                    await asyncio.sleep(0.01)
                rec = orch._agents.get(spec.caller_id)
                if rec is not None:
                    rec.terminate_consumed = True
                exit_order.append(spec.caller_id)
                return _make_result(terminated=True)

    async def leaf_behaviour(orch, spec):
        q = orch.queue_for(spec.caller_id)
        while True:
            msg = await asyncio.wait_for(q.get(), timeout=5.0)
            if msg.kind == "terminate":
                rec = orch._agents.get(spec.caller_id)
                if rec is not None:
                    rec.terminate_consumed = True
                exit_order.append(spec.caller_id)
                leaf_terminated.set()
                return _make_result(terminated=True)

    async def fake_run(orch, spec):
        # Distinguish root from leaf by the role in template_vars (leader is
        # the one we run_root'd with role="root").
        if (spec.template_vars or {}).get("role") == "root":
            return await leader_behaviour(orch, spec)
        return await leaf_behaviour(orch, spec)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

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

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", fake_run)

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
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "LEADER", _TM_TOP, role="leader")
    _seed_team(o, "tm_child", leader_id="LEADER", depth=1, parent=_TM_TOP)
    rec = _seed_agent(o, "VIOLATOR", "tm_child", role="coder")

    async def always_violate(orch, spec):
        return _make_result(terminated=False, stop_reason="end_turn")

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", always_violate)

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
# record_status / liveness
# ---------------------------------------------------------------------------


def test_record_status_updates_and_emits(tmp_path):
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "A", _TM_TOP)

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
    _seed_team(o, _TM_TOP, leader_id="R", depth=0)
    _seed_agent(o, "R", _TM_TOP, role="leader")
    _seed_agent(o, "A", _TM_TOP)
    _seed_agent(o, "B", _TM_TOP)
    _seed_team(o, "tm_sub", leader_id="A", depth=1, parent=_TM_TOP)
    _seed_agent(o, "C", "tm_sub")

    team_peers = o.peer_snapshot("A", "team")
    team = {p.agent_id for p in team_peers}
    assert team == {"R", "B"}
    # Leader R must appear with role='leader'
    r_peer = next(p for p in team_peers if p.agent_id == "R")
    assert r_peer.role == "leader"

    children = {p.agent_id for p in o.peer_snapshot("A", "children")}
    assert children == {"C"}

    allp = {p.agent_id for p in o.peer_snapshot("A", "all")}
    assert allp == {"R", "B", "C"}


def test_peer_snapshot_includes_leader(tmp_path):
    """Peer snapshot includes the team leader even when leader is not in member_ids."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id="L", depth=0)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    _seed_agent(o, "M1", _TM_TOP, role="coder")
    _seed_agent(o, "M2", _TM_TOP, role="coder")
    # _seed_agent pushes agents into member_ids; production spawn_team does
    # NOT push the leader into member_ids. Remove the leader to match.
    o._teams[_TM_TOP].member_ids.remove("L")

    peers = o.peer_snapshot("M1", "team")
    ids = {p.agent_id for p in peers}
    assert ids == {"L", "M2"}, f"Expected {{'L', 'M2'}}, got {ids}"

    # Leader peer must have role='leader' and correct team.
    leader_peer = next(p for p in peers if p.agent_id == "L")
    assert leader_peer.role == "leader"
    assert leader_peer.team_id == _TM_TOP


# ---------------------------------------------------------------------------
# deliver_message
# ---------------------------------------------------------------------------


def test_deliver_message_puts_to_inbox_and_emits_event(tmp_path):
    """deliver_message lands in recipient's inbox and emits a message event."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "sender", _TM_TOP)
    _seed_agent(o, "recipient", _TM_TOP)

    async def body():
        o.deliver_message(from_id="sender", to_id="recipient", body="hello", kind="completion_report")
        await asyncio.sleep(0.05)  # let the async delivery task run

        # Verify the message is in the inbox via queue_for.
        q = o.queue_for("recipient")
        msgs: list[Message] = []
        while not q.empty():
            msgs.append(q.get_nowait())
        assert len(msgs) == 1
        assert msgs[0].content == "hello"
        assert msgs[0].from_id == "sender"
        assert msgs[0].kind == "completion_report"

        # Verify a message event was emitted.
        names = [c[0] for c in emitter.calls]
        assert "message" in names

    run(body())


def test_deliver_message_unknown_recipient_is_silent(tmp_path):
    """deliver_message to an unknown or sentinel recipient silently no-ops."""
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "sender", _TM_TOP)

    # Should not raise; USER_SENTINEL is not a registered agent.
    o.deliver_message(from_id="sender", to_id=USER_SENTINEL, body="hi", kind="completion_report")
    # No events emitted for this no-op.


# ---------------------------------------------------------------------------
# assistant_text_for_turn
# ---------------------------------------------------------------------------


def test_assistant_text_for_turn_exact_binding(tmp_path):
    """assistant_text_for_turn returns text bound to the exact tool_use_id."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "ag1", _TM_TOP)

    # Populate via record_assistant_text (the public method sdk_agent uses).
    o.record_assistant_text("ag1", "I finished the task.", ["toolu_abc"])
    result = o.assistant_text_for_turn("ag1", "toolu_abc")
    assert result == "I finished the task."


def test_assistant_text_for_turn_fallback_to_most_recent(tmp_path):
    """Falls back to most recent when no exact tool_use_id match."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "ag2", _TM_TOP)

    # Record text for a prior turn with a different tool_use_id.
    o.record_assistant_text("ag2", "Prior turn summary.", ["toolu_prior"])
    # Now ask for a tool_use_id that was never bound.
    result = o.assistant_text_for_turn("ag2", "toolu_unknown")
    assert result == "Prior turn summary."  # fallback


def test_assistant_text_for_turn_returns_none_when_no_data(tmp_path):
    """Returns None if no text has been recorded for the agent."""
    o, _ = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "ag3", _TM_TOP)

    assert o.assistant_text_for_turn("ag3", "toolu_xyz") is None


# ---------------------------------------------------------------------------
# build_hooks callback (unit test of hook branches)
# ---------------------------------------------------------------------------


def test_hook_success_path_delivers_message(tmp_path):
    """Hook on_signal_review delivers to leader when all guards pass."""
    from beidou.sdk_agent import build_hooks

    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "leader_ag", _TM_TOP)
    _seed_agent(o, "child_ag", _TM_TOP)

    # Pre-record assistant text bound to the signal_review tool_use_id.
    o.record_assistant_text("child_ag", "I completed the work.", ["toolu_done"])

    hooks_dict = build_hooks(o, "child_ag", "leader_ag")
    matchers = hooks_dict["PostToolUse"]
    callback = matchers[0].hooks[0]

    input_data = {
        "tool_name": "mcp__beidou__signal_review",
        "tool_input": {"detail": "[REVIEW REQUIRED]\nrole=worker agent=child_ag\nDeliverables: done."},
        "tool_response": {"is_error": False},
    }

    async def body():
        result = await callback(input_data, "toolu_done", None)
        assert result == {}

        await asyncio.sleep(0.05)
        # Verify completion.reported was emitted.
        event_names = [c[0] for c in emitter.calls]
        assert "completion.reported" in event_names

    run(body())


def test_hook_wrong_tool_name_is_noop(tmp_path):
    """Hook is a no-op when tool_name doesn't match the matcher guard."""
    from beidou.sdk_agent import build_hooks

    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "leader2", _TM_TOP)
    _seed_agent(o, "child2", _TM_TOP)

    hooks_dict = build_hooks(o, "child2", "leader2")
    callback = hooks_dict["PostToolUse"][0].hooks[0]

    # Pass a non-matching tool_name — the guard should return {} early.
    input_data = {
        "tool_name": "mcp__beidou__send_message",
        "tool_input": {"detail": "not a review"},
        "tool_response": {},
    }
    run(callback(input_data, "toolu_1", None))
    # No events should have been emitted — wrong tool_name triggers guard.
    assert not any(c[0].startswith("completion") for c in emitter.calls)


def test_hook_is_error_is_skipped(tmp_path):
    """Hook skips delivery when is_error=True on the tool response."""
    from beidou.sdk_agent import build_hooks

    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "leader3", _TM_TOP)
    _seed_agent(o, "child3", _TM_TOP)

    hooks_dict = build_hooks(o, "child3", "leader3")
    callback = hooks_dict["PostToolUse"][0].hooks[0]

    input_data = {
        "tool_name": "mcp__beidou__signal_review",
        "tool_input": {"detail": "[REVIEW REQUIRED]\nrole=worker agent=child_ag\nDeliverables: done."},
        "tool_response": {"is_error": True},
    }
    run(callback(input_data, "toolu_err", None))
    # No completion events.
    assert not any(c[0].startswith("completion") for c in emitter.calls)


def test_hook_is_error_true_is_noop(tmp_path):
    """Hook is a no-op when is_error=True (primitive rejected the call).
    Previously tested as 'completion.empty on no summary'; that path was
    replaced by primitive-level envelope_missing rejection."""
    from beidou.sdk_agent import build_hooks

    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "leader4", _TM_TOP)
    _seed_agent(o, "child4", _TM_TOP)

    hooks_dict = build_hooks(o, "child4", "leader4")
    callback = hooks_dict["PostToolUse"][0].hooks[0]

    # Simulate a primitive rejection (is_error=True) — the hook must be a no-op.
    input_data = {
        "tool_name": "mcp__beidou__signal_review",
        "tool_input": {"detail": "[REVIEW REQUIRED]\nrole=worker agent=child_ag\nDeliverables: done."},
        "tool_response": {"is_error": True},
    }

    async def body():
        await callback(input_data, "toolu_empty", None)
        await asyncio.sleep(0.05)
        event_names = [c[0] for c in emitter.calls]
        # No completion events should be emitted — hook is strict no-op.
        assert "completion.empty" not in event_names
        assert "completion.reported" not in event_names
        assert "completion.envelope_synthesized" not in event_names

    run(body())


def test_hook_root_sentinel_routes_through_user_gateway(tmp_path):
    """Root completion-report (USER_SENTINEL) routes through gateway_ask_user_structured.

    Approve → terminate_root() is called; no completion.empty(root_no_leader) is emitted.
    """
    from unittest.mock import AsyncMock

    from beidou.sdk_agent import build_hooks
    from beidou.orchestrator import USER_SENTINEL

    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "root_ag", _TM_TOP)

    # Stub the orchestrator's gateway round-trip and root terminate.
    o.gateway_ask_user_structured = AsyncMock(  # type: ignore[assignment]
        return_value={
            "answers": [{"selected_values": ["approve"], "selected_labels": ["Approve"], "text": None}],
            "answer_text": "Approve",
        }
    )
    o.terminate_root = AsyncMock()  # type: ignore[assignment]

    o.record_assistant_text("root_ag", "Root finished.", ["toolu_root"])
    hooks_dict = build_hooks(o, "root_ag", USER_SENTINEL)
    callback = hooks_dict["PostToolUse"][0].hooks[0]

    input_data = {
        "tool_name": "mcp__beidou__signal_review",
        "tool_input": {"detail": "[REVIEW REQUIRED]\nrole=worker agent=child_ag\nDeliverables: done."},
        "tool_response": {},
    }

    async def body():
        await callback(input_data, "toolu_root", None)
        await asyncio.sleep(0.05)

        # Gateway was asked once.
        o.gateway_ask_user_structured.assert_awaited_once()
        # Approve outcome → terminate_root awaited.
        o.terminate_root.assert_awaited_once()
        # No legacy root_no_leader event.
        for ev in emitter.calls:
            if ev[0] == "completion.empty":
                assert ev[3].get("reason") != "root_no_leader"
        # completion.reported(decision=approve) emitted.
        reported = [c for c in emitter.calls if c[0] == "completion.reported"]
        assert reported and reported[-1][3].get("decision") == "approve"

    run(body())


# ---------------------------------------------------------------------------
# Watchdog backstop: terminate-grace cancel.
# ---------------------------------------------------------------------------


def test_watchdog_cancels_run_task_after_grace_deadline(tmp_path):
    """Pass C of _watchdog_tick cancels a non-consuming agent after grace expires.

    Scenario:
      1. Agent is seeded with a long-running fake run_task (asyncio.sleep(3600)).
      2. A terminate sentinel is delivered via inbox_put, stamping grace deadline.
      3. Deadline is backdated to simulate expiry.
      4. _watchdog_tick is invoked once.
      5. Assert: run_task is cancelled, terminate.forced(reason="watchdog_grace")
         emitted, deadline cleared.
      6. A second _watchdog_tick does NOT emit a duplicate terminate.forced event.
    """
    o, emitter = _make_orchestrator(tmp_path)
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_team(o, "tm_child", leader_id="L", depth=1, parent=_TM_TOP)
    _seed_agent(o, "L", _TM_TOP, role="leader")
    rec = _seed_agent(o, "A", "tm_child")

    async def body():
        # Step 1: attach a non-completing fake run_task to the agent.
        rec.run_task = asyncio.create_task(asyncio.sleep(3600))

        # Step 2: post a terminate sentinel, which stamps terminate_grace_deadline.
        sentinel = Message(
            from_id="beidou",
            content="__terminate__",
            ts=time.time(),
            message_id="sentinel_wg_1",
            kind="terminate",
        )
        await o.inbox_put("A", sentinel)
        assert rec.terminate_grace_deadline is not None, (
            "inbox_put should stamp terminate_grace_deadline on terminate sentinel"
        )

        # Step 3: backdate the deadline so it looks expired.
        rec.terminate_grace_deadline = time.time() - 1.0

        # Step 4: invoke one watchdog tick.
        await o._watchdog_tick()

        # Let the cancellation propagate.
        await asyncio.sleep(0)

        # Step 5a: run_task should be cancelled (done and cancelled).
        assert rec.run_task.done(), "run_task must be done after watchdog cancel"
        assert rec.run_task.cancelled(), "run_task must be cancelled by watchdog"

        # Step 5b: terminate.forced event with reason="watchdog_grace" and caller_id="watchdog".
        await asyncio.sleep(0)  # flush any async emit tasks
        forced = [c for c in emitter.calls if c[0] == "terminate.forced"]
        assert len(forced) == 1, f"expected 1 terminate.forced event, got {len(forced)}"
        _event, _agent_id, _team_id, kw = forced[0]
        assert kw.get("reason") == "watchdog_grace"
        assert kw.get("caller_id") == "watchdog"
        assert _agent_id == "A"

        # Step 5c: deadline cleared to prevent re-fire.
        assert rec.terminate_grace_deadline is None, (
            "terminate_grace_deadline must be cleared after watchdog cancel"
        )

        # Step 6: second tick produces no new terminate.forced event.
        await o._watchdog_tick()
        await asyncio.sleep(0)
        forced2 = [c for c in emitter.calls if c[0] == "terminate.forced"]
        assert len(forced2) == 1, (
            "second _watchdog_tick must not re-fire terminate.forced after deadline cleared"
        )

    run(body())


# ---------------------------------------------------------------------------
# Crash recovery (agent-runtime.md §5.1).
# ---------------------------------------------------------------------------


def test_crash_strike1_resumes_session(tmp_path, monkeypatch):
    """Strike 1: resume via resume_session_id with recovery prompt."""
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "CRASHER", _TM_TOP, role="coder")
    rec.last_session_id = "sess_strike1_test"

    call_log: list[tuple] = []

    async def crash_once(orch, spec):
        call_log.append(("run_agent", getattr(spec, "resume_session_id", None), spec.task))
        if len(call_log) == 1:
            raise RuntimeError("simulated subprocess crash")
        # Second call — resume succeeded.
        orch._agents[spec.caller_id].terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", crash_once)

    async def body():
        spec = SpawnSpec(
            caller_id="CRASHER",
            skill_name="fake",
            skill_root=o.skill_root,
            task="do work",
        )
        result = await o._run_agent_with_policy(rec, spec)
        # Drain background emitter tasks.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert result.terminated is True
        # First call: original spec (no resume_session_id).
        assert call_log[0] == ("run_agent", None, "do work")
        # Second call: resume with session_id and recovery prompt.
        assert call_log[1][0] == "run_agent"
        assert call_log[1][1] == "sess_strike1_test"
        assert "previous session crashed" in call_log[1][2]
        # crash_strikes reset after successful run.
        assert rec.crash_strikes == 0
        # agent_crashed event emitted.
        crashed_events = [c for c in emitter.calls if c[0] == "agent_crashed"]
        assert len(crashed_events) == 1

    run(body())


def test_crash_strike2_fresh_restart(tmp_path, monkeypatch):
    """Strike 2: fresh restart without resume_session_id."""
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "CRASHER2", _TM_TOP, role="coder")
    rec.last_session_id = "sess_strike2_test"

    call_log: list[tuple] = []

    async def crash_twice(orch, spec):
        call_log.append(("run_agent", getattr(spec, "resume_session_id", None), spec.task))
        if len(call_log) <= 2:
            raise RuntimeError("simulated subprocess crash")
        # Third call succeeds.
        orch._agents[spec.caller_id].terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", crash_twice)

    async def body():
        spec = SpawnSpec(
            caller_id="CRASHER2",
            skill_name="fake",
            skill_root=o.skill_root,
            task="do work",
        )
        result = await o._run_agent_with_policy(rec, spec)
        # Drain background emitter tasks.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert result.terminated is True
        # Call 1: original.
        assert call_log[0] == ("run_agent", None, "do work")
        # Call 2: strike 1 — resume attempt.
        assert call_log[1][1] == "sess_strike2_test"
        # Call 3: strike 2 — fresh restart (no session_id, workspace prompt).
        assert call_log[2][1] is None
        assert "Start fresh" in call_log[2][2] or "workspace" in call_log[2][2].lower()
        # Two agent_crashed events.
        crashed_events = [c for c in emitter.calls if c[0] == "agent_crashed"]
        assert len(crashed_events) == 2
        # crash_strikes reset after success.
        assert rec.crash_strikes == 0

    run(body())


def test_crash_three_strikes_escalates(tmp_path, monkeypatch):
    """Three strikes escalate: message to leader for members, root_crash_escalation for root."""
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    # Test with a non-root member.
    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    _seed_agent(o, "LEADER", _TM_TOP, role="leader")
    _seed_team(o, "tm_child", leader_id="LEADER", depth=1, parent=_TM_TOP)
    rec = _seed_agent(o, "CRASHER3", "tm_child", role="coder")

    async def always_crash(orch, spec):
        raise RuntimeError("always crash")

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", always_crash)

    async def body():
        spec = SpawnSpec(
            caller_id="CRASHER3",
            skill_name="fake",
            skill_root=o.skill_root,
            task="do work",
        )
        result = await o._run_agent_with_policy(rec, spec)
        # Drain emitter.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert result.stop_reason == "crash_escalated"
        assert result.terminated is False
        assert rec.crash_strikes == 3
        # Leader received escalation message.
        leader_inbox = o._agents["LEADER"].inbox
        assert leader_inbox.qsize() == 1
        msg = leader_inbox.get_nowait()
        assert "CRASHER3" in msg.content
        assert "crashed" in msg.content
        # Three agent_crashed events.
        crashed_events = [c for c in emitter.calls if c[0] == "agent_crashed"]
        assert len(crashed_events) == 3

    run(body())


def test_crash_then_clean_resets_strikes(tmp_path, monkeypatch):
    """A successful run_agent() after a crash resets crash_strikes to 0."""
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "RESETTER", _TM_TOP, role="coder")
    rec.last_session_id = "sess_reset_test"

    call_count = {"n": 0}

    async def crash_then_ok(orch, spec):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("crash 1")
        if call_count["n"] == 2:
            # Strike 1 resume succeeds, but then contract violation.
            return _make_result(terminated=False)
        # Call 3: contract resume.
        orch._agents[spec.caller_id].terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", crash_then_ok)

    async def body():
        spec = SpawnSpec(
            caller_id="RESETTER",
            skill_name="fake",
            skill_root=o.skill_root,
            task="do work",
        )
        result = await o._run_agent_with_policy(rec, spec)
        # Drain background emitter tasks.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert result.terminated is True
        # After crash run_agent succeeded (call 2 returned normally), crash_strikes are 0.
        assert rec.crash_strikes == 0
        # contract_strikes should be 1 (the one violation on call 2).
        assert rec.contract_strikes == 1
        # Exactly one agent_crashed event.
        crashed_events = [c for c in emitter.calls if c[0] == "agent_crashed"]
        assert len(crashed_events) == 1

    run(body())


def test_cancelled_error_not_treated_as_crash(tmp_path, monkeypatch):
    """asyncio.CancelledError is re-raised, not caught as a crash."""
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "CANCELLED", _TM_TOP, role="coder")

    async def raise_cancelled(orch, spec):
        raise asyncio.CancelledError("watchdog cancel")

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", raise_cancelled)

    async def body():
        spec = SpawnSpec(
            caller_id="CANCELLED",
            skill_name="fake",
            skill_root=o.skill_root,
            task="do work",
        )
        with pytest.raises(asyncio.CancelledError):
            await o._run_agent_with_policy(rec, spec)
        # Drain background emitter tasks.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        # No agent_crashed event.
        crashed_events = [c for c in emitter.calls if c[0] == "agent_crashed"]
        assert len(crashed_events) == 0
        # crash_strikes not incremented.
        assert rec.crash_strikes == 0

    run(body())


def test_inflight_tools_reset_on_crash(tmp_path, monkeypatch):
    """Crash resets inflight_tools to 0 (stale from pre-crash drain)."""
    o, emitter = _make_orchestrator(tmp_path)
    import beidou.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "load_skill", lambda r, n: type("S", (), {"name": n})())

    _seed_team(o, _TM_TOP, leader_id=USER_SENTINEL, depth=0)
    rec = _seed_agent(o, "INFLIGHT", _TM_TOP, role="coder")
    rec.inflight_tools = 5  # Simulate stale inflight tools from before crash.
    rec.last_session_id = "sess_inflight_test"

    call_count = {"n": 0}

    async def crash_then_ok(orch, spec):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("crash")
        orch._agents[spec.caller_id].terminate_consumed = True
        return _make_result(terminated=True)

    monkeypatch.setattr(orch_module._agent_loop, "run_agent", crash_then_ok)

    async def body():
        spec = SpawnSpec(
            caller_id="INFLIGHT",
            skill_name="fake",
            skill_root=o.skill_root,
            task="do work",
        )
        result = await o._run_agent_with_policy(rec, spec)
        # Drain background emitter tasks.
        if o._bg_tasks:
            await asyncio.gather(*list(o._bg_tasks), return_exceptions=True)
        assert result.terminated is True
        # inflight_tools was reset to 0 on crash.
        assert rec.inflight_tools == 0

    run(body())
