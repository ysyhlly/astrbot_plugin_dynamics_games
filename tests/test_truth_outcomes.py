import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_games.bridges.memory_bridge import sync_experience_to_memory
from astrbot_plugin_dynamics_games.bridges.chronicle_bridge import sync_outcome_to_chronicle
from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator
from astrbot_plugin_dynamics_games.core.store import ActivityStore
from astrbot_plugin_dynamics_games.engines.truth_game.core.commands import (
    cmd_create, cmd_end, cmd_help, cmd_join, cmd_leave, cmd_next, cmd_skip,
    cmd_start, cmd_status, handle_playing_message,
)
from astrbot_plugin_dynamics_games.engines.truth_game.core.room import RoomManager

from test_dynamics_games import FakeEvent


async def question(room, _manager):
    room.asked_history.append("一轮问题")
    room.ask_counts["200"] = len(room.asked_history)
    room.round_stage = "question_out"
    return room.asked_history[-1]


async def started_room(coordinator, **config):
    rooms = RoomManager(config)
    rooms.outcome_callback = coordinator.enqueue_truth_outcome
    rooms.advance_hook = question
    host = FakeEvent(uid="100", name="阿青", umo="qq-alt:GroupMessage:888001")
    player = FakeEvent(uid="200", name="阿明", umo=host.unified_msg_origin)
    await cmd_create(rooms, host)
    await cmd_join(rooms, player)
    await cmd_start(rooms, host)
    return rooms, host, player


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["manual", "max_questions", "idle", "too_few_players"])
async def test_truth_settlement_paths_share_one_durable_snapshot(tmp_path, ending):
    store = ActivityStore(tmp_path / "games.sqlite3")
    coordinator = ActivityCoordinator(
        store, None, {"memory_sync_enabled": False, "chronicle_sync_enabled": False}
    )
    rooms, host, player = await started_room(
        coordinator, bot_ask_enabled=False,
        round_idle_seconds=0.03 if ending == "idle" else 300,
        max_idle_rounds=1, max_questions_per_game=2 if ending == "max_questions" else 30,
    )
    rkey = host.unified_msg_origin
    room_id = rooms.rooms[rkey].room_id
    try:
        if ending == "manual":
            denied = await cmd_end(rooms, player)
            assert "房主或管理员" in denied.text()
            replies = await asyncio.gather(cmd_end(rooms, host), cmd_end(rooms, host))
            assert sum("共进行了 1 轮" in reply.text() for reply in replies) == 1
        elif ending == "max_questions":
            assert "共进行了 2 轮" in (await cmd_next(rooms, player)).text()
        elif ending == "idle":
            await asyncio.wait_for(rooms._idle_tasks[rkey], 1)
        else:
            assert "共进行了 1 轮" in (await cmd_leave(rooms, player)).text()

        assert rkey not in rooms.rooms
        assert store.pending_truth_rooms() == [room_id]
        assert not store.list_outcomes()

        # Recover the handoff using a fresh store and coordinator, as on reload.
        resumed_store = ActivityStore(store.path)
        resumed = ActivityCoordinator(resumed_store, None, coordinator.config)
        await resumed.flush_truth_outcomes()
        outcomes = resumed_store.list_outcomes(umo=host.unified_msg_origin)
        assert len(outcomes) == 1
        assert resumed_store.archive_truth_room(room_id) == outcomes[0]["activity_id"]
        outcome = resumed_store.get_outcome(outcomes[0]["activity_id"])
        assert outcome["stats"]["rounds"] == (2 if ending in {"max_questions", "idle"} else 1)
        assert outcome["stats"]["end_reason"] == ending
        assert {p["uid"] for p in outcome["participants"]} == {"100", "200"}
        activity = resumed_store.get_activity(outcome["activity_id"])
        assert activity["created_at"] <= activity["completed_at"]
        assert not resumed_store.pending_truth_rooms()
    finally:
        await rooms.terminate()


@pytest.mark.asyncio
async def test_truth_local_materialization_and_remote_sync_retry(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "games.sqlite3")
    coordinator = ActivityCoordinator(store, None, {})
    rooms, host, _ = await started_room(coordinator)
    gid = host.get_group_id()
    rkey = host.unified_msg_origin
    room_id = rooms.rooms[rkey].room_id
    await cmd_end(rooms, host)
    assert not (await rooms.next(gid, "100", True))[0]

    real_save = store.save_completion
    monkeypatch.setattr(store, "save_completion", lambda *args, **kwargs: False)
    await coordinator.flush_truth_outcomes()
    assert store.pending_truth_rooms() == [room_id]
    assert not store.list_activities(umo=host.unified_msg_origin)
    monkeypatch.setattr(store, "save_completion", real_save)

    sync = AsyncMock(side_effect=[
        {"ok": False, "status": "unavailable", "doc_id": 0, "reason": "offline"},
        {"ok": True, "status": "synced", "doc_id": 22, "reason": ""},
    ])
    monkeypatch.setattr("astrbot_plugin_dynamics_games.core.coordinator.sync_experience_to_memory", sync)
    await coordinator.flush_truth_outcomes()
    resumed = ActivityCoordinator(ActivityStore(store.path), None, {})
    await resumed.flush_truth_outcomes()
    await resumed.flush_truth_outcomes()
    assert sync.await_count == 2
    first, retry = [call.kwargs for call in sync.await_args_list]
    assert first["activity_id"] == retry["activity_id"]
    assert first["occurred_at"] == retry["occurred_at"]
    assert retry["source_refs"] == []
    assert store.get_outcome(first["activity_id"])["memory_doc_id"] == 22
    assert rkey not in rooms.rooms


@pytest.mark.asyncio
async def test_truth_end_holds_completed_snapshot_until_handoff_is_durable(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "games.sqlite3")
    coordinator = ActivityCoordinator(store, None, {"memory_sync_enabled": False})
    rooms, host, _ = await started_room(coordinator)
    gid = host.get_group_id()
    rkey = host.unified_msg_origin
    room = rooms.rooms[rkey]
    real_enqueue = store.enqueue_truth_room

    def fail_before_commit(_snapshot):
        raise sqlite3.OperationalError("injected handoff failure")

    monkeypatch.setattr(store, "enqueue_truth_room", fail_before_commit)
    ok, status = await rooms.end(rkey, "100", False)
    assert not ok and "暂未写入" in status["error"]
    assert rooms.rooms[rkey] is room and room.phase == "settled"
    assert not (await rooms.start(rkey, "100", False))[0]
    assert not (await rooms.next(rkey, "100", False))[0]
    assert not rooms.create(gid, "100", "阿青", umo=rkey)[0]
    assert not store.pending_truth_rooms()

    monkeypatch.setattr(store, "enqueue_truth_room", real_enqueue)
    await rooms.flush_outcomes()
    assert store.pending_truth_rooms() == [room.room_id]
    assert rkey not in rooms.rooms
    await rooms.terminate()


@pytest.mark.asyncio
async def test_memory_bridge_surfaces_source_rejection(monkeypatch):
    memory = type("Memory", (), {})()
    memory.record_activity_experience = AsyncMock(return_value={
        "schema_version": 1, "recorded": False, "reason": "source_unresolved",
    })
    monkeypatch.setattr(
        "astrbot_plugin_dynamics_games.bridges.memory_bridge.find_memory_plugin",
        lambda context: memory,
    )
    receipt = await sync_experience_to_memory(
        None, umo="qq:GroupMessage:10", activity_id="G100", category="reading",
        category_label="共读", title="共读", event_kind="completed",
        summary="小组已完成共读", participants=[{"uid": "1", "name": "阿青"}],
        source_refs=[{"message_id": "10000", "uid": "1", "ts": 1234.5}],
        occurred_at=1235.5,
    )
    assert receipt == {
        "ok": False, "status": "source_unresolved", "doc_id": 0, "reason": "source_unresolved",
    }
    payload = memory.record_activity_experience.await_args.args[0]
    assert payload["occurred_at"] == 1235.5 and payload["source_refs"][0]["message_id"] == "10000"


@pytest.mark.asyncio
async def test_same_group_number_on_another_adapter_cannot_access_truth_room(tmp_path):
    coordinator = ActivityCoordinator(ActivityStore(tmp_path / "games.sqlite3"), None, {})
    rooms, host, _ = await started_room(coordinator)
    gid = host.get_group_id()
    rkey = host.unified_msg_origin
    other = FakeEvent(uid="100", name="另一个适配器", is_admin=True,
                      umo=f"another-qq:GroupMessage:{gid}")
    room = rooms.rooms[rkey]
    room.last_pair = ("100", "200")
    try:
        for command in (cmd_join, cmd_start, cmd_next, cmd_skip, cmd_leave, cmd_end, cmd_status):
            assert "还没有" in (await command(rooms, other)).text()
        assert "真心话" in (await cmd_help(rooms, other)).text()
        for stage, uid, text, quote in (
            ("awaiting_asker_mode", "100", "自己出题", False),
            ("awaiting_asker_question", "100", "外部题目", False),
            ("question_out", "200", "外部回答", True),
        ):
            room.round_stage = stage
            assert await handle_playing_message(
                rooms, gid, uid, text, quote, True, umo=other.unified_msg_origin
            ) is None
            assert room.round_stage == stage
        assert room.advance_count == 1 and len(room.asked_history) == 1

        # Creating a room on the second adapter succeeds independently without blocking host's room.
        created_other = await cmd_create(rooms, other)
        assert "已创建" in created_other.text()
        assert other.unified_msg_origin in rooms.rooms
        assert rooms.rooms[rkey] is room

        assert "共进行了 1 轮" in (await cmd_end(rooms, host)).text()
        await coordinator.flush_truth_outcomes()
        assert len(coordinator.store.list_outcomes(umo=host.unified_msg_origin)) == 1
        assert not coordinator.store.list_outcomes(umo=other.unified_msg_origin)
        assert other.unified_msg_origin in rooms.rooms
    finally:
        await rooms.terminate()


@pytest.mark.asyncio
async def test_chronicle_bridge_sends_only_real_message_ids(monkeypatch):
    chronicle = type("Chronicle", (), {})()
    chronicle.record_valuable_outcome = AsyncMock(return_value={"admitted": True, "event_id": 1})
    monkeypatch.setattr(
        "astrbot_plugin_dynamics_games.bridges.chronicle_bridge.find_chronicle_plugin",
        lambda context: chronicle,
    )
    umo = "qq:GroupMessage:10"
    arguments = dict(
        umo=umo, activity={"id": "M100", "status": "completed", "title": "真心话", "category": "mini_game"},
        participants=[{"uid": "1"}, {"uid": "2"}, {"uid": "3"}],
        summary="已完成", valuable=True, importance=1, admission_mode="auto",
    )
    derived = {"uid": "1", "content": "汇总文本", "message_id": ""}
    await sync_outcome_to_chronicle(None, contributions=[derived], **arguments)
    payload = chronicle.record_valuable_outcome.await_args.args[1]
    assert payload["sources"] == []
    assert payload["source_basis"] == "derived_outcome"
    await sync_outcome_to_chronicle(
        None, contributions=[derived, {**derived, "message_id": "919"}], **arguments
    )
    payload = chronicle.record_valuable_outcome.await_args.args[1]
    assert [source["id"] for source in payload["sources"]] == [919]
    assert payload["source_basis"] == "message_refs"
