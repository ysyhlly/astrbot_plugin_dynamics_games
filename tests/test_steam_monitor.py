"""Comprehensive unit & integration tests for the built-in Steam Monitor engine."""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games.core.models import ENGINE_CATALOG  # noqa: E402
from astrbot_plugin_dynamics_games.engines.steam_monitor import (  # noqa: E402
    ActiveSession,
    MIGRATION_KV_KEY,
    SteamMonitorStore,
    aggregate_playtime_window,
    apply_poll_observation,
    day_key_for_ts,
    end_game_tip,
    format_duration,
    migrate_from_legacy_plugin,
    natural_day_bounds,
    should_skip_game,
    split_interval_by_natural_day,
    tick_session_deadline,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.achievements import (  # noqa: E402
    AchievementService,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.main import (  # noqa: E402
    SteamMonitorEngine,
    parse_rank_period,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.price_service import (  # noqa: E402
    PriceService,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.renderer import (  # noqa: E402
    SteamCardRenderer,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.steam_client import (  # noqa: E402
    SteamClient,
)
from astrbot_plugin_dynamics_games.runtime import (  # noqa: E402
    ConfigSection,
    LEGACY_PLUGINS,
    LEGACY_PLUGIN_IDS,
)


# ---------------------------------------------------------------------------
# Shared test doubles
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(
        self,
        *,
        group_id: str = "10086",
        sender_id: str = "20001",
        sender_name: str = "群友甲",
        is_admin: bool = False,
        message_str: str = "",
        at_qqs: list[str] | None = None,
        umo: str | None = None,
    ) -> None:
        self._gid = group_id
        self._uid = sender_id
        self._name = sender_name
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
        at_nodes = [SimpleNamespace(qq=q) for q in (at_qqs or [])]
        for node in at_nodes:
            node.__class__ = type("At", (), {})
        self.message_obj = SimpleNamespace(message=at_nodes)
        self.stopped = False

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return self._name

    def get_self_id(self) -> str:
        return "999999"

    def is_admin(self) -> bool:
        return self._admin

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> dict[str, Any]:
        return {"kind": "plain", "text": text}

    def image_result(self, image_ref: str) -> dict[str, Any]:
        return {"kind": "image", "image_ref": image_ref}

    def chain_result(self, items: list[Any]) -> dict[str, Any]:
        return {"kind": "chain", "items": items}


class FakeContext:
    def __init__(self, *, module_enabled: bool = True) -> None:
        self.sent_messages: list[tuple[str, Any]] = []
        self._module_enabled = module_enabled
        self.owner = SimpleNamespace(
            name="astrbot_plugin_dynamics_games",
            author="ysyhlly",
            plugin_id="ysyhlly/astrbot_plugin_dynamics_games",
            module_enabled=lambda key: self._module_enabled,
        )

    async def send_message(self, umo: str, payload: Any) -> None:
        self.sent_messages.append((umo, payload))


async def _collect(agen) -> list[Any]:
    out = []
    async for item in agen:
        out.append(item)
    return out


def _reply_text(replies: list[Any]) -> str:
    parts: list[str] = []
    for r in replies:
        if isinstance(r, dict):
            if r.get("kind") == "plain":
                parts.append(str(r.get("text") or ""))
            elif r.get("kind") == "image":
                parts.append(f"[IMAGE:{r.get('image_ref')}]")
            elif r.get("kind") == "chain":
                parts.append(f"[CHAIN:{len(r.get('items') or [])}]")
        else:
            parts.append(str(r))
    return "\n".join(parts)


async def _make_engine(
    tmp_path: pathlib.Path,
    *,
    config: dict[str, Any] | None = None,
    html_render_fn=None,
    module_enabled: bool = True,
) -> tuple[SteamMonitorEngine, FakeContext]:
    ctx = FakeContext(module_enabled=module_enabled)
    cfg = {
        "steam_api_key": "TEST_STEAM_KEY",
        "exit_debounce_seconds": 180,
        "rank_push_hour": 8,
        "rank_push_minute": 30,
        "notify_send_image": True,
        "notify_send_text": True,
        "enable_achievement_poll": True,
    }
    if config:
        cfg.update(config)
    engine = SteamMonitorEngine(
        ctx,
        cfg,
        data_dir=tmp_path,
        start_background_tasks=False,
        html_render_fn=html_render_fn,
    )
    # Mark already migrated so tests don't accidentally scan host workspace data/
    store = SteamMonitorStore(tmp_path / "steam_monitor.db")
    store.set_kv(MIGRATION_KV_KEY, "1")
    await engine.initialize()
    return engine, ctx


# ===========================================================================
# 1. Polling State Machine & API Failure Freeze & Startup Silent Baseline
# ===========================================================================


def test_state_machine_start_switch_fluctuation_and_true_exit():
    sid = "76561198000000001"
    t0 = 1_700_000_000.0

    # Idle -> playing (start)
    sess, evs = apply_poll_observation(
        None,
        "730",
        now=t0,
        steam_id=sid,
        game_name="CS2",
        player_name="Alice",
        avatar_url="https://img/a.jpg",
        api_ok=True,
        debounce_seconds=180,
    )
    assert sess is not None
    assert sess.state == "playing"
    assert sess.game_id == "730"
    assert sess.started_at == t0
    assert len(evs) == 1 and evs[0].kind == "started"

    # Direct game switch: 730 -> 570 emits closed(730) + started(570)
    t1 = t0 + 600.0
    sess, evs = apply_poll_observation(
        sess,
        "570",
        now=t1,
        steam_id=sid,
        game_name="Dota 2",
        player_name="Alice",
        api_ok=True,
        debounce_seconds=180,
    )
    assert sess is not None
    assert sess.game_id == "570"
    assert sess.started_at == t1
    assert [e.kind for e in evs] == ["closed", "started"]
    assert evs[0].game_id == "730" and evs[0].duration_sec == pytest.approx(600.0)
    assert evs[1].game_id == "570" and evs[1].started_at == t1

    # Playing -> confirming_exit when observed_game_id=None
    t2 = t1 + 300.0
    sess, evs = apply_poll_observation(
        sess,
        None,
        now=t2,
        steam_id=sid,
        api_ok=True,
        debounce_seconds=180,
    )
    assert sess is not None
    assert sess.state == "confirming_exit"
    assert sess.exited_at == t2
    assert sess.exit_deadline == t2 + 180.0
    assert evs == []

    # Debounce fluctuation recovery within exit_deadline: resumes SAME session
    t3 = t2 + 120.0
    sess, evs = apply_poll_observation(
        sess,
        "570",
        now=t3,
        steam_id=sid,
        game_name="Dota 2",
        api_ok=True,
        debounce_seconds=180,
    )
    assert sess is not None
    assert sess.state == "playing"
    assert sess.started_at == t1  # original start preserved
    assert sess.exited_at is None and sess.exit_deadline is None
    assert len(evs) == 1 and evs[0].kind == "fluctuation"

    # Enter confirming_exit again, stay absent past deadline -> true exit
    t4 = t3 + 60.0
    sess, evs = apply_poll_observation(
        sess, None, now=t4, steam_id=sid, api_ok=True, debounce_seconds=180
    )
    assert sess is not None and sess.state == "confirming_exit"
    assert evs == []

    # Still within deadline -> stays confirming_exit
    sess, evs = apply_poll_observation(
        sess, None, now=t4 + 100.0, steam_id=sid, api_ok=True, debounce_seconds=180
    )
    assert sess is not None and sess.state == "confirming_exit"
    assert evs == []

    # Past deadline -> closes with ended_at == exited_at (t4)
    sess_after, evs = apply_poll_observation(
        sess, None, now=t4 + 180.0, steam_id=sid, api_ok=True, debounce_seconds=180
    )
    assert sess_after is None
    assert len(evs) == 1 and evs[0].kind == "closed"
    assert evs[0].ended_at == t4
    assert evs[0].duration_sec == pytest.approx(t4 - t1)


def test_state_machine_api_failure_freeze():
    sid = "76561198000000002"
    t0 = 1_700_000_000.0
    sess, _ = apply_poll_observation(
        None, "1245620", now=t0, steam_id=sid, game_name="Elden Ring", api_ok=True
    )
    assert sess is not None

    # Enter confirming_exit at t0 + 100 (deadline = t0 + 280)
    sess, _ = apply_poll_observation(
        sess, None, now=t0 + 100.0, steam_id=sid, api_ok=True, debounce_seconds=180
    )
    assert sess is not None and sess.state == "confirming_exit"
    original_deadline = sess.exit_deadline
    original_exited = sess.exited_at

    # API failure well past exit_deadline: MUST FREEZE state & deadline, emit NO events
    frozen, evs = apply_poll_observation(
        sess,
        None,
        now=t0 + 1000.0,
        steam_id=sid,
        api_ok=False,
        debounce_seconds=180,
    )
    assert frozen is not None
    assert frozen.state == "confirming_exit"
    assert frozen.exit_deadline == original_deadline
    assert frozen.exited_at == original_exited
    assert frozen.consecutive_failures == 1
    assert evs == []

    # tick_session_deadline MUST NOT close when consecutive_failures > 0
    still_frozen, tick_evs = tick_session_deadline(frozen, now=t0 + 5000.0)
    assert still_frozen is not None
    assert still_frozen.state == "confirming_exit"
    assert still_frozen.consecutive_failures == 1
    assert tick_evs == []

    # Playing session also freezes cleanly on api_ok=False
    playing = ActiveSession(
        steam_id=sid,
        game_id="1245620",
        game_name="Elden Ring",
        started_at=t0,
        state="playing",
        last_seen_at=t0,
    )
    frozen_play, evs_p = apply_poll_observation(
        playing, None, now=t0 + 600.0, steam_id=sid, api_ok=False
    )
    assert frozen_play is not None
    assert frozen_play.state == "playing"
    assert frozen_play.consecutive_failures == 1
    assert frozen_play.exited_at is None
    assert evs_p == []


def test_engine_startup_silent_baseline_and_api_failure_freeze(tmp_path: pathlib.Path):
    async def run():
        engine, ctx = await _make_engine(tmp_path, html_render_fn=None)
        assert engine.store is not None and engine.steam_client is not None
        sid = "76561198000000010"
        engine.store.add_group_player("10086", sid)
        engine.store.set_notify_session("10086", "aiocqhttp:GroupMessage:10086")
        # Stale session from before restart should be cleared silently on baseline
        engine.store.save_active_session(
            ActiveSession(
                steam_id="76561198999999999",
                game_id="999",
                game_name="StaleGame",
                started_at=1_700_000_000.0,
            )
        )

        engine.steam_client.get_game_names = AsyncMock(return_value=("反恐精英2", "CS2"))
        engine.steam_client.get_game_cover_url = AsyncMock(return_value="https://cdn/730.jpg")
        engine.steam_client.get_game_online_count = AsyncMock(return_value=123456)
        engine.steam_client.fetch_avatar_frame_url = AsyncMock(return_value=None)
        engine.steam_client.fetch_player_achievements = AsyncMock(return_value=set())

        # Poll 1 (startup): player already playing 730 -> SILENT baseline, 0 notifications
        t0 = 1_700_000_000.0
        engine.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=(
                {
                    sid: {
                        "steamid": sid,
                        "name": "PlayerOne",
                        "gameid": "730",
                        "gameextrainfo": "Counter-Strike 2",
                        "personastate": 1,
                        "avatarfull": "https://img/p1.jpg",
                    }
                },
                set(),
            )
        )
        res1 = await engine.poll_once(now=t0)
        assert res1["silent"] is True
        assert res1["events"] == 0
        assert len(ctx.sent_messages) == 0
        assert engine.store.get_active_session("76561198999999999") is None
        act = engine.store.get_active_session(sid)
        assert act is not None and act.game_id == "730" and act.state == "playing"

        # Poll 2: player stops playing -> enters confirming_exit (no notification yet)
        engine.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=(
                {
                    sid: {
                        "steamid": sid,
                        "name": "PlayerOne",
                        "gameid": None,
                        "gameextrainfo": None,
                        "personastate": 1,
                        "avatarfull": "https://img/p1.jpg",
                    }
                },
                set(),
            )
        )
        res2 = await engine.poll_once(now=t0 + 60.0)
        assert res2["silent"] is False
        assert res2["events"] == 0
        act2 = engine.store.get_active_session(sid)
        assert act2 is not None and act2.state == "confirming_exit"

        # Poll 3: API FAILS even 600s later -> MUST FREEZE confirming_exit, no closed event!
        engine.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=({}, {sid})
        )
        res3 = await engine.poll_once(now=t0 + 660.0)
        assert res3["failed"] == 1
        assert res3["events"] == 0
        assert len(ctx.sent_messages) == 0
        act3 = engine.store.get_active_session(sid)
        assert act3 is not None
        assert act3.state == "confirming_exit"
        assert act3.consecutive_failures == 1

        # Poll 4: API recovers and confirms still not playing -> closes & notifies
        engine.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=(
                {
                    sid: {
                        "steamid": sid,
                        "name": "PlayerOne",
                        "gameid": None,
                        "gameextrainfo": None,
                        "personastate": 1,
                        "avatarfull": "https://img/p1.jpg",
                    }
                },
                set(),
            )
        )
        res4 = await engine.poll_once(now=t0 + 720.0)
        assert res4["events"] == 1
        assert engine.store.get_active_session(sid) is None
        assert len(ctx.sent_messages) == 1
        closed_rows = engine.store.query_play_sessions_overlapping(t0 - 10, t0 + 1000)
        assert len(closed_rows) == 1
        assert closed_rows[0]["duration_sec"] == pytest.approx(60.0)

        await engine.terminate()

    asyncio.run(run())


def test_game_filter_modes_and_helpers():
    assert should_skip_game("全部游戏", "730,570", "730") is False
    assert should_skip_game("白名单", "730,570", "730") is False
    assert should_skip_game("白名单", "730,570", "440") is True
    assert should_skip_game("黑名单", "431960，730", "431960") is True
    assert should_skip_game("黑名单", "431960，730", "570") is False
    assert format_duration(45) == "45秒"
    assert format_duration(3660) == "1小时1分钟"
    assert isinstance(end_game_tip(120.0), str) and len(end_game_tip(120.0)) > 0


# ===========================================================================
# 2. Natural Day 00:00 Time-Slicing & Ranking
# ===========================================================================


def test_natural_day_cross_midnight_split_and_exact_float_aggregation():
    tz = timezone(timedelta(hours=8))
    # Session spanning 3 natural days:
    # 2026-11-20 23:00:00 -> 2026-11-22 01:30:30
    start_dt = datetime(2026, 11, 20, 23, 0, 0, tzinfo=tz)
    end_dt = datetime(2026, 11, 22, 1, 30, 30, tzinfo=tz)
    slices = split_interval_by_natural_day(start_dt.timestamp(), end_dt.timestamp(), tz=tz)
    assert len(slices) == 3
    assert slices[0] == ("2026-11-20", pytest.approx(3600.0))
    assert slices[1] == ("2026-11-21", pytest.approx(86400.0))
    assert slices[2] == ("2026-11-22", pytest.approx(5430.0))
    assert sum(sec for _, sec in slices) == pytest.approx(end_dt.timestamp() - start_dt.timestamp())
    assert day_key_for_ts(datetime(2026, 11, 21, 0, 0, 0, tzinfo=tz).timestamp(), tz=tz) == "2026-11-21"
    s0, e0 = natural_day_bounds("2026-11-21", tz=tz)
    assert e0 - s0 == pytest.approx(86400.0)

    # Active still-playing session started 2026-11-21 23:30:00, now is 2026-11-22 02:00:00
    now_dt = datetime(2026, 11, 22, 2, 0, 0, tzinfo=tz)
    active_sess = ActiveSession(
        steam_id="sid_a",
        game_id="100",
        game_name="Hades",
        started_at=datetime(2026, 11, 21, 23, 30, 0, tzinfo=tz).timestamp(),
        state="playing",
    )

    # Fractional-minute closed sessions: two 90s sessions on 2026-11-21 -> exact 180s = 3.0 min
    closed_sessions = [
        {
            "steam_id": "sid_b",
            "game_id": "200",
            "game_name": "Celeste",
            "started_at": datetime(2026, 11, 21, 10, 0, 0, tzinfo=tz).timestamp(),
            "ended_at": datetime(2026, 11, 21, 10, 1, 30, tzinfo=tz).timestamp(),
        },
        {
            "steam_id": "sid_b",
            "game_id": "200",
            "game_name": "Celeste",
            "started_at": datetime(2026, 11, 21, 11, 0, 0, tzinfo=tz).timestamp(),
            "ended_at": datetime(2026, 11, 21, 11, 1, 30, tzinfo=tz).timestamp(),
        },
    ]
    # Legacy daily row: sid_b also has 45.5 seconds on 2026-11-21 + out-of-window row on 2026-11-20
    legacy_rows = [
        {
            "day_key": "2026-11-21",
            "steam_id": "sid_b",
            "game_id": "200",
            "game_name": "Celeste",
            "seconds": 45.5,
        },
        {
            "day_key": "2026-11-20",
            "steam_id": "sid_b",
            "game_id": "200",
            "game_name": "Celeste",
            "seconds": 9999.0,
        },
    ]

    # Query yesterday (2026-11-21 only):
    # - sid_a active session MUST be clipped to window_end (2026-11-22 00:00) -> 1800s (30 min)
    # - sid_b has 90 + 90 + 45.5 = 225.5s (3.758333... min, zero int truncation!)
    ranked_yday = aggregate_playtime_window(
        closed_sessions,
        [active_sess],
        legacy_rows,
        start_day="2026-11-21",
        end_day="2026-11-21",
        now_ts=now_dt.timestamp(),
        target_sids={"sid_a", "sid_b"},
        tz=tz,
    )
    assert [r["sid"] for r in ranked_yday] == ["sid_a", "sid_b"]
    assert ranked_yday[0]["total_seconds"] == pytest.approx(1800.0)
    assert ranked_yday[0]["total_minutes"] == pytest.approx(30.0)
    assert ranked_yday[0]["top_game_id"] == "100"
    assert ranked_yday[1]["total_seconds"] == pytest.approx(225.5)
    assert ranked_yday[1]["total_minutes"] == pytest.approx(225.5 / 60.0)

    # Query today (2026-11-22): active session clipped to min(now, window_end) = now (02:00) -> 7200s
    ranked_today = aggregate_playtime_window(
        closed_sessions,
        [active_sess],
        legacy_rows,
        start_day="2026-11-22",
        end_day="2026-11-22",
        now_ts=now_dt.timestamp(),
        target_sids={"sid_a", "sid_b"},
        tz=tz,
    )
    assert len(ranked_today) == 1
    assert ranked_today[0]["sid"] == "sid_a"
    assert ranked_today[0]["total_seconds"] == pytest.approx(7200.0)


def test_parse_rank_period_date_labels():
    tz = timezone(timedelta(hours=8))
    now_dt = datetime(2026, 11, 23, 9, 15, 0, tzinfo=tz)

    s_y, e_y, lbl_y, date_y = parse_rank_period("yesterday", now_dt=now_dt)
    assert (s_y, e_y) == ("2026-11-22", "2026-11-22")
    assert lbl_y == "昨日"
    assert date_y == "2026/11/22"  # MUST be yesterday, NOT 2026/11/23!

    s_t, e_t, lbl_t, date_t = parse_rank_period("", now_dt=now_dt)
    assert (s_t, e_t) == ("2026-11-23", "2026-11-23")
    assert date_t == "2026/11/23"

    s_w, e_w, lbl_w, date_w = parse_rank_period("week", now_dt=now_dt)
    assert (s_w, e_w) == ("2026-11-17", "2026-11-23")
    assert date_w == "2026/11/17 ~ 2026/11/23"


# ===========================================================================
# 3. Daily Push Scheduler (Missed Minute, Restart Dedup, Skip Zero Playtime)
# ===========================================================================


def test_daily_push_scheduler_missed_minute_restart_dedup_and_empty_skip(
    tmp_path: pathlib.Path,
):
    async def run():
        local_tz = datetime.now().astimezone().tzinfo
        engine, ctx = await _make_engine(
            tmp_path,
            config={"rank_push_hour": 8, "rank_push_minute": 30},
            html_render_fn=None,
        )
        assert engine.store is not None
        # Group A has playtime yesterday (2026-11-22); Group B has ZERO playtime yesterday
        engine.store.add_group_player("group_a", "76561198000000101")
        engine.store.upsert_binding("76561198000000101", nickname="肝帝阿明")
        engine.store.upsert_group_settings(
            "group_a",
            notify_session="aiocqhttp:GroupMessage:group_a",
            rank_push_enabled=True,
            rank_push_global=False,
        )
        engine.store.add_group_player("group_b", "76561198000000102")
        engine.store.upsert_group_settings(
            "group_b",
            notify_session="aiocqhttp:GroupMessage:group_b",
            rank_push_enabled=True,
            rank_push_global=False,
        )

        y_start = datetime(2026, 11, 22, 20, 0, 0, tzinfo=local_tz).timestamp()
        y_end = datetime(2026, 11, 22, 22, 30, 0, tzinfo=local_tz).timestamp()
        engine.store.insert_play_session(
            "76561198000000101", "570", "Dota 2", y_start, y_end
        )

        # Before schedule (08:29): must NOT push
        before_dt = datetime(2026, 11, 23, 8, 29, 0, tzinfo=local_tz)
        r_before = await engine.check_daily_rank_push(now_dt=before_dt)
        assert r_before["pushed"] is False
        assert r_before["reason"] == "before_schedule"
        assert len(ctx.sent_messages) == 0

        # Missed exact 08:30 minute (checked at 09:17): MUST STILL FIRE!
        missed_dt = datetime(2026, 11, 23, 9, 17, 0, tzinfo=local_tz)
        r_fire = await engine.check_daily_rank_push(now_dt=missed_dt)
        assert r_fire["pushed"] is True
        assert r_fire["day_key"] == "2026-11-22"
        assert r_fire["date_label"] == "2026/11/22"
        assert r_fire["pushed_groups"] == ["group_a"]
        assert r_fire["skipped_empty_groups"] == ["group_b"]
        assert len(ctx.sent_messages) == 1
        sent_umo, sent_payload = ctx.sent_messages[0]
        assert sent_umo == "aiocqhttp:GroupMessage:group_a"
        assert "2026/11/22" in str(sent_payload)
        assert "2026/11/23" not in str(sent_payload)

        # Persisted in SQLite
        assert engine.store.get_kv("last_rank_push_date") == "2026-11-22"
        await engine.terminate()

        # Simulate bot restart (brand new engine reading same SQLite DB): MUST NOT double-push
        engine2, ctx2 = await _make_engine(
            tmp_path,
            config={"rank_push_hour": 8, "rank_push_minute": 30},
            html_render_fn=None,
        )
        r_after_restart = await engine2.check_daily_rank_push(
            now_dt=datetime(2026, 11, 23, 10, 0, 0, tzinfo=local_tz)
        )
        assert r_after_restart["pushed"] is False
        assert r_after_restart["reason"] == "already_pushed"
        assert len(ctx2.sent_messages) == 0
        await engine2.terminate()

    asyncio.run(run())


# ===========================================================================
# 4. Legacy Migration (Idempotent, Read-Only, Non-Destructive Config Backfill)
# ===========================================================================


def test_legacy_migration_idempotent_readonly_and_config_backfill(
    tmp_path: pathlib.Path,
):
    legacy_dir = tmp_path / "steam_status_monitor_V3"
    legacy_dir.mkdir()
    local_tz = datetime.now().astimezone().tzinfo
    s_ts = datetime(2026, 11, 20, 20, 0, 0, tzinfo=local_tz).timestamp()
    e_ts = datetime(2026, 11, 20, 21, 0, 0, tzinfo=local_tz).timestamp()

    legacy_files: dict[str, Any] = {
        "group_steam_ids.json": {
            "111001": ["76561198000000001", "76561198000000002"],
        },
        "bind_data.json": {
            "50001": {"sid": "76561198000000001", "nickname": "老王"},
            "__remark:76561198000000002": {
                "sid": "76561198000000002",
                "nickname": "阿花",
            },
        },
        "push_groups.json": {
            "76561198000000001": ["222002"],
        },
        "notify_sessions.json": {
            "111001": "aiocqhttp:GroupMessage:111001",
            "222002": "aiocqhttp:GroupMessage:222002",
        },
        "rank_push_groups.json": {
            "groups": ["111001"],
            "all": True,
        },
        "group_switches.json": {
            "monitor": {"111001": True, "222002": False},
            "achievement": {"111001": False},
        },
        "session_records.json": {
            "76561198000000001": [
                {
                    "gameid": "730",
                    "game_name": "CS2",
                    "start_time": s_ts,
                    "end_time": e_ts,
                }
            ]
        },
        "play_records.json": {
            "2026-11-20": {
                "76561198000000001": {
                    # 3600s already covered by session_records -> remainder 1800s in legacy_daily
                    "730": {"name": "CS2", "minutes": 90},
                    "570": {"name": "Dota 2", "minutes": 45},
                }
            }
        },
    }
    raw_snapshots: dict[str, bytes] = {}
    for fname, payload in legacy_files.items():
        p = legacy_dir / fname
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        p.write_bytes(raw)
        raw_snapshots[fname] = raw

    legacy_cfg_path = tmp_path / "steam_status_monitor_V3_config.json"
    legacy_cfg_bytes = json.dumps(
        {
            "steam_api_key": "LEGACY_STEAM_KEY",
            "itad_api_key": "LEGACY_ITAD_KEY",
            "sgdb_api_key": "LEGACY_SGDB_KEY",
            "enable_proxy": True,
            "proxy_url": "http://127.0.0.1:7890",
            "rank_push_hour": 9,
            "rank_push_minute": 15,
            "game_filter_mode": "黑名单",
            "game_filter_ids": "431960",
        }
    ).encode("utf-8")
    legacy_cfg_path.write_bytes(legacy_cfg_bytes)

    saved_calls: list[bool] = []

    class FakeRootCfg(dict):
        def save_config(self) -> None:
            saved_calls.append(True)

    # User ALREADY configured steam_api_key and rank_push_hour; others are empty/unset
    parent_cfg = FakeRootCfg(
        {
            "steam_monitor": {
                "steam_api_key": "USER_NEW_KEY",
                "itad_api_key": "",
                "rank_push_hour": 7,
            }
        }
    )
    section = ConfigSection(
        parent_cfg,
        "steam_monitor",
        defaults={"steam_api_key": "", "itad_api_key": "", "enable_proxy": False},
    )

    store = SteamMonitorStore(tmp_path / "steam_monitor.db")
    summary1 = migrate_from_legacy_plugin(
        store,
        section,
        legacy_data_dir=legacy_dir,
        legacy_config_path=legacy_cfg_path,
    )
    assert summary1["migrated"] is True
    assert summary1["already_migrated"] is False
    assert summary1["counts"]["group_players"] == 2
    assert summary1["counts"]["bindings"] == 2
    assert summary1["counts"]["push_routes"] == 1
    assert summary1["counts"]["notify_sessions"] == 2
    assert summary1["counts"]["rank_push_groups"] == 1
    assert summary1["counts"]["play_sessions"] == 1
    assert summary1["counts"]["legacy_daily_rows"] == 2

    # Verify imported SQLite data
    assert store.list_group_players("111001") == [
        "76561198000000001",
        "76561198000000002",
    ]
    assert store.resolve_display_name("76561198000000001") == "老王"
    assert store.resolve_display_name("76561198000000002") == "阿花"
    assert store.get_binding_by_qq("50001")["steam_id"] == "76561198000000001"
    assert store.get_notify_session("111001") == "aiocqhttp:GroupMessage:111001"
    gs1 = store.get_group_settings("111001")
    assert gs1["monitor_enabled"] is True
    assert gs1["achievement_enabled"] is False
    assert gs1["rank_push_enabled"] is True
    assert gs1["rank_push_global"] is True
    assert store.get_group_settings("222002")["monitor_enabled"] is False

    # Verify deduped play_sessions + legacy_daily_playtime aggregate to exact 90m CS2 + 45m Dota 2
    agg = aggregate_playtime_window(
        store.query_play_sessions_overlapping(s_ts - 86400, e_ts + 86400),
        [],
        store.query_legacy_daily_playtime("2026-11-20", "2026-11-20"),
        start_day="2026-11-20",
        end_day="2026-11-20",
        now_ts=e_ts + 1000,
        target_sids={"76561198000000001"},
        tz=local_tz,
    )
    assert len(agg) == 1
    assert agg[0]["total_minutes"] == pytest.approx(135.0)
    by_game = {g["gameid"]: g["minutes"] for g in agg[0]["games"]}
    assert by_game["730"] == pytest.approx(90.0)
    assert by_game["570"] == pytest.approx(45.0)

    # Verify config backfill: existing non-empty user values PRESERVED, empty fields BACKFILLED
    assert section["steam_api_key"] == "USER_NEW_KEY"
    assert section["rank_push_hour"] == 7
    assert section["itad_api_key"] == "LEGACY_ITAD_KEY"
    assert section["sgdb_api_key"] == "LEGACY_SGDB_KEY"
    assert section["enable_proxy"] is True
    assert section["proxy_url"] == "http://127.0.0.1:7890"
    assert section["rank_push_minute"] == 15
    assert section["game_filter_mode"] == "黑名单"
    assert len(saved_calls) == 1

    # Verify legacy files are strictly untouched (read-only)
    for fname, orig_bytes in raw_snapshots.items():
        assert (legacy_dir / fname).read_bytes() == orig_bytes
    assert legacy_cfg_path.read_bytes() == legacy_cfg_bytes

    # Verify idempotency: second call does nothing and creates zero duplicate rows
    summary2 = migrate_from_legacy_plugin(
        store,
        section,
        legacy_data_dir=legacy_dir,
        legacy_config_path=legacy_cfg_path,
    )
    assert summary2["migrated"] is False
    assert summary2["already_migrated"] is True
    assert len(saved_calls) == 1
    assert store.stats_summary()["total_play_sessions"] == 1


# ===========================================================================
# 5. Permissions, Commands, /steamwho, Price/PX/Game/Openbox & Selection
# ===========================================================================


def test_command_permissions_member_vs_admin_and_rs_recovery(tmp_path: pathlib.Path):
    async def run():
        engine, _ = await _make_engine(tmp_path, html_render_fn=None)
        assert engine.store is not None and engine.steam_client is not None
        sid_self = "76561198000000201"
        sid_other = "76561198000000202"

        ev_member = FakeEvent(group_id="10086", sender_id="30001", is_admin=False)
        ev_admin = FakeEvent(group_id="10086", sender_id="90001", is_admin=True)

        # Member: self-bind & self-addid & self-delid & unbind allowed
        r_bind = _reply_text(await _collect(engine.cmd_bind(ev_member, sid_self, "小蓝")))
        assert "✅" in r_bind and sid_self in r_bind
        assert sid_self in engine.store.list_group_players("10086")

        # Member: managing others via addid @other or comma-batch MUST be blocked
        r_add_other = _reply_text(
            await _collect(engine.cmd_addid(ev_member, f"{sid_other} @30002 别人"))
        )
        assert "权限不足" in r_add_other
        assert sid_other not in engine.store.list_group_players("10086")

        r_add_batch = _reply_text(
            await _collect(engine.cmd_addid(ev_member, f"{sid_self},{sid_other}"))
        )
        assert "权限不足" in r_add_batch

        # Admin adds sid_other
        r_admin_add = _reply_text(
            await _collect(engine.cmd_addid(ev_admin, f"{sid_other} @30002 小红"))
        )
        assert "✅" in r_admin_add
        assert sid_other in engine.store.list_group_players("10086")

        # Member CANNOT delete someone else's SteamID or cross-group
        r_del_other = _reply_text(await _collect(engine.cmd_delid(ev_member, sid_other)))
        assert "权限不足" in r_del_other
        assert sid_other in engine.store.list_group_players("10086")

        r_del_cross = _reply_text(
            await _collect(engine.cmd_delid(ev_member, sid_self, "99999"))
        )
        assert "权限不足" in r_del_cross

        # Member CAN delete own bound SteamID & unbind
        r_del_self = _reply_text(await _collect(engine.cmd_delid(ev_member, sid_self)))
        assert "✅" in r_del_self
        assert sid_self not in engine.store.list_group_players("10086")

        r_unbind = _reply_text(await _collect(engine.cmd_unbind(ev_member)))
        assert "✅" in r_unbind
        assert engine.store.get_binding_by_qq("30001") is None

        # Member CANNOT execute any admin-only commands
        admin_calls = [
            engine.cmd_on(ev_member),
            engine.cmd_off(ev_member),
            engine.cmd_achievement_on(ev_member),
            engine.cmd_achievement_off(ev_member),
            engine.cmd_rank_on(ev_member, ""),
            engine.cmd_push_group(ev_member, sid_other),
            engine.cmd_delpush_group(ev_member, sid_other),
            engine.cmd_clear_groupids(ev_member, ""),
            engine.cmd_clear_allids(ev_member),
            engine.cmd_alllist(ev_member, "text"),
            engine.cmd_allrank(ev_member, ""),
            engine.cmd_config(ev_member),
            engine.cmd_set(ev_member, "poll_interval_seconds", "45"),
            engine.cmd_rs(ev_member),
            engine.cmd_clear_cache(ev_member),
        ]
        for agen in admin_calls:
            txt = _reply_text(await _collect(agen))
            assert "权限不足" in txt

        # Admin CAN execute admin commands
        assert "✅" in _reply_text(await _collect(engine.cmd_off(ev_admin)))
        assert engine.store.get_group_settings("10086")["monitor_enabled"] is False
        assert "✅" in _reply_text(await _collect(engine.cmd_on(ev_admin)))
        assert engine.store.get_group_settings("10086")["monitor_enabled"] is True

        assert "✅" in _reply_text(await _collect(engine.cmd_rank_on(ev_admin, "")))
        assert engine.store.get_group_settings("10086")["rank_push_enabled"] is True

        cfg_txt = _reply_text(await _collect(engine.cmd_config(ev_admin)))
        assert "TEST_STEAM_KEY" not in cfg_txt  # secret must be masked!
        assert "******" in cfg_txt

        set_txt = _reply_text(
            await _collect(engine.cmd_set(ev_admin, "poll_interval_seconds", "45"))
        )
        assert "✅" in set_txt and engine.config["poll_interval_seconds"] == 45

        # /steam rs MUST NOT permanently close AchievementService
        rs_txt = _reply_text(await _collect(engine.cmd_rs(ev_admin)))
        assert "✅" in rs_txt
        assert engine.achievement_service is not None
        engine.steam_client.fetch_player_achievements = AsyncMock(return_value=set())
        engine.achievement_service.start_tracking(
            sid_other, "730", "CS2", "小红", ""
        )
        assert engine.achievement_service.is_tracking(sid_other) is True

        # /steam clear_allids MUST format count cleanly (no raw dict repr) and keep AchievementService usable
        clr_txt = _reply_text(await _collect(engine.cmd_clear_allids(ev_admin)))
        assert "✅" in clr_txt
        assert "{" not in clr_txt
        engine.achievement_service.start_tracking(
            sid_other, "730", "CS2", "小红", ""
        )
        assert engine.achievement_service.is_tracking(sid_other) is True

        await engine.terminate()

    asyncio.run(run())


def test_steamwho_price_px_game_openbox_and_candidate_selection(
    tmp_path: pathlib.Path,
):
    async def run():
        engine, _ = await _make_engine(tmp_path, html_render_fn=None)
        assert (
            engine.store is not None
            and engine.steam_client is not None
            and engine.price_service is not None
        )
        sid = "76561198000000301"
        engine.store.upsert_binding(sid, qq_id="40001", nickname="阿杰")

        # 1) /steamwho (在干嘛)
        engine.steam_client.fetch_player_statuses_batch = AsyncMock(
            return_value=(
                {
                    sid: {
                        "steamid": sid,
                        "name": "AJie",
                        "gameid": "1245620",
                        "gameextrainfo": "ELDEN RING",
                        "personastate": 1,
                        "avatarfull": "https://img/aj.jpg",
                    }
                },
                set(),
            )
        )
        engine.steam_client.get_game_names = AsyncMock(
            return_value=("艾尔登法环", "ELDEN RING")
        )
        engine.steam_client.get_game_cover_url = AsyncMock(
            return_value="https://cdn/1245620.jpg"
        )
        engine.steam_client.fetch_avatar_frame_url = AsyncMock(return_value=None)

        ev_who = FakeEvent(group_id="10086", sender_id="40001", is_admin=False)
        who_txt = _reply_text(await _collect(engine.cmd_who(ev_who, "")))
        assert "阿杰" in who_txt and "艾尔登法环" in who_txt

        # 2) /steam price -> multiple candidates -> reply "2" via on_message
        engine.steam_client.search_steam_store = AsyncMock(
            return_value=[
                {"appid": "1091500", "name": "Cyberpunk 2077", "type": "app"},
                {"appid": "2138330", "name": "Cyberpunk 2077: Phantom Liberty", "type": "dlc"},
            ]
        )
        sample_detail = {
            "name": "Cyberpunk 2077: Phantom Liberty",
            "header_image": "https://cdn/2138330.jpg",
            "short_description": "Spy-thriller expansion.",
            "release_date": {"date": "2023-09-26"},
            "developers": ["CD PROJEKT RED"],
            "publishers": ["CD PROJEKT RED"],
            "genres": [{"description": "RPG"}],
            "is_free": False,
            "price_overview": {
                "currency": "CNY",
                "initial": 14900,
                "final": 11920,
                "discount_percent": 20,
                "final_formatted": "¥ 119.20",
            },
        }
        engine.steam_client.fetch_store_appdetails = AsyncMock(return_value=sample_detail)
        engine.steam_client.fetch_game_reviews = AsyncMock(
            return_value={
                "all": {"desc": "特别好评", "percent": 89, "total": 25000},
                "schinese": None,
            }
        )
        engine.steam_client.get_game_online_count = AsyncMock(return_value=42000)
        engine.steam_client.fetch_region_price = AsyncMock(return_value=None)

        price_prompt = _reply_text(
            await _collect(engine.cmd_price(ev_who, "Cyberpunk 2077"))
        )
        assert "1." in price_prompt and "2." in price_prompt
        skey = engine._search_session_key(ev_who)
        assert engine.price_service.has_pending_selection(skey) is True

        ev_reply = FakeEvent(
            group_id="10086", sender_id="40001", message_str="2", umo=ev_who.unified_msg_origin
        )
        sel_txt = _reply_text(await _collect(engine.on_message(ev_reply)))
        assert ev_reply.stopped is True
        assert "Phantom Liberty" in sel_txt
        assert engine.price_service.has_pending_selection(skey) is False

        # 3) /steam px skips candidate selection and returns first match directly
        px_txt = _reply_text(await _collect(engine.cmd_px(ev_who, "Cyberpunk 2077")))
        assert "Cyberpunk" in px_txt

        # 4) /steam game <appid>
        game_txt = _reply_text(await _collect(engine.cmd_game(ev_who, "2138330")))
        assert "Phantom Liberty" in game_txt

        # 5) /steam openbox
        engine.steam_client.fetch_openbox_profile = AsyncMock(
            return_value={
                "steam_id": sid,
                "summary": {
                    "steamid": sid,
                    "personaname": "AJie",
                    "avatarfull": "https://img/aj.jpg",
                    "personastate": 1,
                    "timecreated": 1500000000,
                },
                "steam_level": 42,
                "bans": {"VACBanned": False, "NumberOfVACBans": 0, "NumberOfGameBans": 0},
                "owned_games_count": 120,
                "total_playtime_hours": 1532.5,
                "recent_games": [
                    {"appid": "1245620", "name": "ELDEN RING", "playtime_2weeks_hours": 18.5}
                ],
                "top_games": [
                    {"appid": "1245620", "name": "ELDEN RING", "playtime_hours": 320.0}
                ],
                "avatar_frame_url": None,
                "privacy_note": "",
            }
        )
        ob_txt = _reply_text(await _collect(engine.cmd_openbox(ev_who, sid)))
        assert "AJie" in ob_txt and "120" in ob_txt

        await engine.terminate()

    asyncio.run(run())


def test_steam_client_resolve_inputs_and_achievement_service(tmp_path: pathlib.Path):
    async def run():
        client = SteamClient(lambda: {"steam_api_key": "K", "retry_times": 1}, tmp_path)
        assert await client.resolve_steam_input("76561198000000001") == "76561198000000001"
        assert (
            await client.resolve_steam_input(
                "https://steamcommunity.com/profiles/76561198000000001/"
            )
            == "76561198000000001"
        )
        # Friend code: 100000000 + 76561197960265728 = 76561198060265728
        assert await client.resolve_steam_input("100000000") == "76561198060265728"
        assert await client.resolve_steam_input("[U:1:100000000]") == "76561198060265728"

        # AchievementService periodic unlock + delayed final check (deterministic)
        unlocked_payloads: list[dict[str, Any]] = []
        current_achs: set[str] = {"ACH_1"}
        baseline_done = asyncio.Event()
        first_unlock_ev = asyncio.Event()
        final_unlock_ev = asyncio.Event()

        async def fake_fetch_ach(sid: str, appid: str):
            snap = set(current_achs)
            baseline_done.set()
            return snap

        client.fetch_player_achievements = fake_fetch_ach  # type: ignore[method-assign]
        client.fetch_achievement_schema = AsyncMock(
            return_value={
                "ACH_2": {
                    "apiname": "ACH_2",
                    "display_name": "首杀",
                    "description": "击败第一个Boss",
                    "icon": "https://img/ach2.png",
                    "global_percent": 4.2,
                },
                "ACH_3": {
                    "apiname": "ACH_3",
                    "display_name": "终局",
                    "description": "通关主线",
                    "icon": "https://img/ach3.png",
                    "global_percent": 15.0,
                },
            }
        )

        async def on_unlocked(payload: dict[str, Any]) -> None:
            unlocked_payloads.append(payload)
            if len(unlocked_payloads) == 1:
                first_unlock_ev.set()
            elif len(unlocked_payloads) == 2:
                final_unlock_ev.set()

        ach_svc = AchievementService(
            client,
            lambda: {
                "enable_achievement_poll": True,
                "achievement_poll_interval_seconds": 0.03,
                "achievement_final_check_delay_seconds": 0.03,
            },
            on_unlocked,
        )
        ach_svc.start_tracking("76561198000000001", "1245620", "艾尔登法环", "玩家A", "")
        await asyncio.wait_for(baseline_done.wait(), timeout=2.0)

        # Unlock ACH_2 during active periodic polling
        current_achs.add("ACH_2")
        await asyncio.wait_for(first_unlock_ev.wait(), timeout=2.0)
        assert len(unlocked_payloads) == 1
        assert unlocked_payloads[0]["achievements"][0]["apiname"] == "ACH_2"

        # Stop tracking & unlock ACH_3 -> caught by delayed final check
        ach_svc.stop_tracking("76561198000000001", delayed_final_check=True)
        current_achs.add("ACH_3")
        await asyncio.wait_for(final_unlock_ev.wait(), timeout=2.0)
        assert len(unlocked_payloads) == 2
        assert unlocked_payloads[1]["achievements"][0]["apiname"] == "ACH_3"
        await ach_svc.aclose()

        # PriceService pending selection TTL expiration
        ps = PriceService(client, lambda: {}, pending_ttl_seconds=0.02)
        client.search_steam_store = AsyncMock(
            return_value=[
                {"appid": "10", "name": "Game One", "type": "app"},
                {"appid": "20", "name": "Game Two", "type": "app"},
            ]
        )
        cands = await ps.search_candidates("Game", session_key="sess_ttl")
        assert len(cands) == 2 and ps.has_pending_selection("sess_ttl") is True
        await asyncio.sleep(0.05)
        assert ps.has_pending_selection("sess_ttl") is False
        assert ps.select_candidate("sess_ttl", 1) is None

    asyncio.run(run())


# ===========================================================================
# 6. Card Renderer (All 7 Templates, Rank 100% Fixed Column, Graceful Fallback)
# ===========================================================================


def test_card_renderer_all_7_templates_and_fallbacks():
    sample_inputs: dict[str, tuple[str, dict[str, Any]]] = {
        "game_start.html": (
            "render_game_start",
            {
                "player_name": "阿青<script>alert(1)</script>",
                "avatar_url": "https://img/a.jpg",
                "game_name": "艾尔登法环",
                "game_name_en": "ELDEN RING",
                "appid": "1245620",
                "online_count": 88888,
                "started_at": 1_700_000_000.0,
            },
        ),
        "game_end.html": (
            "render_game_end",
            {
                "player_name": "阿青",
                "game_name": "艾尔登法环",
                "appid": "1245620",
                "duration_sec": 5400.0,
                "ended_at": 1_700_005_400.0,
                "tip": "记得休息！",
            },
        ),
        "achievement.html": (
            "render_achievement",
            {
                "player_name": "阿青",
                "game_name": "艾尔登法环",
                "appid": "1245620",
                "achievements": [
                    {
                        "display_name": "艾尔登之王",
                        "description": "达成艾尔登之王结局",
                        "icon": "https://img/ach.png",
                        "percent": 3.5,
                    }
                ],
            },
        ),
        "player_list.html": (
            "render_player_list",
            {
                "title": "本群 Steam 玩家实时状态",
                "group_label": "群 10086",
                "players": [
                    {
                        "player_name": "阿青",
                        "steam_id": "76561198000000001",
                        "status": "in_game",
                        "game_name": "CS2",
                        "game_id": "730",
                        "duration_sec": 1800,
                    },
                    {
                        "player_name": "白露",
                        "steam_id": "76561198000000002",
                        "status": "offline",
                        "last_logoff": 1_700_000_000,
                    },
                ],
            },
        ),
        "rank.html": (
            "render_rank",
            {
                "title": "昨日 Steam 游玩时长榜",
                "date_label": "2026/11/22",
                "group_label": "群 10086",
                "entries": [
                    {
                        "rank": 1,
                        "player_name": "肝帝一号",
                        "steam_id": "76561198000000001",
                        "total_seconds": 14400.0,
                        "top_game_id": "1245620",
                        "top_game_name": "艾尔登法环",
                        "games": [
                            {"gameid": "1245620", "name": "艾尔登法环", "seconds": 10800.0},
                            {"gameid": "730", "name": "CS2", "seconds": 3600.0},
                        ],
                    },
                    {
                        "rank": 2,
                        "player_name": "休闲二号",
                        "steam_id": "76561198000000002",
                        "total_seconds": 3600.0,
                        "top_game_id": "",
                        "top_game_name": "神秘游戏",
                        "games": [{"gameid": "0", "name": "神秘游戏", "seconds": 3600.0}],
                    },
                ],
            },
        ),
        "game_detail.html": (
            "render_game_detail",
            {
                "appid": "1245620",
                "name": "艾尔登法环",
                "release_date": "2022-02-25",
                "developers": ["FromSoftware"],
                "publishers": ["Bandai Namco"],
                "tags": ["魂类", "开放世界"],
                "price": {
                    "is_free": False,
                    "formatted": "¥298",
                    "regular_formatted": "¥298",
                    "cut": 0,
                },
                "itad": {
                    "history_low_formatted": "¥178",
                    "history_low_cut": 40,
                },
                "region_prices": [
                    {"region": "CN", "label": "国区", "converted_formatted": "¥298", "cut": 0},
                    {"region": "UA", "label": "乌区", "converted_formatted": "¥215", "cut": 0},
                ],
                "store_url": "https://store.steampowered.com/app/1245620/",
            },
        ),
        "openbox.html": (
            "render_openbox",
            {
                "steam_id": "76561198000000001",
                "summary": {"personaname": "阿青", "personastate": 1},
                "steam_level": 50,
                "bans": {"VACBanned": False, "NumberOfVACBans": 0},
                "owned_games_count": 88,
                "total_playtime_hours": 666.0,
                "recent_games": [{"appid": "730", "name": "CS2", "playtime_2weeks_hours": 10.0}],
                "top_games": [{"appid": "730", "name": "CS2", "playtime_hours": 300.0}],
            },
        ),
    }

    async def run():
        # A) None html_render -> all 7 templates render valid HTML & fall back cleanly to plain text
        renderer_none = SteamCardRenderer(html_render_fn=None)
        for tpl_name, (method_name, data) in sample_inputs.items():
            html = renderer_none.render_template_html(tpl_name, data)
            assert "<!DOCTYPE html>" in html or "<html" in html
            assert "</html>" in html
            if tpl_name == "game_start.html":
                assert "<script>" not in html  # Jinja autoescape active
            if tpl_name == "rank.html":
                assert "2026/11/22" in html
                assert "rk-progress-grid" in html
                assert "100%" in html
                assert "No Cover" not in html
            img_ref, fb_text = await getattr(renderer_none, method_name)(data)
            assert img_ref is None
            assert isinstance(fb_text, str) and len(fb_text.strip()) > 0

        # B) Failing html_render -> graceful plain-text fallback
        async def broken_render(*args, **kwargs):
            raise RuntimeError("playwright crashed")

        renderer_broken = SteamCardRenderer(html_render_fn=broken_render)
        img_ref, fb_text = await renderer_broken.render_rank(sample_inputs["rank.html"][1])
        assert img_ref is None
        assert "2026/11/22" in fb_text and "肝帝一号" in fb_text

        # C) Positional fallback signature (html, {}, False, opts) on TypeError
        seen_calls: list[tuple] = []

        async def legacy_sig_render(html: str, data: dict, return_url: bool, opts: dict):
            seen_calls.append((return_url, opts.get("type")))
            return "/tmp/card.png"

        renderer_ok = SteamCardRenderer(html_render_fn=legacy_sig_render)
        img_ref, _ = await renderer_ok.render_game_start(
            sample_inputs["game_start.html"][1]
        )
        assert img_ref == "/tmp/card.png"
        assert seen_calls == [(False, "png")]

    asyncio.run(run())


# ===========================================================================
# 7. Host Integration (Catalog, Legacy Guard, Schema, Web Console, LLM Tools)
# ===========================================================================


def test_host_integration_catalog_schema_web_console_and_llm_tools(
    tmp_path: pathlib.Path,
):
    # 1. LEGACY_PLUGINS & ENGINE_CATALOG
    assert "steam_status_monitor_V3" in LEGACY_PLUGIN_IDS
    assert any(pid == "steam_status_monitor_V3" for pid, _ in LEGACY_PLUGINS)
    sm_entries = [e for e in ENGINE_CATALOG if e["key"] == "steam_monitor"]
    assert len(sm_entries) == 1
    assert sm_entries[0]["config_flag"] == "enable_steam_monitor"

    # 2. _conf_schema.json contains modules.enable_steam_monitor & steam_monitor section
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "steam_monitor" in schema
    assert schema["modules"]["items"]["enable_steam_monitor"]["default"] is True
    sm_items = schema["steam_monitor"]["items"]
    for req_key in (
        "steam_api_key",
        "poll_interval_seconds",
        "exit_debounce_seconds",
        "game_filter_mode",
        "rank_push_hour",
        "rank_push_minute",
        "enable_network_fluctuation_notify",
    ):
        assert req_key in sm_items
    assert set(sm_items["game_filter_mode"]["enum"]) == {"全部游戏", "白名单", "黑名单"}
    assert sm_items["enable_network_fluctuation_notify"]["default"] is False

    # 3. Host main.py registers SteamMonitorEngine, /steam group, /steamwho (在干嘛), LLM tools
    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(main_src)
    assert "SteamMonitorEngine" in main_src
    assert 'command_group("steam")' in main_src
    assert '"steamwho"' in main_src and '"在干嘛"' in main_src
    llm_tools: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                raw = ast.unparse(dec)
                if "llm_tool" in raw:
                    llm_tools.add(node.name)
    assert {"get_group_steam_status", "get_group_steam_rank"}.issubset(llm_tools)

    # 4. Zero @register/@filter decorators anywhere in engines/steam_monitor/
    sm_dir = PLUGIN_ROOT / "engines" / "steam_monitor"
    for py in sm_dir.rglob("*.py"):
        py_tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(py_tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    raw = ast.unparse(dec)
                    assert not raw.startswith(("register(", "filter.")), (
                        f"Forbidden decorator in {py.name}: {raw}"
                    )

    # 5. Web Console runtime snapshot + LLM Tool summaries end-to-end
    async def run():
        engine, _ = await _make_engine(
            tmp_path, config={"steam_api_key": ""}, html_render_fn=None
        )
        assert engine.store is not None
        sid = "76561198000000401"
        engine.store.add_group_player("77777", sid)
        engine.store.upsert_binding(sid, nickname="测试玩家")
        now_ts = datetime.now().astimezone().timestamp()
        engine.store.save_active_session(
            ActiveSession(
                steam_id=sid,
                game_id="730",
                game_name="CS2",
                started_at=now_ts - 1200.0,
                state="playing",
                last_seen_at=now_ts,
                player_name="测试玩家",
            )
        )

        # Read-only LLM tool helpers
        status_summary = await engine.get_group_steam_status_summary("77777")
        assert status_summary["available"] is True
        assert status_summary["monitored_count"] == 1
        assert status_summary["playing_count"] == 1
        assert status_summary["playing"][0]["game_name"] == "CS2"

        rank_summary = await engine.get_group_steam_rank_summary("77777", days=1)
        assert rank_summary["available"] is True
        assert rank_summary["total_players"] == 1
        assert rank_summary["rankings"][0]["player_name"] == "测试玩家"
        assert rank_summary["rankings"][0]["total_minutes"] >= 19.0

        # GamesWebConsole._engine_runtime_snapshot includes steam_monitor summary
        # Stub astrbot.api.web if astrbot is not installed in standalone pytest env
        try:
            from astrbot_plugin_dynamics_games.core.web_console import GamesWebConsole
        except ImportError:
            fake_web = SimpleNamespace(json_response=lambda *a, **k: a, request=SimpleNamespace())
            sys.modules.setdefault("astrbot", SimpleNamespace())
            sys.modules.setdefault("astrbot.api", SimpleNamespace())
            sys.modules["astrbot.api.web"] = fake_web  # type: ignore[assignment]
            from astrbot_plugin_dynamics_games.core.web_console import GamesWebConsole

        fake_plugin = SimpleNamespace(
            module_enabled=lambda key: True,
            engines={"steam_monitor": engine},
        )
        console = GamesWebConsole(fake_plugin)
        snap = console._engine_runtime_snapshot()
        sm_snap = next(row for row in snap if row["key"] == "steam_monitor")
        assert sm_snap["enabled"] is True
        assert sm_snap["loaded"] is True
        assert sm_snap["runtime_summary"]["monitored_groups"] == 1
        assert sm_snap["runtime_summary"]["monitored_players"] == 1
        assert sm_snap["runtime_summary"]["playing_count"] == 1
        assert "监控 1 群" in sm_snap["desc"]

        await engine.terminate()

    asyncio.run(run())
