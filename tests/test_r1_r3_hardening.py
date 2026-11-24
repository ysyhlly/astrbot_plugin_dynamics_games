"""Focused unit tests for R1 (P0) and R3 (P1) hardening across dynamics_games."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games import series_panels  # noqa: E402
from astrbot_plugin_dynamics_games.engines.rollpig.main import (  # noqa: E402
    RollPigPlugin,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.main import (  # noqa: E402
    DEFAULT_STEAM_MONITOR_CONFIG,
    MAX_SEND_QUEUE_SIZE,
    SteamMonitorEngine,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.price_service import (  # noqa: E402
    MAX_PENDING_SELECTIONS,
    PriceService,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.renderer import (  # noqa: E402
    SteamCardRenderer,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.state_machine import (  # noqa: E402
    SessionEvent,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.steam_client import (  # noqa: E402
    MAX_COVER_CACHE_SIZE,
    MAX_GAME_NAME_CACHE_SIZE,
    MAX_NO_ACHIEVEMENT_APPS,
    SteamClient,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.food_api import (  # noqa: E402
    FoodApiClient,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.main import (  # noqa: E402
    WhatToEatPlugin,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.persona_llm import (  # noqa: E402
    PersonaLLMResponder,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.taste_memory import (  # noqa: E402
    TasteMemoryStore,
)


class _FakeContext:
    def __init__(self, *, owner: Any = None, provider: Any = None, budget: Any = None) -> None:
        self.owner = owner
        self._provider = provider
        self._chat_dynamics_provider_budget = budget
        self.sent_messages: list[tuple[str, Any]] = []

    def get_config(self) -> dict[str, Any]:
        return {}

    def get_using_provider(self, umo: str | None = None) -> Any:
        _ = umo
        return self._provider

    async def send_message(self, umo: str, payload: Any) -> None:
        self.sent_messages.append((umo, payload))


async def _make_steam_engine(
    tmp_path: pathlib.Path,
    *,
    config: dict[str, Any] | None = None,
    owner: Any = None,
    provider: Any = None,
    budget: Any = None,
    html_render_fn: Any = None,
) -> tuple[SteamMonitorEngine, _FakeContext]:
    ctx = _FakeContext(owner=owner, provider=provider, budget=budget)
    cfg = {"steam_api_key": "TEST_KEY", **(config or {})}
    eng = SteamMonitorEngine(
        ctx,
        cfg,
        data_dir=tmp_path,
        start_background_tasks=False,
        html_render_fn=html_render_fn,
    )
    await eng.initialize()
    assert eng.store is not None
    eng.store.set_kv("legacy_migrated_v1", "1")
    return eng, ctx


# ---------------------------------------------------------------------------
# 1. Steam Monitor LLM routing via series_llm_call + asyncio.timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam_translate_routes_through_series_llm_call_and_enforces_timeout(
    tmp_path: pathlib.Path,
):
    budget_calls: list[tuple[str, str, str]] = []

    class FakeBudget:
        supports_series_telemetry = True

        async def run(self, provider_id, purpose, invoke, *, plugin_id="", acquire_timeout=None):
            _ = acquire_timeout
            budget_calls.append((provider_id, purpose, plugin_id))
            return await invoke()

    class FastProvider:
        id = "fast-llm"

        async def text_chat(self, **kwargs):
            _ = kwargs
            return SimpleNamespace(completion_text="<think>hmm</think>Elden Ring")

    eng, _ = await _make_steam_engine(
        tmp_path,
        provider=FastProvider(),
        budget=FakeBudget(),
        config={"translate_timeout_seconds": 2.0},
    )
    try:
        translated = await eng._translate_game_query("艾尔登法环")
        assert translated == "Elden Ring"
        assert budget_calls == [("fast-llm", "steam_translate", "astrbot_plugin_dynamics_games")]
    finally:
        await eng.terminate()

    # Slow provider must time out and fall back to original query
    class HangingProvider:
        id = "slow-llm"

        async def text_chat(self, **kwargs):
            _ = kwargs
            await asyncio.sleep(5.0)
            return SimpleNamespace(completion_text="Never Returned")

    eng_slow, _ = await _make_steam_engine(
        tmp_path / "slow",
        provider=HangingProvider(),
        budget=FakeBudget(),
        config={"translate_timeout_seconds": 1.0},
    )
    try:
        t0 = time.monotonic()
        fallback = await eng_slow._translate_game_query("黑神话悟空")
        elapsed = time.monotonic() - t0
        assert fallback == "黑神话悟空"
        assert elapsed < 3.0
    finally:
        await eng_slow.terminate()

    # Busy budget must gracefully fall back to original query
    class BusyBudget:
        supports_series_telemetry = True

        async def run(self, provider_id, purpose, invoke, **_kwargs):
            _ = invoke
            raise series_panels.ProviderBudgetBusyError(
                "busy", provider=provider_id, purpose=purpose, reason="circuit_open"
            )

    eng_busy, _ = await _make_steam_engine(
        tmp_path / "busy",
        provider=FastProvider(),
        budget=BusyBudget(),
    )
    try:
        assert await eng_busy._translate_game_query("博德之门3") == "博德之门3"
    finally:
        await eng_busy.terminate()


# ---------------------------------------------------------------------------
# 2. Steam Monitor proactive broadcast gate & series timezone guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam_proactive_broadcast_and_daily_rank_respect_owner_gate_and_timezone(
    tmp_path: pathlib.Path, monkeypatch
):
    blocked_groups = {"group_excluded"}
    quiet_flag = {"quiet": False}
    gate_calls: list[tuple[str, str]] = []

    class FakeOwner:
        def module_enabled(self, key: str) -> bool:
            return key == "steam_monitor"

        def _can_proactive_broadcast(self, *, umo: str = "", group_id: str = "") -> bool:
            gate_calls.append((umo, group_id))
            if quiet_flag["quiet"]:
                return False
            if group_id in blocked_groups:
                return False
            return True

    # Configure series timezone to UTC+9 (Tokyo)
    tz_plus9 = timezone(timedelta(hours=9))
    monkeypatch.setattr(series_panels, "resolve_series_timezone", lambda _ctx: tz_plus9)

    eng, ctx = await _make_steam_engine(
        tmp_path,
        owner=FakeOwner(),
        config={"rank_push_hour": 8, "rank_push_minute": 30},
    )
    try:
        assert eng.store is not None
        assert eng._series_tz() == tz_plus9

        eng.store.add_group_player("group_ok", "76561198000000101")
        eng.store.add_group_player("group_excluded", "76561198000000101")
        eng.store.upsert_group_settings(
            "group_ok",
            notify_session="aiocqhttp:GroupMessage:group_ok",
            rank_push_enabled=True,
        )
        eng.store.upsert_group_settings(
            "group_excluded",
            notify_session="aiocqhttp:GroupMessage:group_excluded",
            rank_push_enabled=True,
        )

        # Broadcast to both groups -> only group_ok receives it
        await eng._broadcast_to_groups(
            ["group_ok", "group_excluded"],
            image_ref=None,
            text="start",
            fallback_text="start fallback",
        )
        assert [u for u, _ in ctx.sent_messages] == ["aiocqhttp:GroupMessage:group_ok"]

        # Quiet hours -> zero deliveries for broadcast AND daily rank push deferred without marking pushed
        ctx.sent_messages.clear()
        quiet_flag["quiet"] = True
        await eng._broadcast_to_groups(
            ["group_ok"],
            image_ref=None,
            text="quiet msg",
            fallback_text="quiet fallback",
        )
        assert ctx.sent_messages == []

        y_start = datetime(2026, 11, 22, 19, 0, 0, tzinfo=tz_plus9).timestamp()
        y_end = datetime(2026, 11, 22, 20, 0, 0, tzinfo=tz_plus9).timestamp()
        eng.store.insert_play_session("76561198000000101", "570", "Dota 2", y_start, y_end)

        now_after_sched = datetime(2026, 11, 23, 9, 0, 0, tzinfo=tz_plus9)
        res_quiet = await eng.check_daily_rank_push(now_dt=now_after_sched)
        assert res_quiet["pushed"] is False
        assert res_quiet["reason"] == "proactive_blocked"
        assert eng.store.get_kv("last_rank_push_date", "") == ""

        # After quiet hours lift: group_ok receives daily rank push, group_excluded skipped
        quiet_flag["quiet"] = False
        res_ok = await eng.check_daily_rank_push(now_dt=now_after_sched)
        assert res_ok["pushed"] is True
        assert res_ok["pushed_groups"] == ["group_ok"]
        assert res_ok["skipped_blocked_groups"] == ["group_excluded"]
        assert len(ctx.sent_messages) == 1
        assert ctx.sent_messages[0][0] == "aiocqhttp:GroupMessage:group_ok"
    finally:
        await eng.terminate()


# ---------------------------------------------------------------------------
# 3. Temp steam_card_*.png cleanup (proactive multi-group + startup + command)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam_temp_card_cleanup_after_send_and_on_startup(tmp_path: pathlib.Path):
    # A) Stale temp file cleanup
    stale_dir = tmp_path / "temp_cards"
    stale_dir.mkdir()
    old_file = stale_dir / "steam_card_old_123.png"
    fresh_file = stale_dir / "steam_card_fresh_456.png"
    other_file = stale_dir / "other_image.png"
    old_file.write_bytes(b"old")
    fresh_file.write_bytes(b"fresh")
    other_file.write_bytes(b"other")
    old_mtime = time.time() - 7200.0
    os.utime(old_file, (old_mtime, old_mtime))

    removed = SteamCardRenderer.cleanup_stale_temp_files(
        max_age_seconds=3600.0, temp_dir=stale_dir
    )
    assert removed == 1
    assert not old_file.exists()
    assert fresh_file.exists()
    assert other_file.exists()

    # B) Multi-group proactive broadcast keeps file alive until ALL groups sent, then deletes it
    seen_exists_during_send: list[bool] = []
    png_bytes = b"\x89PNG\r\n\x1a\nfake-card"

    async def render_bytes(*_args, **_kwargs):
        return png_bytes

    eng, ctx = await _make_steam_engine(tmp_path / "eng", html_render_fn=render_bytes)
    try:
        assert eng.store is not None and eng.renderer is not None
        eng.store.upsert_group_settings("g1", notify_session="aiocqhttp:GroupMessage:g1")
        eng.store.upsert_group_settings("g2", notify_session="aiocqhttp:GroupMessage:g2")

        orig_send = ctx.send_message

        async def spy_send(umo: str, payload: Any) -> None:
            # Inspect image path existence at actual send time (real AstrBot Image sets file:/// URI)
            chain = getattr(payload, "chain", [])
            for comp in chain:
                fpath = str(getattr(comp, "file", "") or "")
                if fpath:
                    if fpath.startswith("file:///"):
                        fpath = fpath[7:] if os.name != "nt" else fpath[8:]
                    elif fpath.startswith("file://"):
                        fpath = fpath[7:]
                    seen_exists_during_send.append(pathlib.Path(fpath).exists())
            await orig_send(umo, payload)

        ctx.send_message = spy_send  # type: ignore[method-assign]

        img_ref, fb = await eng.renderer.render_game_start(
            {"player_name": "Alice", "game_name": "Hades", "appid": "1145360"}
        )
        assert img_ref is not None
        assert pathlib.Path(img_ref).exists()
        assert pathlib.Path(img_ref).name.startswith("steam_card_")

        await eng._broadcast_to_groups(
            ["g1", "g2"], image_ref=img_ref, text="start", fallback_text=fb
        )
        assert seen_exists_during_send == [True, True]
        assert not pathlib.Path(img_ref).exists()
    finally:
        await eng.terminate()


# ---------------------------------------------------------------------------
# 4. Pooled httpx.AsyncClient reuse & close on terminate + cache/queue caps + cover TTL default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam_client_pooled_httpx_reuse_close_caps_and_cover_ttl(
    tmp_path: pathlib.Path, monkeypatch
):
    created_clients: list[Any] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.is_closed = False
            self.calls: list[str] = []
            created_clients.append(self)

        async def get(self, url: str, **kwargs):
            _ = kwargs
            self.calls.append(url)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"response": {"result": 1, "player_count": 42}},
                raise_for_status=lambda: None,
            )

        async def aclose(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    cfg: dict[str, Any] = {"steam_api_key": "K"}
    eng, _ = await _make_steam_engine(tmp_path, config=cfg)
    assert eng.steam_client is not None
    # Verify default cover TTL alignment across schema, DEFAULT_STEAM_MONITOR_CONFIG, and SteamClient
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    schema_default = schema["steam_monitor"]["items"]["cache_cover_hours"]["default"]
    assert schema_default == 168
    assert DEFAULT_STEAM_MONITOR_CONFIG["cache_cover_hours"] == 168
    assert eng.steam_client.cache_cover_hours == 168.0

    # Multiple requests reuse ONE pooled client
    c1 = await eng.steam_client.get_game_online_count("570")
    c2 = await eng.steam_client.get_game_online_count("730")
    assert (c1, c2) == (42, 42)
    assert len(created_clients) == 1
    assert len(created_clients[0].calls) == 2

    # Terminate cleanly closes pooled client
    await eng.terminate()
    assert created_clients[0].is_closed is True


@pytest.mark.asyncio
async def test_steam_cache_caps_and_send_queue_cap(tmp_path: pathlib.Path):
    client = SteamClient(config_getter=dict, data_dir=tmp_path)
    for i in range(MAX_GAME_NAME_CACHE_SIZE + 25):
        client._cache_set(
            client._game_name_cache, str(i), (f"zh{i}", f"en{i}", 1.0), MAX_GAME_NAME_CACHE_SIZE
        )
    assert len(client._game_name_cache) == MAX_GAME_NAME_CACHE_SIZE

    for i in range(MAX_COVER_CACHE_SIZE + 10):
        client._cache_set(
            client._cover_cache, (str(i), False), f"http://c/{i}", MAX_COVER_CACHE_SIZE
        )
    assert len(client._cover_cache) == MAX_COVER_CACHE_SIZE

    for i in range(MAX_NO_ACHIEVEMENT_APPS + 15):
        client._mark_no_achievement_app(str(i))
    assert len(client._no_achievement_apps) == MAX_NO_ACHIEVEMENT_APPS

    # PriceService pending cap
    ps = PriceService(client, config_getter=dict)
    ps._search_steam_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"appid": "1", "title": "Game A", "en_title": "Game A", "type": "app"},
            {"appid": "2", "title": "Game B", "en_title": "Game B", "type": "app"},
        ]
    )
    for i in range(MAX_PENDING_SELECTIONS + 10):
        await ps.search_candidates("Game", session_key=f"sess:{i}")
    assert len(ps._pending) == MAX_PENDING_SELECTIONS

    # Send queue cap: QueueFull drops cleanly without blocking and deletes temp card
    eng, _ = await _make_steam_engine(tmp_path / "q")
    try:
        assert eng._send_queue is not None
        assert eng._send_queue.maxsize == MAX_SEND_QUEUE_SIZE
        eng._send_queue = asyncio.Queue(maxsize=1)

        ok1 = await eng._deliver_notification(
            group_id="g1",
            session_umo="aiocqhttp:GroupMessage:g1",
            image_ref=None,
            text="first",
            fallback_text="first",
            immediate=False,
        )
        assert ok1 is True

        # Second enqueue overflows and must drop + clean up temp PNG
        tmp_png = pathlib.Path(tempfile.gettempdir()) / f"steam_card_overflow_{os.getpid()}.png"
        tmp_png.write_bytes(b"png")
        ok2 = await eng._deliver_notification(
            group_id="g2",
            session_umo="aiocqhttp:GroupMessage:g2",
            image_ref=str(tmp_png),
            text="second",
            fallback_text="second",
            immediate=False,
        )
        assert ok2 is False
        assert not tmp_png.exists()
    finally:
        await eng.terminate()


# ---------------------------------------------------------------------------
# 5. Parallel _handle_session_event & batched async SQLite offloading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_session_event_fetches_in_parallel(tmp_path: pathlib.Path):
    eng, ctx = await _make_steam_engine(tmp_path, config={"notify_send_image": True})
    try:
        assert eng.store is not None and eng.steam_client is not None
        eng.store.add_group_player("g1", "76561198000000101")
        eng.store.upsert_group_settings("g1", notify_session="aiocqhttp:GroupMessage:g1")

        in_flight = 0
        max_concurrent = 0

        async def slow_cover(_gid: str) -> str:
            nonlocal in_flight, max_concurrent
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            await asyncio.sleep(0.08)
            in_flight -= 1
            return "https://example.test/cover.jpg"

        async def slow_online(_gid: str) -> int:
            nonlocal in_flight, max_concurrent
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            await asyncio.sleep(0.08)
            in_flight -= 1
            return 1234

        async def slow_frame(_sid: str) -> str:
            nonlocal in_flight, max_concurrent
            in_flight += 1
            max_concurrent = max(max_concurrent, in_flight)
            await asyncio.sleep(0.08)
            in_flight -= 1
            return "https://example.test/frame.png"

        eng.steam_client.get_game_names = AsyncMock(return_value=("刀塔2", "Dota 2"))  # type: ignore[method-assign]
        eng.steam_client.get_game_cover_url = slow_cover  # type: ignore[method-assign]
        eng.steam_client.get_game_online_count = slow_online  # type: ignore[method-assign]
        eng.steam_client.fetch_avatar_frame_url = slow_frame  # type: ignore[method-assign]

        ev = SessionEvent(
            kind="started",
            steam_id="76561198000000101",
            game_id="570",
            game_name="Dota 2",
            player_name="PlayerOne",
            avatar_url="",
            started_at=time.time(),
        )
        await eng._handle_session_event(
            ev, filter_mode="全部游戏", filter_ids="", ach_global=False
        )
        assert max_concurrent == 3
        assert len(ctx.sent_messages) == 1
    finally:
        await eng.terminate()


@pytest.mark.asyncio
async def test_poll_once_batches_sqlite_writes_off_event_loop(tmp_path: pathlib.Path):
    eng, _ = await _make_steam_engine(tmp_path)
    try:
        assert eng.store is not None and eng.steam_client is not None
        sids = ["76561198000000101", "76561198000000102", "76561198000000103"]
        for sid in sids:
            eng.store.add_group_player("g1", sid)
        eng.store.upsert_group_settings("g1", notify_session="aiocqhttp:GroupMessage:g1")
        eng._baseline_synced = True

        eng.steam_client.fetch_player_statuses_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=(
                {
                    sids[0]: {"name": "P1", "avatarfull": "a1", "gameid": "570", "gameextrainfo": "Dota 2"},
                    sids[1]: {"name": "P2", "avatarfull": "a2", "gameid": None, "gameextrainfo": None},
                    sids[2]: {"name": "P3", "avatarfull": "a3", "gameid": "730", "gameextrainfo": "CS2"},
                },
                set(),
            )
        )
        eng.steam_client.get_game_names = AsyncMock(side_effect=lambda gid, fb="": (fb or gid, fb or gid))  # type: ignore[method-assign]
        eng.steam_client.get_game_cover_url = AsyncMock(return_value="http://c")  # type: ignore[method-assign]
        eng.steam_client.get_game_online_count = AsyncMock(return_value=10)  # type: ignore[method-assign]
        eng.steam_client.fetch_avatar_frame_url = AsyncMock(return_value=None)  # type: ignore[method-assign]

        main_thread_id = threading.get_ident()
        batch_call_threads: list[int] = []
        orig_commit = eng.store.commit_poll_batch

        def tracked_commit(**kwargs):
            batch_call_threads.append(threading.get_ident())
            return orig_commit(**kwargs)

        eng.store.commit_poll_batch = tracked_commit  # type: ignore[method-assign]

        res = await eng.poll_once(now=1_700_000_000.0, silent=False)
        assert res["polled"] == 3
        assert len(batch_call_threads) == 1
        assert batch_call_threads[0] != main_thread_id
        assert len(eng.store.load_active_sessions()) == 2
    finally:
        await eng.terminate()


@pytest.mark.asyncio
async def test_steam_client_game_cache_sqlite_runs_in_worker_thread(tmp_path: pathlib.Path):
    main_tid = threading.get_ident()
    read_tids: list[int] = []

    class FakeStore:
        def get_game_cache(self, gid: str) -> dict[str, Any]:
            read_tids.append(threading.get_ident())
            return {
                "game_id": gid,
                "name_zh": "黑神话：悟空",
                "name_en": "Black Myth: Wukong",
                "updated_at": time.time(),
            }

    client = SteamClient(config_getter=dict, data_dir=tmp_path, store=FakeStore())
    zh, en = await client.get_game_names("2358720")
    assert (zh, en) == ("黑神话：悟空", "Black Myth: Wukong")
    assert len(read_tids) == 1
    assert read_tids[0] != main_tid


# ---------------------------------------------------------------------------
# 6. RollPig asyncio.to_thread offloading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollpig_draw_runs_off_event_loop():
    main_tid = threading.get_ident()
    draw_tids: list[int] = []

    plugin = object.__new__(RollPigPlugin)
    plugin.targeting = SimpleNamespace(
        resolve=lambda _ev: SimpleNamespace(rejected=False, user_id="u1", message="")
    )

    def fake_draw(uid: str):
        draw_tids.append(threading.get_ident())
        return SimpleNamespace(id="1", name=f"pig-{uid}")

    plugin.service = SimpleNamespace(draw=fake_draw)
    sent_cards: list[tuple[Any, str]] = []

    async def fake_send_card(_ev, pig, uid):
        sent_cards.append((pig, uid))

    plugin._send_card = fake_send_card  # type: ignore[method-assign]

    await plugin._draw_and_send(SimpleNamespace())
    assert len(draw_tids) == 1
    assert draw_tids[0] != main_tid
    assert len(sent_cards) == 1 and sent_cards[0][1] == "u1"


# ---------------------------------------------------------------------------
# 7. WhatToEat timeouts (PersonaLLMResponder & FoodApiClient) & taste_memory.flush()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_what_to_eat_persona_llm_timeout_falls_back_cleanly():
    class HangingContext:
        persona_manager = None

        async def get_current_chat_provider_id(self, _umo: str) -> str:
            return "p1"

        async def llm_generate(self, **_kwargs):
            await asyncio.sleep(5.0)
            return SimpleNamespace(completion_text="too late")

    responder = PersonaLLMResponder(
        HangingContext(),  # type: ignore[arg-type]
        enabled=True,
        timeout_seconds=0.08,
    )
    t0 = time.monotonic()
    res = await responder.generate_recommend(
        umo="aiocqhttp:GroupMessage:1",
        user_message="今天吃什么",
        food="牛肉面",
    )
    elapsed = time.monotonic() - t0
    assert res is None
    assert elapsed < 1.5


@pytest.mark.asyncio
async def test_what_to_eat_food_api_list_tools_timeout_does_not_hang(monkeypatch):
    class SlowToolsMCPClient:
        def __init__(self) -> None:
            self.tools: list[Any] = []
            self.connected = False
            self.cleaned = False

        async def connect_to_server(self, _cfg: dict[str, Any], _name: str) -> None:
            self.connected = True

        async def list_tools_and_save(self) -> None:
            await asyncio.sleep(5.0)

        async def cleanup(self) -> None:
            self.cleaned = True

    import types as _types

    agent_pkg = _types.ModuleType("astrbot.core.agent")
    mcp_mod = _types.ModuleType("astrbot.core.agent.mcp_client")
    mcp_mod.MCPClient = SlowToolsMCPClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "astrbot.core.agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.mcp_client", mcp_mod)

    api = FoodApiClient(
        context=None,
        enabled=True,
        remote_url="http://127.0.0.1:9999/sse",
    )
    api.timeout_seconds = 0.08

    t0 = time.monotonic()
    client = await api._ensure_remote_client()
    elapsed = time.monotonic() - t0
    assert client is not None
    assert client.connected is True
    assert elapsed < 1.5
    await api.close()
    assert client.cleaned is True


@pytest.mark.asyncio
async def test_what_to_eat_terminate_flushes_pending_taste_memory(tmp_path: pathlib.Path):
    store = TasteMemoryStore(enabled=True, data_dir=tmp_path)
    store.add_likes("u42", ["拉面"])
    # Write is deferred by 0.25s timer; file should not be flushed yet or dirty is True
    assert getattr(store, "_dirty", False) is True

    plugin = object.__new__(WhatToEatPlugin)
    plugin.taste_memory = store
    plugin.food_api = None

    await plugin.terminate()
    assert getattr(store, "_dirty", False) is False
    saved = tmp_path / "taste_memory.json"
    assert saved.is_file()
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert "拉面" in data["users"]["u42"]["likes"]
