"""Wave 2 feature & cross-plugin synergy tests for astrbot_plugin_dynamics_games."""

from __future__ import annotations

import datetime as dt
import pathlib
import random
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot.api.message_components import At, Plain  # noqa: E402
from astrbot_plugin_dynamics_games.bridges.social_bridge import (  # noqa: E402
    emit_entertainment_signal,
)
from astrbot_plugin_dynamics_games.core.commands import (  # noqa: E402
    handle_complete_with_media,
    handle_create_command,
    handle_join_command,
    handle_poster_command,
    handle_rebind_command,
    handle_steam_rally_command,
    handle_submit_with_media,
    handle_transfer_command,
    handle_withdraw_command,
)
from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.core.web_console import GamesWebConsole  # noqa: E402
from astrbot_plugin_dynamics_games.engines.blackjack21.main import (  # noqa: E402
    Blackjack21Plugin,
)
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.repository import (  # noqa: E402
    PigRepository,
)
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.service import (  # noqa: E402
    RollPigService,
)
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.storage import (  # noqa: E402
    BoardStore,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.main import (  # noqa: E402
    SteamMonitorEngine,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.state_machine import (  # noqa: E402
    ActiveSession,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.store import (  # noqa: E402
    SteamMonitorStore,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.main import (  # noqa: E402
    WhatToEatPlugin,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.taste_memory import (  # noqa: E402
    TasteMemoryStore,
)
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin  # noqa: E402


class _Event:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:GroupMessage:3001",
        group_id: str = "3001",
        uid: str = "101",
        name: str = "甲",
        is_admin: bool = False,
        message_str: str = "",
        messages: list | None = None,
        self_id: str = "99999",
        bot=None,
    ) -> None:
        self.unified_msg_origin = umo
        self._gid = group_id
        self._uid = uid
        self._name = name
        self._admin = is_admin
        self.message_str = message_str
        self._messages = list(messages or [])
        self._self_id = self_id
        self.bot = bot
        self.is_at_or_wake_command = True
        self.message_obj = SimpleNamespace(message_id="12345", message=self._messages)
        self.stopped = False

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return self._name

    def get_self_id(self) -> str:
        return self._self_id

    def get_messages(self) -> list:
        return list(self._messages)

    def is_admin(self) -> bool:
        return self._admin

    def is_private_chat(self) -> bool:
        return not bool(self._gid)

    def should_call_llm(self, _flag: bool) -> None:
        pass

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> str:
        return text

    def image_result(self, path: str) -> tuple[str, str]:
        return ("image", path)


async def _drain(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


def _result_text(res) -> str:
    if isinstance(res, str):
        return res
    parts = []
    for c in getattr(res, "chain", []) or []:
        if isinstance(c, Plain):
            parts.append(c.text)
        elif isinstance(c, At):
            parts.append(f"[At:{c.qq}]")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 1. Activity Poster (Visual Media + Pillow Fallback) & Story Relay Illustration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_activity_poster_visual_media_fallback_relay_and_archive(
    tmp_path: pathlib.Path, monkeypatch
):
    # Redirect default poster output to tmp_path so tests leave zero workspace artifacts
    from astrbot_plugin_dynamics_games.core import poster as poster_mod

    monkeypatch.setattr(poster_mod, "_default_output_dir", lambda: tmp_path / "posters")

    # Create a tiny valid sample artwork image simulating Visual Media output
    art_path = tmp_path / "visual_art.png"
    Image.new("RGB", (400, 300), "#789ABC").save(art_path)

    mem_records: list[dict] = []
    chr_records: list[dict] = []
    learning_signals: list[dict] = []
    world_signals: list[dict] = []

    class _FakeVisual:
        def get_dynamics_capabilities(self):
            return {"ready": True}

        async def generate_series_artwork(self, prompt, **kwargs):
            return {"ok": True, "image_path": str(art_path), "error": ""}

    class _FakeMemory:
        def get_dynamics_capabilities(self):
            return {"ready": True}

        async def record_activity_experience(self, payload):
            mem_records.append(payload)
            return {"recorded": True, "doc_id": len(mem_records)}

    class _FakeChronicle:
        def get_dynamics_capabilities(self):
            return {"ready": True}

        async def record_valuable_outcome(self, umo, payload):
            chr_records.append(payload)
            return {"admitted": True, "status": "approved", "event_id": len(chr_records)}

    class _FakeLearning:
        async def record_entertainment_signal(self, payload):
            learning_signals.append(payload)
            return {"ok": True}

    class _FakeWorld:
        async def record_mood_signal(self, payload):
            world_signals.append(payload)
            return {"ok": True}

    stars = [
        SimpleNamespace(name="astrbot_plugin_visual_media", activated=True, star_cls=_FakeVisual()),
        SimpleNamespace(name="astrbot_plugin_dynamics_memory", activated=True, star_cls=_FakeMemory()),
        SimpleNamespace(name="astrbot_plugin_group_chronicle", activated=True, star_cls=_FakeChronicle()),
        SimpleNamespace(name="astrbot_plugin_dynamics_learning", activated=True, star_cls=_FakeLearning()),
        SimpleNamespace(name="astrbot_plugin_dynamics_world", activated=True, star_cls=_FakeWorld()),
    ]
    ctx = SimpleNamespace(get_all_stars=lambda: stars, get_registered_star=lambda _n: None)
    store = ActivityStore(tmp_path / "poster.db")
    coord = ActivityCoordinator(
        store,
        ctx,
        {
            "activities": {
                "memory_sync_enabled": True,
                "chronicle_sync_enabled": True,
                "poster_on_complete": True,
                "relay_illustration_interval": 2,
            }
        },
    )
    umo = "aiocqhttp:GroupMessage:4001"
    ev1 = _Event(umo=umo, group_id="4001", uid="1", name="阿青")
    ev2 = _Event(umo=umo, group_id="4001", uid="2", name="白露")

    try:
        await handle_create_command(coord, ev1, "接龙 雨夜电车奇遇 目标=2")
        act = store.list_activities(umo=umo, status="open")[0]
        await handle_join_command(coord, ev2, act["id"])

        # Turn 1: below interval=2 -> no illustration yet
        _, img1 = await handle_submit_with_media(coord, ev1, f"{act['id']} 电车停在了无名站台。")
        assert img1 == ""

        # Turn 2: hits interval=2 -> auto-generates comic panel!
        txt2, img2 = await handle_submit_with_media(coord, ev2, f"{act['id']} 站台灯下站着一只撑伞的黑猫。")
        assert "连环画插画" in txt2
        assert img2 and pathlib.Path(img2).is_file()
        with Image.open(img2) as im:
            im.verify()

        # Complete activity -> generates poster + attaches image_path to Memory & Chronicle + emits social signals!
        comp_txt, poster_path = await handle_complete_with_media(coord, ev1, f"{act['id']} 完美收官")
        assert "画报" in comp_txt
        assert poster_path and pathlib.Path(poster_path).is_file()
        with Image.open(poster_path) as im:
            im.verify()

        assert mem_records and mem_records[0].get("image_path") == poster_path
        assert chr_records and chr_records[0].get("image_path") == poster_path
        assert len(learning_signals) == 1
        assert len(world_signals) == 1

        # Graceful Pillow fallback when Visual Media is unavailable
        coord.context = SimpleNamespace(get_all_stars=lambda: [], get_registered_star=lambda _n: None)
        p_txt, p_img = await handle_poster_command(coord, ev1, act["id"])
        assert "文本画报" in p_txt
        assert p_img and pathlib.Path(p_img).is_file()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. Steam Smart Matchmaking (/约局 摇人 + Post-Match Battle Report)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_steam_smart_matchmaking_rally_and_battle_report(
    tmp_path: pathlib.Path, monkeypatch
):
    from astrbot_plugin_dynamics_games.core import poster as poster_mod

    monkeypatch.setattr(poster_mod, "_default_output_dir", lambda: tmp_path / "posters")

    sm_store = SteamMonitorStore(tmp_path / "steam.db")
    gid = "5001"
    umo = f"aiocqhttp:GroupMessage:{gid}"
    sid1 = "76561198000001001"
    sid2 = "76561198000001002"
    sid3 = "76561198000001003"
    sm_store.upsert_binding(sid1, qq_id="101", nickname="猎人甲")
    sm_store.upsert_binding(sid2, qq_id="102", nickname="猎人乙")
    sm_store.upsert_binding(sid3, qq_id="103", nickname="老猎人丙")
    for sid in (sid1, sid2, sid3):
        sm_store.add_group_player(gid, sid)

    now = time.time()
    sm_store.save_active_session(
        ActiveSession(
            steam_id=sid1,
            game_id="2246340",
            game_name="Monster Hunter Wilds",
            started_at=now - 1800,
            state="playing",
            player_name="猎人甲",
        )
    )
    sm_store.save_active_session(
        ActiveSession(
            steam_id=sid2,
            game_id="2246340",
            game_name="Monster Hunter Wilds",
            started_at=now - 1200,
            state="playing",
            player_name="猎人乙",
        )
    )
    sm_store.insert_play_session(
        steam_id=sid3,
        game_id="2246340",
        game_name="Monster Hunter Wilds",
        started_at=now - 86400,
        ended_at=now - 82800,
        duration_sec=3600.0,
    )

    sm_engine = SteamMonitorEngine.__new__(SteamMonitorEngine)
    sm_engine.store = sm_store
    sm_engine._recent_achievements = [
        {
            "steam_id": sid1,
            "player_name": "猎人甲",
            "achievement_name": "初猎成功",
            "ts": now - 60,
        }
    ]

    ctx = SimpleNamespace(get_all_stars=lambda: [], get_registered_star=lambda _n: None)
    plugin_stub = SimpleNamespace(
        engines={"steam_monitor": sm_engine},
        module_enabled=lambda k, **kw: True,
    )
    store = ActivityStore(tmp_path / "games_steam.db")
    coord = ActivityCoordinator(
        store,
        ctx,
        {"activities": {"memory_sync_enabled": False, "chronicle_sync_enabled": False, "poster_on_complete": False}},
        plugin=plugin_stub,
    )
    ev1 = _Event(umo=umo, group_id=gid, uid="101", name="猎人甲")
    ev2 = _Event(umo=umo, group_id=gid, uid="102", name="猎人乙")

    try:
        # Creating matchmaking activity automatically surfaces matching Steam players!
        c_msg = await handle_create_command(coord, ev1, "约局 Monster Hunter Wilds 今晚狩猎")
        assert "Steam 同好可摇人" in c_msg
        assert "猎人甲" in c_msg and "猎人乙" in c_msg and "老猎人丙" in c_msg

        # Explicit `/约局 摇人` shows multiplayer cluster
        rally_msg = await handle_steam_rally_command(coord, ev1, "")
        assert "多人同玩热点" in rally_msg
        assert "Monster Hunter Wilds" in rally_msg

        # Completing matchmaking activity appends Steam battle report & achievements
        act = store.list_activities(umo=umo, status="open")[0]
        await handle_join_command(coord, ev2, act["id"])
        await handle_submit_with_media(coord, ev1, f"{act['id']} 狩猎开始")
        await handle_submit_with_media(coord, ev2, f"{act['id']} 讨伐成功")
        comp_txt, _ = await handle_complete_with_media(coord, ev1, act["id"])
        assert "恭喜完结" in comp_txt
        outcome = store.get_outcome(act["id"])
        assert outcome is not None
        assert "Steam 局后战报" in outcome["summary"]
        assert "初猎成功" in outcome["summary"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Blackjack 21 soft penalty mode & auto-fallback on OneBot permission failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blackjack_soft_penalty_mode_and_auto_fallback_on_ban_failure():
    ctx = SimpleNamespace(get_config=lambda: {}, get_all_stars=lambda: [])

    class _NoPermBot:
        async def set_group_ban(self, **_kw):
            raise RuntimeError("permission denied")

    # Auto mode: when OneBot set_group_ban fails due to missing admin rights, fall back to soft penalty!
    bj = Blackjack21Plugin(ctx, {"penalty_mode": "auto", "mute_per_point": 10})
    umo = "aiocqhttp:GroupMessage:6001"
    ev1 = _Event(umo=umo, group_id="6001", uid="101", name="甲", bot=_NoPermBot())
    ev2 = _Event(umo=umo, group_id="6001", uid="102", name="乙", bot=_NoPermBot())
    try:
        await _drain(bj.cmd_open(ev1))
        await _drain(bj.cmd_join(ev2))
        room = bj.rooms[umo]
        room.phase = "playing"
        room.round_no = 1
        room.players["101"].cards = ["♠10", "♥10"]
        room.players["102"].cards = ["♣10", "♦4"]
        await _drain(bj.cmd_stand(ev1))
        settle = _result_text((await _drain(bj.cmd_stand(ev2)))[0])
        assert "权限不足" not in settle
        assert "趣味软惩罚" in settle
    finally:
        await bj.terminate()

    # Soft mode: never calls set_group_ban at all
    ban_called = []

    class _SpyBot:
        async def set_group_ban(self, **kw):
            ban_called.append(kw)

    bj_soft = Blackjack21Plugin(ctx, {"penalty_mode": "soft"})
    ev3 = _Event(umo=umo, group_id="6001", uid="101", name="甲", bot=_SpyBot())
    ev4 = _Event(umo=umo, group_id="6001", uid="102", name="乙", bot=_SpyBot())
    try:
        await _drain(bj_soft.cmd_open(ev3))
        await _drain(bj_soft.cmd_join(ev4))
        room = bj_soft.rooms[umo]
        room.phase = "playing"
        room.round_no = 1
        room.players["101"].cards = ["♠10", "♥9"]
        room.players["102"].cards = ["♣10", "♦2"]
        await _drain(bj_soft.cmd_stand(ev3))
        settle = _result_text((await _drain(bj_soft.cmd_stand(ev4)))[0])
        assert ban_called == []
        assert "趣味软惩罚" in settle
    finally:
        await bj_soft.terminate()


# ---------------------------------------------------------------------------
# 4. Stale 24h reminders (respecting quiet_hours), multi-activity matching,
#    withdraw, rebind & creator transfer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stale_reminder_quiet_hours_withdraw_rebind_and_transfer(
    tmp_path: pathlib.Path,
):
    quiet_flag = {"quiet": True}

    class _ChatOwner:
        def get_series_shared_config(self):
            return {
                "quiet_hours": {
                    "daily_rhythm_enabled": True,
                    "in_quiet_hours": quiet_flag["quiet"],
                }
            }

    stars = [
        SimpleNamespace(
            name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_ChatOwner()
        )
    ]
    ctx = SimpleNamespace(get_all_stars=lambda: stars, get_registered_star=lambda _n: stars[0])
    store = ActivityStore(tmp_path / "lifecycle.db")
    coord = ActivityCoordinator(
        store,
        ctx,
        {
            "activities": {
                "stale_expire_hours": 72,
                "stale_reminder_hours": 24,
                "memory_sync_enabled": False,
                "chronicle_sync_enabled": False,
                "poster_on_complete": False,
            }
        },
    )
    umo = "aiocqhttp:GroupMessage:7001"
    ev1 = _Event(umo=umo, group_id="7001", uid="1", name="甲")
    ev2 = _Event(umo=umo, group_id="7001", uid="2", name="乙")

    try:
        # Activity aged 55h (within 24h of 72h expiry)
        now = time.time()
        act_old = store.create_activity(
            umo=umo,
            group_id="7001",
            category="reading",
            title="临期测试共读",
            creator_uid="1",
            creator_name="甲",
            created_at=now - 55 * 3600,
        )
        sent_reminders: list[tuple[str, str]] = []

        async def _send(target_umo, text):
            sent_reminders.append((target_umo, text))

        # Quiet hours -> reminder suppressed
        rem_quiet = await coord.check_and_send_stale_reminders(send_fn=_send, now=now)
        assert rem_quiet == [] and sent_reminders == []

        # Awake -> reminder sent once and NOT duplicated on second call
        quiet_flag["quiet"] = False
        rem1 = await coord.check_and_send_stale_reminders(send_fn=_send, now=now)
        assert len(rem1) == 1 and rem1[0]["id"] == act_old["id"]
        assert len(sent_reminders) == 1 and "临期提醒" in sent_reminders[0][1]
        rem2 = await coord.check_and_send_stale_reminders(send_fn=_send, now=now)
        assert rem2 == [] and len(sent_reminders) == 1

        # Multi-activity: ev2 joins act_old, then ev1 creates newer act_new
        await handle_join_command(coord, ev2, act_old["id"])
        act_new = store.create_activity(
            umo=umo,
            group_id="7001",
            category="challenge",
            title="最新挑战",
            creator_uid="1",
            creator_name="甲",
            created_at=now,
        )
        # ev2 submits without specifying ID -> prioritizes act_old (the one ev2 joined!) + echoes disambiguation tip
        sub_txt, _ = await handle_submit_with_media(coord, ev2, "读完第三章啦")
        assert act_old["id"] in sub_txt
        assert "/活动 撤回" in sub_txt and "/活动 改绑" in sub_txt

        # Rebind ev2's latest submission from act_old -> act_new
        reb_txt = await handle_rebind_command(coord, ev2, act_new["id"])
        assert "改绑" in reb_txt and act_new["id"] in reb_txt
        assert len(store.get_contributions(act_old["id"])) == 0
        assert len(store.get_contributions(act_new["id"])) == 1

        # Withdraw ev2's latest submission from act_new
        wd_txt = await handle_withdraw_command(coord, ev2, "")
        assert "撤回" in wd_txt
        assert len(store.get_contributions(act_new["id"])) == 0

        # Transfer creator of act_old from uid 1 -> uid 2
        ev_transfer = _Event(
            umo=umo,
            group_id="7001",
            uid="1",
            name="甲",
            messages=[At(qq="2")],
        )
        tr_txt = await handle_transfer_command(coord, ev_transfer, act_old["id"])
        assert "移交" in tr_txt
        refreshed = store.get_activity(act_old["id"])
        assert refreshed is not None and refreshed["creator_uid"] == "2"

        # Challenge completion includes soft penalty for 0-contribution member
        await handle_join_command(coord, ev2, act_new["id"])
        await handle_submit_with_media(coord, ev1, f"{act_new['id']} Day1打卡")
        await handle_complete_with_media(coord, ev1, act_new["id"])
        out_ch = store.get_outcome(act_new["id"])
        assert out_ch is not None and "趣味软惩罚" in out_ch["summary"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 5. Per-group module toggles & WebConsole Steam sub-panel APIs + UI checks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_per_group_modules_and_steam_web_console(tmp_path: pathlib.Path):
    excluded_umo = "aiocqhttp:GroupMessage:9099"

    class _ChatOwner:
        def get_series_shared_config(self):
            return {"exclude_groups": [excluded_umo]}

    stars = [
        SimpleNamespace(
            name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_ChatOwner()
        )
    ]
    ctx = SimpleNamespace(
        get_all_stars=lambda: stars,
        get_registered_star=lambda n: stars[0] if n == "astrbot_plugin_chat_dynamics" else None,
        get_config=lambda: {},
    )
    raw_cfg: dict = {"activities": {"enabled": True}, "modules": {"enable_rollpig": True, "enable_steam_monitor": True}}
    plugin = DynamicsGamesPlugin(ctx, raw_cfg)
    plugin.store = ActivityStore(tmp_path / "pg.db")
    plugin.coordinator = ActivityCoordinator(plugin.store, ctx, raw_cfg, plugin=plugin)
    plugin._ready = True

    sm_store = SteamMonitorStore(tmp_path / "sm_web.db")
    sm_engine = SteamMonitorEngine.__new__(SteamMonitorEngine)
    sm_engine.store = sm_store
    sm_engine._last_poll_ts = 0.0
    sm_engine.achievement_service = None
    sm_engine.config = {}
    sm_engine._cfg = lambda k, d=None: d

    plugin.engines = {
        "rollpig": SimpleNamespace(roll_pig_wake_free=AsyncMock()),
        "steam_monitor": sm_engine,
    }

    umo_a = "aiocqhttp:GroupMessage:1111"
    umo_b = "aiocqhttp:GroupMessage:2222"

    try:
        plugin.set_group_module_enabled(umo_a, "rollpig", False)
        assert plugin.module_enabled("rollpig", umo=umo_a) is False
        assert plugin.module_enabled("rollpig", umo=umo_b) is True

        ev_a = _Event(umo=umo_a, group_id="1111")
        ev_b = _Event(umo=umo_b, group_id="2222")
        ev_ex = _Event(umo=excluded_umo, group_id="9099")

        # Both explicit and passive honor per_group_modules & exclude_groups
        eng_a, rep_a = plugin._guard_event(ev_a, "rollpig", explicit=True)
        assert eng_a is None and "关闭" in str(rep_a)
        assert plugin._guard_passive_event(ev_a, "rollpig") is None

        eng_b, rep_b = plugin._guard_event(ev_b, "rollpig", explicit=True)
        assert eng_b is not None and rep_b is None

        eng_ex, rep_ex = plugin._guard_event(ev_ex, "rollpig", explicit=True)
        assert eng_ex is None and "排除" in str(rep_ex)
        assert plugin._guard_passive_event(ev_ex, "rollpig") is None

        # WebConsole endpoints: toggle_group_module, steam_player_action, steam_group_switch, steam_overview
        console = GamesWebConsole(plugin)
        console._body = AsyncMock(return_value={"umo": umo_a, "key": "rollpig", "enabled": True})
        res_tg = await console.toggle_group_module()
        assert res_tg["enabled"] is True

        console._body = AsyncMock(
            return_value={
                "action": "bind",
                "group_id": "1111",
                "steam_id": "76561198000009999",
                "qq_id": "88888",
                "nickname": "小星",
            }
        )
        res_bind = await console.steam_player_action()
        assert "76561198000009999" in res_bind["message"]

        console._body = AsyncMock(
            return_value={"group_id": "1111", "field": "rank_push_enabled", "enabled": True}
        )
        res_sw = await console.steam_group_switch()
        assert res_sw["group"]["rank_push_enabled"] is True

        console._q = lambda k, d="": ""
        sm_ov = await console.steam_overview()
        assert len(sm_ov["groups"]) == 1
        assert sm_ov["groups"][0]["rank_push_enabled"] is True
        assert sm_ov["groups"][0]["players"][0]["display_name"] == "小星"

        # Frontend HTML/JS structure & offline safety for Steam panel
        html = (PLUGIN_ROOT / "pages" / "games" / "index.html").read_text(encoding="utf-8")
        js = (PLUGIN_ROOT / "pages" / "games" / "app.js").read_text(encoding="utf-8")
        css = (PLUGIN_ROOT / "pages" / "games" / "style.css").read_text(encoding="utf-8")
        assert 'id="steam-console-card"' in html
        assert 'id="steam-bind-form"' in html
        assert "toggle_group_module" in js
        assert "steam_overview" in js
        for txt in (html, js, css):
            assert "http://" not in txt and "https://" not in txt
    finally:
        plugin.store.close()


# ---------------------------------------------------------------------------
# 6. Social daily minigames: what_to_eat (/口味 删除 + @multi-user) & rollpig
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_what_to_eat_delete_and_multi_user_intersection(tmp_path: pathlib.Path):
    ctx = SimpleNamespace(get_config=lambda: {}, get_all_stars=lambda: [])
    wte = WhatToEatPlugin(
        ctx,
        {
            "llm_enabled": False,
            "response_delay_enabled": False,
            "taste_memory_enabled": True,
            "builtin_foods": ["宫保鸡丁花生", "白灼虾", "香菜牛肉面", "番茄鸡蛋面", "烤鱼"],
        },
    )
    # Isolate taste memory into tmp_path
    wte.taste_memory = TasteMemoryStore(data_dir=tmp_path)
    wte.food_manager.taste_memory = wte.taste_memory

    wte.taste_memory.add_allergies("u1", ["花生"])
    wte.taste_memory.add_dislikes("u1", ["香菜"])
    wte.taste_memory.add_likes("u1", ["番茄"])

    wte.taste_memory.add_allergies("u2", ["虾"])
    wte.taste_memory.add_dislikes("u2", ["鱼"])
    wte.taste_memory.add_likes("u2", ["番茄"])

    signals: list[dict] = []

    async def _sig_cb(**kw):
        signals.append(kw)

    wte.signal_callback = _sig_cb

    # 1. `/口味 删除 香菜` removes tag cleanly
    ev_del = _Event(uid="u1", message_str="/口味 删除 香菜")
    del_out = await _drain(wte.cmd_taste_show(ev_del))
    assert "香菜" in del_out[0]
    assert "香菜" not in wte.taste_memory.get_profile("u1").dislikes

    # Restore dislike for multi-user intersection test
    wte.taste_memory.add_dislikes("u1", ["香菜"])

    # 2. `@u2 一起吃什么` intersects safe foods -> ONLY "番茄鸡蛋面" avoids 花生, 虾, 香菜, 鱼!
    ev_group = _Event(
        umo="aiocqhttp:GroupMessage:7701",
        group_id="7701",
        uid="u1",
        name="甲",
        message_str="@机器人 @乙 一起吃什么",
        messages=[At(qq="99999"), At(qq="u2")],
    )
    out_group = await _drain(wte.on_what_to_eat(ev_group))
    assert len(out_group) == 1
    assert "番茄鸡蛋面" in out_group[0]
    assert "多人合选" in out_group[0]
    assert "花生" in out_group[0] and "虾" in out_group[0]
    assert "口味共鸣" in out_group[0]
    assert len(signals) == 1 and signals[0]["signal_type"] == "what_to_eat"

    # Direct TasteMemoryStore.pick_food_for_group deterministic check
    picked, meta = wte.taste_memory.pick_food_for_group(
        ["u1", "u2"],
        ["宫保鸡丁花生", "白灼虾", "香菜牛肉面", "番茄鸡蛋面", "烤鱼"],
        rng=random.Random(7),
    )
    assert picked == "番茄鸡蛋面"
    assert set(meta["avoided_allergies"]) == {"花生", "虾"}
    assert "番茄" in meta["shared_likes"]


def test_rollpig_collision_easter_egg_and_collection_progress_across_days(
    tmp_path: pathlib.Path,
):
    res_dir = PLUGIN_ROOT / "engines" / "rollpig" / "resource"
    noop_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    repo = PigRepository(res_dir / "pig.json", res_dir / "image", noop_logger)
    day_box = [dt.date(2026, 11, 27)]
    pick_idx = [0]
    store = BoardStore(tmp_path / "rollpig_today.json", noop_logger)
    service = RollPigService(
        store,
        repo,
        chooser=lambda pigs: pigs[pick_idx[0]],
        clock=lambda: day_box[0],
    )

    # User A draws pig 0 in group G1
    o1 = service.draw_with_social_info("u1", group_id="G1", user_name="甲")
    assert o1 is not None
    assert o1.is_new_unlock is True
    assert o1.collected_count == 1
    assert o1.total_count == len(repo.pigs)
    assert o1.collision_peers == ()

    # User B draws the SAME pig 0 in group G1 on the same day -> collision easter egg!
    o2 = service.draw_with_social_info("u2", group_id="G1", user_name="乙")
    assert o2 is not None
    assert o2.pig.id == o1.pig.id
    assert o2.collision_peers == ("甲",)

    # Next day rollover: User A draws pig 1 -> collection progress persists and grows to 2!
    day_box[0] = dt.date(2026, 11, 28)
    pick_idx[0] = 1
    o3 = service.draw_with_social_info("u1", group_id="G1", user_name="甲")
    assert o3 is not None
    assert o3.is_new_unlock is True
    assert o3.collected_count == 2
    assert o3.collision_peers == ()


@pytest.mark.asyncio
async def test_social_bridge_gracefully_handles_missing_and_fallback_hooks():
    # No companions -> silent ok=False without raising
    res_empty = await emit_entertainment_signal(
        SimpleNamespace(get_all_stars=lambda: []),
        umo="aiocqhttp:GroupMessage:1",
        signal_type="truth_game",
        summary="开心玩了一局真心话",
        participants=[{"uid": "1", "name": "甲"}],
    )
    assert res_empty["learning"]["status"] == "unavailable"
    assert res_empty["world"]["status"] == "unavailable"

    # Reflection fallback on dynamics_learning when record_entertainment_signal is absent
    refl_calls: list[tuple] = []

    class _ReflectionOnlyLearning:
        async def ingest_horizons_reflection(self, umo, member_id, topic, summary, sentiment="positive"):
            refl_calls.append((umo, member_id, topic, summary, sentiment))
            return {"ok": True}

    ctx = SimpleNamespace(
        get_all_stars=lambda: [
            SimpleNamespace(
                name="astrbot_plugin_dynamics_learning",
                activated=True,
                star_cls=_ReflectionOnlyLearning(),
            )
        ]
    )
    res_fb = await emit_entertainment_signal(
        ctx,
        umo="aiocqhttp:GroupMessage:2",
        signal_type="blackjack21",
        summary="21点对局结束",
        participants=[{"uid": "u1", "name": "甲"}, {"uid": "u2", "name": "乙"}],
    )
    assert res_fb["learning"]["ok"] is True
    assert res_fb["learning"]["status"] == "sent_reflection"
    assert len(refl_calls) == 2
