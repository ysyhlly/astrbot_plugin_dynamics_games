"""Focused unit tests for 2GB RAM server performance optimizations in astrbot_plugin_dynamics_games."""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOS_PARENT = REPO_ROOT.parent
if str(REPOS_PARENT) not in sys.path:
    sys.path.insert(0, str(REPOS_PARENT))

from astrbot_plugin_dynamics_games import series_panels  # noqa: E402
from astrbot_plugin_dynamics_games.core.poster import prune_old_posters  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.engines.multimodal_party.main import (  # noqa: E402
    MultimodalPartyEngine,
    PictionaryRoom,
    TelephoneRoom,
)
from astrbot_plugin_dynamics_games.engines.rollpig import main as rollpig_main  # noqa: E402
from astrbot_plugin_dynamics_games.engines.steam_monitor.main import (  # noqa: E402
    SteamMonitorEngine,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.renderer import (  # noqa: E402
    SteamCardRenderer,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.steam_client import (  # noqa: E402
    SteamClient,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.store import (  # noqa: E402
    SteamMonitorStore,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.taste_memory import (  # noqa: E402
    TasteMemoryStore,
)


def _dummy_context(profile: str | None = None):
    cfg = {}
    if profile is not None:
        cfg["resource_profile"] = profile
    return SimpleNamespace(
        get_config=lambda: {"admins_id": []},
        get_registered_star=lambda _name: None,
        _chat_dynamics_shared_config_override=cfg,
    )


def test_multimodal_party_idle_ttl_and_lock_cleanup(tmp_path: Path) -> None:
    ctx = _dummy_context()
    eng = MultimodalPartyEngine(ctx, {}, output_dir=tmp_path)

    now = 10_000.0
    stale_tel = TelephoneRoom(
        room_key="g:old_tel",
        group_id="old_tel",
        host_id="u1",
        host_name="A",
        created_at=now - 2000.0,
        last_active_at=now - 1900.0,
    )
    fresh_tel = TelephoneRoom(
        room_key="g:fresh_tel",
        group_id="fresh_tel",
        host_id="u2",
        host_name="B",
        created_at=now - 300.0,
        last_active_at=now - 100.0,
    )
    stale_pic = PictionaryRoom(
        room_key="g:old_pic",
        group_id="old_pic",
        host_id="u3",
        host_name="C",
        created_at=now - 2500.0,
        last_active_at=now - 1801.0,
    )
    eng.telephone_rooms["g:old_tel"] = stale_tel
    eng.telephone_rooms["g:fresh_tel"] = fresh_tel
    eng.pictionary_rooms["g:old_pic"] = stale_pic
    eng._lock("tel:g:old_tel")
    eng._lock("tel:g:fresh_tel")
    eng._lock("pic:g:old_pic")
    eng._lock("tel:g:orphan")

    removed = eng.cleanup_idle_rooms(now=now)
    assert removed == 2
    assert "g:old_tel" not in eng.telephone_rooms
    assert "g:fresh_tel" in eng.telephone_rooms
    assert "g:old_pic" not in eng.pictionary_rooms
    assert "tel:g:old_tel" not in eng._locks
    assert "pic:g:old_pic" not in eng._locks
    assert "tel:g:orphan" not in eng._locks
    assert "tel:g:fresh_tel" in eng._locks


def test_rollpig_lazy_font_library_init(tmp_path: Path, monkeypatch) -> None:
    load_calls = {"count": 0}
    real_font_lib = rollpig_main.FontLibrary

    class SpyFontLibrary(real_font_lib):
        def __init__(self, font_dir, logger_obj):
            load_calls["count"] += 1
            super().__init__(font_dir, logger_obj)

    monkeypatch.setattr(rollpig_main, "FontLibrary", SpyFontLibrary)
    monkeypatch.setattr(
        rollpig_main, "resolve_rollpig_data_dir", lambda **kwargs: tmp_path
    )

    plugin = rollpig_main.RollPigPlugin(_dummy_context(), {})
    assert load_calls["count"] == 0
    assert plugin._fonts is None
    assert plugin._renderer is None

    # First render/renderer access triggers lazy FontLibrary load once
    _ = plugin.renderer
    assert load_calls["count"] == 1
    _ = plugin.renderer
    assert load_calls["count"] == 1


@pytest.mark.asyncio
async def test_taste_memory_lru_eviction_and_async_flush(tmp_path: Path) -> None:
    store = TasteMemoryStore(
        enabled=True,
        data_dir=tmp_path,
        max_users=3,
        save_debounce_seconds=60.0,
    )
    store.add_likes("u1", ["火锅"])
    store.add_likes("u2", ["拉面"])
    store.add_likes("u3", ["寿司"])
    # Touch u1 so u2 becomes the least-recently-used profile
    _ = store.get_profile("u1")
    store.add_likes("u4", ["烤肉"])

    assert len(store._profiles) == 3
    assert "u2" not in store._profiles
    assert {"u1", "u3", "u4"} == set(store._profiles.keys())

    await store.aflush()
    assert (tmp_path / "taste_memory.json").is_file()
    reloaded = TasteMemoryStore(enabled=True, data_dir=tmp_path, max_users=3)
    assert set(reloaded._profiles.keys()) == {"u1", "u3", "u4"}


@pytest.mark.asyncio
async def test_steam_monitor_skips_background_tasks_without_api_key_or_disabled(
    tmp_path: Path,
) -> None:
    # 1) Empty steam_api_key -> no background polling / daily rank tasks spawned
    eng_no_key = SteamMonitorEngine(
        _dummy_context(),
        {"steam_api_key": ""},
        data_dir=tmp_path / "no_key",
        start_background_tasks=True,
    )
    await eng_no_key.initialize()
    try:
        assert eng_no_key._poll_task is None
        assert eng_no_key._daily_push_task is None
        assert eng_no_key._send_worker_task is None
    finally:
        await eng_no_key.terminate()

    # 2) Disabled module -> no background tasks spawned even with API key
    eng_disabled = SteamMonitorEngine(
        _dummy_context(),
        {"steam_api_key": "TESTKEY123"},
        data_dir=tmp_path / "disabled",
        start_background_tasks=True,
    )
    eng_disabled.owner = SimpleNamespace(module_enabled=lambda _k: False)
    await eng_disabled.initialize()
    try:
        assert eng_disabled._poll_task is None
        assert eng_disabled._daily_push_task is None
        assert eng_disabled._send_worker_task is None
    finally:
        await eng_disabled.terminate()


@pytest.mark.asyncio
async def test_steam_monitor_daily_rank_push_caches_date_in_memory(
    tmp_path: Path, monkeypatch
) -> None:
    eng = SteamMonitorEngine(
        _dummy_context(),
        {"steam_api_key": "TESTKEY123", "rank_push_hour": 8, "rank_push_minute": 30},
        data_dir=tmp_path / "daily_cache",
        start_background_tasks=False,
    )
    await eng.initialize()
    try:
        assert eng.store is not None
        # Pre-seed yesterday's date as already pushed
        eng.store.set_kv("last_rank_push_date", "2026-11-26")

        kv_reads = {"count": 0}
        real_get_kv = eng.store.get_kv

        def spy_get_kv(key: str, default=None):
            if key == "last_rank_push_date":
                kv_reads["count"] += 1
            return real_get_kv(key, default)

        monkeypatch.setattr(eng.store, "get_kv", spy_get_kv)

        after_sched = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
        res1 = await eng.check_daily_rank_push(now_dt=after_sched)
        assert res1["reason"] == "already_pushed"
        assert kv_reads["count"] == 1

        # Subsequent ticks all day must hit the in-memory cache with zero SQLite reads
        for minute in (1, 2, 3, 15, 45):
            res_n = await eng.check_daily_rank_push(
                now_dt=after_sched.replace(minute=minute)
            )
            assert res_n["reason"] == "already_pushed"
        assert kv_reads["count"] == 1
    finally:
        await eng.terminate()


@pytest.mark.asyncio
async def test_steam_monitor_aggressive_profile_limits_and_renderer_watermark(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "950")

    eng = SteamMonitorEngine(
        _dummy_context(),
        {"steam_api_key": "TESTKEY", "notify_send_image": True, "poll_interval_seconds": 60},
        data_dir=tmp_path / "agg",
        start_background_tasks=False,
    )
    assert eng._should_send_notify_image() is False
    assert eng._effective_poll_interval() == 120

    # Explicit force flag overrides aggressive image fallback
    eng.config["notify_send_image_force"] = True
    assert eng._should_send_notify_image() is True

    # Renderer skips html_render cleanly when memory watermark is hard
    called = {"n": 0}

    async def fake_html_render(*args, **kwargs):
        called["n"] += 1
        return "should_not_be_called.png"

    render_ctx = _dummy_context()
    renderer = SteamCardRenderer(html_render_fn=fake_html_render, context=render_ctx)
    img_ref, fallback = await renderer.render_game_start(
        {"player_name": "Alice", "game_name": "Hades", "appid": "1145360"}
    )
    assert img_ref is None
    assert "Alice" in fallback
    assert called["n"] == 0

    # When RSS is healthy, html_render runs inside heavy_subprocess_slot
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "200")
    img_ref_ok, _ = await renderer.render_game_start(
        {"player_name": "Alice", "game_name": "Hades", "appid": "1145360"}
    )
    assert img_ref_ok == "should_not_be_called.png"
    assert called["n"] == 1
    gate_snap = series_panels.get_heavy_subprocess_snapshot(render_ctx)
    assert gate_snap["total_acquired"] >= 1


def test_steam_client_profile_cache_caps_and_httpx_limits(
    tmp_path: Path, monkeypatch
) -> None:
    client = SteamClient(
        config_getter=lambda: {},
        data_dir=tmp_path / "cache",
        context=_dummy_context(),
    )

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")
    assert client._cache_cap("achievement_schema") == 32
    assert client._cache_cap("game_name") == 256
    low_limits = client._httpx_kwargs()["limits"]
    assert low_limits.max_connections == 10
    assert low_limits.max_keepalive_connections == 5

    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory_aggressive")
    assert client._cache_cap("achievement_schema") == 16
    assert client._cache_cap("game_name") == 128
    agg_limits = client._httpx_kwargs()["limits"]
    assert agg_limits.max_connections == 6
    assert agg_limits.max_keepalive_connections == 3


def test_sqlite_pragmas_and_retention_pruning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASTRBOT_RESOURCE_PROFILE", "low_memory")

    # 1. SteamMonitorStore PRAGMAs + >365d play_sessions retention
    sm_store = SteamMonitorStore(tmp_path / "steam.db", context=_dummy_context())
    with sm_store.connection(write=False) as db:
        assert int(db.execute("PRAGMA temp_store").fetchone()[0]) == 1
        assert int(db.execute("PRAGMA mmap_size").fetchone()[0]) == 0
        assert int(db.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        assert int(db.execute("PRAGMA cache_size").fetchone()[0]) == -512

    now = 2_000_000_000.0
    sm_store.commit_poll_batch(
        play_sessions=[
            ("76561198000000001", "570", "Dota 2", now - 400 * 86400, now - 399 * 86400, 3600.0, "live"),
            ("76561198000000001", "570", "Dota 2", now - 10 * 86400, now - 9 * 86400, 3600.0, "live"),
        ],
        last_poll_at=now,
    )
    pruned_sm = sm_store.prune_old_play_sessions(max_age_days=365, now=now)
    assert pruned_sm["play_sessions"] == 1
    assert sm_store.stats_summary()["total_play_sessions"] == 1

    # 2. ActivityStore PRAGMAs + >180d completed activities retention
    act_store = ActivityStore(tmp_path / "games.db", context=_dummy_context())
    try:
        with act_store.connection(write=False) as db:
            assert int(db.execute("PRAGMA temp_store").fetchone()[0]) == 1
            assert int(db.execute("PRAGMA mmap_size").fetchone()[0]) == 0
            assert int(db.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
            assert int(db.execute("PRAGMA cache_size").fetchone()[0]) == -1024

        old_act = act_store.create_activity(
            umo="u:g:1",
            group_id="1",
            category="creation",
            title="旧活动",
            creator_uid="u1",
            creator_name="A",
            created_at=now - 220 * 86400,
        )
        fresh_act = act_store.create_activity(
            umo="u:g:1",
            group_id="1",
            category="creation",
            title="新活动",
            creator_uid="u1",
            creator_name="A",
            created_at=now - 10 * 86400,
        )
        with act_store.connection(write=True) as db:
            db.execute(
                "UPDATE activities SET status='completed', completed_at=?, updated_at=? WHERE id=?",
                (now - 200 * 86400, now - 200 * 86400, old_act["id"]),
            )
        pruned_act = act_store.prune_old_activities(max_age_days=180, now=now)
        assert pruned_act["activities"] == 1
        assert act_store.get_activity(old_act["id"]) is None
        assert act_store.get_activity(fresh_act["id"]) is not None
    finally:
        act_store.close()

    # 3. Posters directory cap (100 files)
    posters_dir = tmp_path / "posters"
    posters_dir.mkdir()
    base_mtime = time.time() - 500
    newest_file = None
    for idx in range(115):
        p = posters_dir / f"poster_{idx:03d}.png"
        p.write_bytes(b"PNG")
        os.utime(p, (base_mtime + idx, base_mtime + idx))
        newest_file = p
    removed_posters = prune_old_posters(posters_dir, max_files=100, keep=newest_file)
    assert removed_posters == 15
    remaining = sorted(p.name for p in posters_dir.glob("*.png"))
    assert len(remaining) == 100
    assert newest_file is not None and newest_file.name in remaining
    assert "poster_000.png" not in remaining
