"""Batch 4 regression tests for astrbot_plugin_dynamics_games.

Covers:
- P0-1: Steam 9 yield commands must NOT delete temp PNG in finally before RespondStage
        (Image.fromBytes in-memory preference + deferred TTL cleanup drained on terminate).
- P0-2: /steam off, /steam clear_groupids, /steam delpush_group, /steam delid prune
        orphan active_sessions, stop achievement polling tasks, and stop rank inflation.
- P1-6: Multi-adapter same bare group_id isolation + legacy bare lookup + commit_poll_batch
        preventing in-flight poll from resurrecting deleted/disabled active_sessions.
- P0-3: truth_game proactive timer broadcasts route through context.send_message(umo, ...)
        instead of failing on raw OneBot bot.send(text), with FakeBot fallback for tests.
- P1-4: ActivityStore.list_outcomes / get_outcome top-level participant_count & contribution_count.
- P1-5: _extract_tail prefix-anchored parsing + rollpig case-insensitive wake-free dedup.
- P2 & Packaging: sgdb_api_base /api/v2 hint, what_to_eat empty mcp_server_name auto-scan,
        app.js mem.detail fallback, theme.js null guards, package_release install ZIP hygiene.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_games.core.store import ActivityStore
from astrbot_plugin_dynamics_games.engines.rollpig.main import RollPigPlugin
from astrbot_plugin_dynamics_games.engines.steam_monitor import (
    ActiveSession,
    MIGRATION_KV_KEY,
    SteamMonitorStore,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor import main as steam_main_mod
from astrbot_plugin_dynamics_games.engines.steam_monitor.main import SteamMonitorEngine
from astrbot_plugin_dynamics_games.engines.steam_monitor.renderer import (
    STEAM_CARD_TEMP_PREFIX,
    STEAM_CARD_TEMP_SUFFIX,
)
from astrbot_plugin_dynamics_games.engines.truth_game.core.commands import cmd_create
from astrbot_plugin_dynamics_games.engines.truth_game.core.room import RoomManager
from astrbot_plugin_dynamics_games.engines.what_to_eat.main import WhatToEatPlugin
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin
from astrbot_plugin_dynamics_games.tools import package_release

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _FakeSteamEvent:
    def __init__(
        self,
        *,
        group_id: str = "10086",
        sender_id: str = "20001",
        is_admin: bool = True,
        message_str: str = "",
        umo: str | None = None,
    ) -> None:
        self._gid = group_id
        self._uid = sender_id
        self._admin = is_admin
        self.message_str = message_str
        self.unified_msg_origin = (
            umo
            if umo is not None
            else (
                f"aiocqhttp:GroupMessage:{group_id}"
                if group_id
                else f"aiocqhttp:FriendMessage:{sender_id}"
            )
        )
        self.message_obj = SimpleNamespace(message=[])

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_self_id(self) -> str:
        return "999999"

    def is_admin(self) -> bool:
        return self._admin

    def stop_event(self) -> None:
        pass

    def plain_result(self, text: str) -> dict:
        return {"kind": "plain", "text": text}

    def image_result(self, image_ref: str) -> dict:
        return {"kind": "image", "image_ref": image_ref}

    def chain_result(self, items: list) -> dict:
        return {"kind": "chain", "items": items}


class _FakeSteamContext:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, object]] = []
        self.owner = SimpleNamespace(
            name="astrbot_plugin_dynamics_games",
            plugin_id="ysyhlly/astrbot_plugin_dynamics_games",
            module_enabled=lambda key: True,
        )

    async def send_message(self, umo: str, payload: object) -> None:
        self.sent_messages.append((umo, payload))


async def _make_steam_engine(
    tmp_path: pathlib.Path, *, html_render_fn=None
) -> tuple[SteamMonitorEngine, _FakeSteamContext]:
    ctx = _FakeSteamContext()
    cfg = {
        "steam_api_key": "TEST_KEY",
        "exit_debounce_seconds": 180,
        "enable_achievement_poll": True,
    }
    engine = SteamMonitorEngine(
        ctx,
        cfg,
        data_dir=tmp_path,
        start_background_tasks=False,
        html_render_fn=html_render_fn,
    )
    store = SteamMonitorStore(tmp_path / "steam_monitor.db")
    store.set_kv(MIGRATION_KV_KEY, "1")
    await engine.initialize()
    return engine, ctx


async def _collect(agen) -> list:
    out = []
    async for item in agen:
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# P0-1: Steam yield commands must keep temp PNG alive past yield + fromBytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_1_steam_yield_card_survives_until_ttl_and_prefers_from_bytes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    created_paths: list[pathlib.Path] = []

    async def fake_render(html: str, data: dict, return_url: bool = False, options=None):
        fd, raw_path = tempfile.mkstemp(
            prefix=STEAM_CARD_TEMP_PREFIX, suffix=STEAM_CARD_TEMP_SUFFIX
        )
        p = pathlib.Path(raw_path)
        p.write_bytes(b"\x89PNG\r\n\x1a\nFAKE_CARD_BYTES")
        import os

        os.close(fd)
        created_paths.append(p)
        return str(p)

    # 1) When Image is None (image_result(path) path), file MUST STILL EXIST after yield finishes!
    monkeypatch.setattr(steam_main_mod, "Image", None)
    engine, _ = await _make_steam_engine(tmp_path / "e1", html_render_fn=fake_render)
    engine._temp_card_ttl_seconds = 60.0
    try:
        sid = "76561198000001001"
        ev = _FakeSteamEvent(group_id="10086", sender_id="20001", is_admin=True)
        await _collect(engine.cmd_bind(ev, sid, "阿明"))
        engine.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=(
                {
                    sid: {
                        "steamid": sid,
                        "name": "Ming",
                        "gameid": "730",
                        "gameextrainfo": "CS2",
                        "personastate": 1,
                    }
                },
                set(),
            )
        )
        engine.steam_client.get_game_names = AsyncMock(return_value=("CS2", "CS2"))
        engine.steam_client.get_game_cover_url = AsyncMock(return_value="")

        replies = await _collect(engine.cmd_list(ev))
        assert len(replies) == 1
        assert replies[0]["kind"] == "image"
        card_path = pathlib.Path(replies[0]["image_ref"])
        # Critical P0-1 assertion: file MUST survive generator exit for RespondStage!
        assert card_path.is_file(), "Temp card was deleted before RespondStage could send it!"
    finally:
        await engine.terminate()
    # terminate() MUST clean up deferred temp cards cleanly
    assert not created_paths[0].exists()

    # 2) When Image.fromBytes is available, _make_reply embeds bytes in-memory via chain_result
    class _StubImage:
        @classmethod
        def fromBytes(cls, data: bytes):
            return ("bytes", data)

        @classmethod
        def fromFileSystem(cls, path: str):
            return ("fs", path)

    monkeypatch.setattr(steam_main_mod, "Image", _StubImage)
    engine2, _ = await _make_steam_engine(tmp_path / "e2", html_render_fn=fake_render)
    engine2._temp_card_ttl_seconds = 0.02
    try:
        sid = "76561198000001002"
        ev = _FakeSteamEvent(group_id="10086", sender_id="20002", is_admin=True)
        await _collect(engine2.cmd_bind(ev, sid, "小美"))
        engine2.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=({sid: {"steamid": sid, "name": "Mei", "personastate": 1}}, set())
        )
        replies2 = await _collect(engine2.cmd_list(ev))
        assert len(replies2) == 1
        assert replies2[0]["kind"] == "chain"
        assert replies2[0]["items"][0] == ("bytes", b"\x89PNG\r\n\x1a\nFAKE_CARD_BYTES")
        # Wait past short TTL to confirm deferred cleanup removes temp file automatically
        await asyncio.sleep(0.08)
        assert not created_paths[-1].exists()
    finally:
        await engine2.terminate()


# ---------------------------------------------------------------------------
# P0-2 & P1-6: Orphan active_sessions pruning, achievement stop, adapter isolation,
#              and commit_poll_batch anti-resurrection guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_2_off_clear_delpush_prune_active_sessions_and_stop_achievements(
    tmp_path: pathlib.Path,
):
    engine, _ = await _make_steam_engine(tmp_path)
    assert engine.store is not None and engine.achievement_service is not None
    engine.steam_client.fetch_player_achievements = AsyncMock(return_value=set())
    try:
        sid_only_g1 = "76561198000002001"
        sid_shared = "76561198000002002"
        sid_routed = "76561198000002003"
        ev_g1 = _FakeSteamEvent(group_id="111", umo="aiocqhttp:GroupMessage:111", is_admin=True)
        ev_g2 = _FakeSteamEvent(group_id="222", umo="aiocqhttp:GroupMessage:222", is_admin=True)

        await _collect(engine.cmd_addid(ev_g1, sid_only_g1))
        await _collect(engine.cmd_addid(ev_g1, sid_shared))
        await _collect(engine.cmd_addid(ev_g2, sid_shared))
        await _collect(engine.cmd_push_group(ev_g2, sid_routed))

        now_ts = datetime.now().astimezone().timestamp()
        for sid in (sid_only_g1, sid_shared, sid_routed):
            engine.store.save_active_session(
                ActiveSession(
                    steam_id=sid,
                    game_id="730",
                    game_name="CS2",
                    started_at=now_ts - 3600.0,
                    state="playing",
                    last_seen_at=now_ts,
                    player_name=sid,
                )
            )
            engine.achievement_service.start_tracking(sid, "730", "CS2", sid, "")

        # 1) /steam off in g1 prunes sid_only_g1, keeps sid_shared (monitored by enabled g2)
        await _collect(engine.cmd_off(ev_g1))
        active = engine.store.load_active_sessions()
        assert sid_only_g1 not in active
        assert sid_shared in active
        assert not engine.achievement_service.is_tracking(sid_only_g1)
        assert engine.achievement_service.is_tracking(sid_shared)

        # Rank in g1 must NOT be inflated by pruned sid_only_g1
        rank_g1 = await engine.get_group_steam_rank_summary("aiocqhttp:111", days=1)
        ranked_sids = {r["steam_id"] for r in rank_g1["rankings"]}
        assert sid_only_g1 not in ranked_sids

        # 2) /steam delpush_group in g2 prunes sid_routed and stops achievement task
        await _collect(engine.cmd_delpush_group(ev_g2, sid_routed))
        assert sid_routed not in engine.store.load_active_sessions()
        assert not engine.achievement_service.is_tracking(sid_routed)

        # 3) /steam clear_groupids in g2 prunes sid_shared (g1 is off!) and stops achievement task
        await _collect(engine.cmd_clear_groupids(ev_g2))
        assert sid_shared not in engine.store.load_active_sessions()
        assert not engine.achievement_service.is_tracking(sid_shared)
    finally:
        await engine.terminate()


@pytest.mark.asyncio
async def test_p1_6_multi_adapter_group_isolation_and_poll_no_resurrection(
    tmp_path: pathlib.Path,
):
    engine, _ = await _make_steam_engine(tmp_path)
    assert engine.store is not None
    try:
        sid_a = "76561198000003001"
        sid_b = "76561198000003002"
        ev_a = _FakeSteamEvent(group_id="10086", umo="aiocqhttp:GroupMessage:10086", is_admin=True)
        ev_b = _FakeSteamEvent(group_id="10086", umo="napcat:GroupMessage:10086", is_admin=True)

        await _collect(engine.cmd_addid(ev_a, sid_a))
        await _collect(engine.cmd_addid(ev_b, sid_b))

        assert engine.store.list_group_players("aiocqhttp:10086") == [sid_a]
        assert engine.store.list_group_players("napcat:10086") == [sid_b]

        # Turning off adapter A does NOT disable adapter B or prune sid_b
        now_ts = datetime.now().astimezone().timestamp()
        for sid in (sid_a, sid_b):
            engine.store.save_active_session(
                ActiveSession(
                    steam_id=sid,
                    game_id="570",
                    game_name="Dota 2",
                    started_at=now_ts - 600.0,
                    state="playing",
                    last_seen_at=now_ts,
                    player_name=sid,
                )
            )
        await _collect(engine.cmd_off(ev_a))
        assert engine.store.get_group_settings("aiocqhttp:10086")["monitor_enabled"] is False
        assert engine.store.get_group_settings("napcat:10086")["monitor_enabled"] is True
        assert sid_a not in engine.store.load_active_sessions()
        assert sid_b in engine.store.load_active_sessions()

        # In-flight lock-free poll race: commit_poll_batch MUST NOT resurrect sid_a
        engine.store.commit_poll_batch(
            profile_updates=[],
            upsert_sessions=[
                ActiveSession(
                    steam_id=sid_a,
                    game_id="570",
                    game_name="Dota 2",
                    started_at=now_ts - 600.0,
                    state="playing",
                    last_seen_at=now_ts + 30.0,
                    player_name=sid_a,
                ),
                ActiveSession(
                    steam_id=sid_b,
                    game_id="570",
                    game_name="Dota 2",
                    started_at=now_ts - 600.0,
                    state="playing",
                    last_seen_at=now_ts + 30.0,
                    player_name=sid_b,
                ),
            ],
            delete_session_sids=[],
            play_sessions=[],
            last_poll_at=now_ts + 30.0,
        )
        sessions_after = engine.store.load_active_sessions()
        assert sid_a not in sessions_after
        assert sid_b in sessions_after
    finally:
        await engine.terminate()


# ---------------------------------------------------------------------------
# P0-3: truth_game proactive timer broadcasts use context.send_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_3_truth_game_timer_broadcast_uses_context_send_message():
    sent: list[tuple[str, object]] = []

    class _CtxWithSend:
        async def send_message(self, umo: str, chain: object) -> None:
            sent.append((umo, chain))

    class _BrokenRawOneBot:
        async def send(self, event, message):
            raise AssertionError("Raw OneBot 2-arg bot.send must NOT be called")

    rooms = RoomManager(
        {"waiting_timeout_seconds": 0.03, "_waiting_check_interval": 0.01},
        context=_CtxWithSend(),
    )
    ev = SimpleNamespace(
        unified_msg_origin="aiocqhttp:GroupMessage:8888",
        get_group_id=lambda: "8888",
        get_sender_id=lambda: "1001",
        get_sender_name=lambda: "甲",
        is_admin=lambda: False,
        bot=_BrokenRawOneBot(),
    )
    try:
        reply = await cmd_create(rooms, ev)
        assert "已创建" in reply.text()
        await asyncio.wait_for(rooms._waiting_tasks[ev.unified_msg_origin], timeout=1.0)
        assert len(sent) == 1
        assert sent[0][0] == "aiocqhttp:GroupMessage:8888"
        payload = sent[0][1]
        rendered = (
            payload.get_plain_text()
            if hasattr(payload, "get_plain_text")
            else str(payload)
        )
        assert "自动关闭" in rendered
    finally:
        await rooms.terminate()


# ---------------------------------------------------------------------------
# P1-4: ActivityStore list_outcomes / get_outcome top-level counts
# ---------------------------------------------------------------------------


def test_p1_4_activity_store_outcomes_expose_top_level_counts(tmp_path: pathlib.Path):
    store = ActivityStore(tmp_path / "games.db")
    outcome, created = store.record_minigame_outcome_atomic(
        umo="aiocqhttp:GroupMessage:5555",
        group_id="5555",
        game_key="truth_game",
        title="真心话测试局",
        rounds=3,
        participants=[
            {"uid": "u1", "name": "甲"},
            {"uid": "u2", "name": "乙"},
            {"uid": "u3", "name": "丙"},
        ],
        summary="完赛测试",
        extra_contributions=[
            {"uid": "u1", "name": "甲", "content": "第一轮"},
            {"uid": "u2", "name": "乙", "content": "第二轮"},
        ],
        dedup_key="batch4-test-1",
    )
    assert created is True
    assert outcome["participant_count"] == 3
    assert outcome["contribution_count"] == 2

    fetched = store.get_outcome(outcome["activity_id"])
    assert fetched is not None
    assert fetched["participant_count"] == 3
    assert fetched["contribution_count"] == 2

    listed = store.list_outcomes(umo="aiocqhttp:GroupMessage:5555")
    assert len(listed) == 1
    assert listed[0]["participant_count"] == 3
    assert listed[0]["contribution_count"] == 2


# ---------------------------------------------------------------------------
# P1-5: _extract_tail prefix anchoring + rollpig case-insensitive dedup
# ---------------------------------------------------------------------------


def test_p1_5_extract_tail_anchored_at_command_prefix_only():
    ev1 = SimpleNamespace(message_str="/开黑 一起约局打本")
    assert DynamicsGamesPlugin._extract_tail(ev1, ("游戏约局", "约局", "开黑")) == "一起约局打本"

    ev2 = SimpleNamespace(message_str="/活动 new 创建新活动 明天八点")
    assert (
        DynamicsGamesPlugin._extract_tail(ev2, ("发起", "create", "new", "创建", "新建"))
        == "创建新活动 明天八点"
    )

    ev3 = SimpleNamespace(message_str="@bot /steam 绑定 76561198000000001 bind_nick")
    assert (
        DynamicsGamesPlugin._extract_tail(ev3, ("bind", "绑定"))
        == "76561198000000001 bind_nick"
    )

    # Alias appearing ONLY in message body must NOT be matched
    ev4 = SimpleNamespace(message_str="/unknown_cmd 约局 123")
    assert DynamicsGamesPlugin._extract_tail(ev4, ("约局",), fallback="fb") == "fb"


@pytest.mark.asyncio
async def test_p1_5_rollpig_wake_free_dedup_is_case_insensitive():
    plugin = RollPigPlugin.__new__(RollPigPlugin)
    plugin.settings = SimpleNamespace(wake_free_trigger=True)
    drawn: list[str] = []

    async def fake_draw(ev):
        drawn.append(ev.get_message_str())

    plugin._draw_and_send = fake_draw  # type: ignore[method-assign]

    for raw in ("RollPig", "PIG", "/RollPig", "rollpig"):
        ev = SimpleNamespace(
            get_message_str=lambda s=raw: s,
            is_at_or_wake_command=True,
            is_private_chat=lambda: False,
        )
        await plugin.roll_pig_wake_free(ev)
    assert drawn == []

    # Non-wake group message still triggers wake-free draw
    ev_free = SimpleNamespace(
        get_message_str=lambda: "今天想抽RollPig",
        is_at_or_wake_command=False,
        is_private_chat=lambda: False,
    )
    await plugin.roll_pig_wake_free(ev_free)
    assert drawn == ["今天想抽RollPig"]


# ---------------------------------------------------------------------------
# P2 & Packaging hygiene
# ---------------------------------------------------------------------------


def test_p2_schema_what_to_eat_frontend_and_packaging_hygiene(tmp_path: pathlib.Path):
    # 1) sgdb_api_base hint includes /api/v2
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    sgdb_hint = schema["steam_monitor"]["items"]["sgdb_api_base"]["hint"]
    assert "/api/v2" in sgdb_hint

    # 2) what_to_eat preserves explicit empty mcp_server_name for auto-scan
    wte_empty = WhatToEatPlugin(
        SimpleNamespace(), {"mcp_server_name": "", "mcp_enabled": False}
    )
    assert wte_empty.food_api.server_name == ""
    wte_default = WhatToEatPlugin(SimpleNamespace(), {"mcp_enabled": False})
    assert wte_default.food_api.server_name == "cook"

    # 3) games frontend fixes
    app_js = (PLUGIN_ROOT / "pages" / "games" / "app.js").read_text(encoding="utf-8")
    assert "mem.detail || mem.reason" in app_js
    theme_js = (PLUGIN_ROOT / "pages" / "games" / "theme.js").read_text(encoding="utf-8")
    assert "getElementById('theme-label').textContent" not in theme_js
    assert 'getElementById("theme-label").textContent' not in theme_js
    assert "getElementById('theme-toggle').onclick" not in theme_js
    assert 'getElementById("theme-toggle").onclick' not in theme_js
    assert "if (standardLabel)" in theme_js or "if (label)" in theme_js
    assert "if (standardToggle)" in theme_js or "if (toggle)" in theme_js

    # 4) package_release install ZIP: single top-level dir & excludes dev files
    records = package_release.build(PLUGIN_ROOT, tmp_path / "dist")
    install_rec = next(r for r in records if r["kind"] == "install")
    zip_path = tmp_path / "dist" / install_rec["file"]
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert names
    top_dirs = {n.split("/", 1)[0] for n in names}
    assert top_dirs == {"astrbot_plugin_dynamics_games"}
    forbidden_parts = {".github", "tests", "tools", "docs"}
    forbidden_names = {"package-lock.json", "build_release.py", ".gitignore"}
    for n in names:
        parts = n.split("/")[1:]
        assert not (set(parts) & forbidden_parts), f"Dev dir leaked into install ZIP: {n}"
        assert parts[-1] not in forbidden_names, f"Dev file leaked into install ZIP: {n}"
