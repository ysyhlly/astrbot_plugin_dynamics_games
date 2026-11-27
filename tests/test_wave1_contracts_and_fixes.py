"""Wave 1 regression and contract tests for astrbot_plugin_dynamics_games."""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot.api.message_components import At, Plain  # noqa: E402
from astrbot_plugin_dynamics_games.core.commands import (  # noqa: E402
    build_help_text,
    handle_category_shortcut,
    handle_complete_command,
    handle_create_command,
    handle_join_command,
    handle_outcome_command,
    handle_submit_command,
)
from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.engines.blackjack21.main import (  # noqa: E402
    Blackjack21Plugin,
    Player,
    Room as BJRoom,
)
from astrbot_plugin_dynamics_games.engines.rollpig.main import (  # noqa: E402
    COMMAND_ALIASES as ROLLPIG_ALIASES,
    TRIGGER_WORDS as ROLLPIG_TRIGGERS,
    WAKE_FREE_PATTERN as ROLLPIG_WAKE_FREE_PATTERN,
)
from astrbot_plugin_dynamics_games.engines.steam_monitor.main import (  # noqa: E402
    HELP_TEXT as STEAM_HELP_TEXT,
)
from astrbot_plugin_dynamics_games.engines.truth_game.core.room import (  # noqa: E402
    Player as TGPlayer,
    Room as TGRoom,
)
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin  # noqa: E402


class _Event:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:GroupMessage:1001",
        group_id: str = "1001",
        uid: str = "101",
        name: str = "测试员",
        is_admin: bool = False,
        message_str: str = "",
        bot=None,
        platform_id: str = "",
    ) -> None:
        self.unified_msg_origin = umo
        self._gid = group_id
        self._uid = uid
        self._name = name
        self._admin = is_admin
        self.message_str = message_str
        self.bot = bot
        self._platform_id = platform_id
        self.message_obj = SimpleNamespace(message_id="m-1")
        self.sent: list = []

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return self._name

    def get_platform_id(self) -> str:
        return self._platform_id

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result) -> None:
        self.sent.append(result)


class _SavableConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self) -> None:
        self.save_count += 1


async def _drain(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


def _result_text(res) -> str:
    if isinstance(res, str):
        return res
    parts: list[str] = []
    for comp in getattr(res, "chain", []) or []:
        if isinstance(comp, Plain):
            parts.append(comp.text)
        elif isinstance(comp, At):
            parts.append(f"[At:{comp.qq}]")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 1. Shortcut guards, /共创 unification, help & outcome truncation hints
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_category_shortcut_subcommand_guards_and_creation_unification(
    tmp_path: pathlib.Path,
):
    store = ActivityStore(tmp_path / "shortcut.db")
    ctx = SimpleNamespace(get_all_stars=lambda: [], get_registered_star=lambda _n: None)
    coord = ActivityCoordinator(
        store,
        ctx,
        {"activities": {"memory_sync_enabled": False, "chronicle_sync_enabled": False}},
    )
    umo = "aiocqhttp:GroupMessage:8801"
    ev1 = _Event(umo=umo, group_id="8801", uid="1", name="甲")
    ev2 = _Event(umo=umo, group_id="8801", uid="2", name="乙")

    try:
        # When NO reading activity exists, reserved subwords MUST NOT create junk activities.
        for sub in ("帮助", "help", "列表", "退出", "取消", "开始", "阶段", "成果"):
            res = await handle_category_shortcut(coord, ev1, "reading", sub)
            assert store.list_activities(umo=umo) == [], f"subword {sub!r} leaked creation"
            assert res

        # Create a reading activity and verify subwords dispatch instead of checking in as content.
        created = await handle_category_shortcut(coord, ev1, "reading", "发起 银河铁道之夜 每晚一章")
        assert "银河铁道之夜" in created
        act = store.list_activities(umo=umo, status="open")[0]

        help_res = await handle_category_shortcut(coord, ev1, "reading", "帮助")
        assert "快捷指令说明" in help_res
        list_res = await handle_category_shortcut(coord, ev1, "reading", "列表")
        assert "银河铁道之夜" in list_res
        join_res = await handle_category_shortcut(coord, ev2, "reading", "报名 领读")
        assert "已报名" in join_res and "领读" in join_res
        start_res = await handle_category_shortcut(coord, ev1, "reading", "开始")
        assert "进行中" in start_res or "阶段" in start_res
        stage_res = await handle_category_shortcut(coord, ev1, "reading", "阶段 深度讨论")
        assert "深度讨论" in stage_res

        # None of those subwords should have created fake contributions!
        assert store.get_contributions(act["id"]) == []

        # /共读 退出 actually leaves the activity instead of checking in "退出"
        leave_res = await handle_category_shortcut(coord, ev2, "reading", "退出")
        assert "退出" in leave_res
        assert store.get_contributions(act["id"]) == []

        # /共读 取消 actually cancels the activity instead of checking in "取消"
        cancel_res = await handle_category_shortcut(coord, ev1, "reading", "取消 临时改期")
        assert "取消" in cancel_res or "流局" in cancel_res
        refreshed = store.get_activity(act["id"])
        assert refreshed is not None and refreshed["status"] == "cancelled"

        # /共创 unified shortcut: supports `/共创 发起 <标题>`, `/共创 发起 共创 <标题>`, and legacy `/共创 发起 共读 <标题>`
        c1 = await handle_category_shortcut(coord, ev1, "creation", "发起 本群黑话图鉴 征集词条")
        assert "共同制作作品" in c1 and "本群黑话图鉴" in c1
        c1_act = store.list_activities(umo=umo, category="creation", status="open")[0]
        assert c1_act["title"] == "本群黑话图鉴"

        # Submitting bare text via /共创 appends a contribution to the active creation activity
        c_sub = await handle_category_shortcut(coord, ev2, "creation", "新增词条：摸鱼学")
        assert "已记录" in c_sub
        contribs = store.get_contributions(c1_act["id"])
        assert len(contribs) == 1 and contribs[0]["content"] == "新增词条：摸鱼学"

        # Legacy `/共创 发起 共读 <标题>` remains compatible
        c_legacy = await handle_category_shortcut(coord, ev1, "creation", "发起 共读 三体")
        assert "组织共读" in c_legacy and "三体" in c_legacy

        # `/共创 发起 共创 <标题>` avoids titling the activity "共创"
        c_dup = await handle_category_shortcut(coord, ev1, "creation", "发起 共创 年度合集")
        assert "年度合集" in c_dup
        all_creation = store.list_activities(umo=umo, category="creation", status="open")
        assert any(a["title"] == "年度合集" for a in all_creation)
        assert not any(a["title"] == "共创" for a in all_creation)

        # `/共创 娱乐` dispatches to arcade summary
        arcade = await handle_category_shortcut(
            coord,
            ev1,
            "creation",
            "娱乐",
            module_states={"steam_monitor": True, "blackjack21": False},
        )
        assert "Steam 状态监控" in arcade
    finally:
        store.close()


@pytest.mark.asyncio
async def test_help_text_and_outcome_truncation_console_hint(tmp_path: pathlib.Path):
    help_txt = build_help_text()
    assert "Steam" in help_txt
    assert "阶段=A|B|C" in help_txt
    assert "目标=10" in help_txt
    assert "/共创" in help_txt

    store = ActivityStore(tmp_path / "outcome.db")
    ctx = SimpleNamespace(get_all_stars=lambda: [], get_registered_star=lambda _n: None)
    coord = ActivityCoordinator(
        store,
        ctx,
        {"activities": {"memory_sync_enabled": False, "chronicle_sync_enabled": False}},
    )
    umo = "aiocqhttp:GroupMessage:8802"
    ev1 = _Event(umo=umo, group_id="8802", uid="1", name="甲")
    ev2 = _Event(umo=umo, group_id="8802", uid="2", name="乙")
    try:
        await handle_create_command(coord, ev1, "接龙 超长作品测试 目标=2")
        act = store.list_activities(umo=umo, status="open")[0]
        await handle_join_command(coord, ev2, act["id"])
        for idx in range(12):
            await handle_submit_command(
                coord, ev1 if idx % 2 == 0 else ev2, f"{act['id']} 第{idx}段：" + ("故事内容" * 120)
            )
        await handle_complete_command(coord, ev1, f"{act['id']} 完结")

        text = await handle_outcome_command(coord, ev1, act["id"])
        assert len(text) <= 3600
        assert "已截断" in text
        assert "Web 控制台" in text
        assert act["id"] in text
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. Removed false triggers: rollpig "猪" & bare /steamwho alias "在干嘛"
# ---------------------------------------------------------------------------
def test_rollpig_removed_single_char_pig_trigger():
    assert "猪" not in ROLLPIG_TRIGGERS
    assert "猪" not in ROLLPIG_ALIASES
    pat = re.compile(ROLLPIG_WAKE_FREE_PATTERN)
    for daily_chat in ("猪脚饭", "烤乳猪", "今晚想吃猪脚饭", "来份脆皮烤乳猪", "猪"):
        assert pat.search(daily_chat) is None, f"Should NOT match {daily_chat!r}"
    for intended in ("今日小猪", "抽小猪", "猪咪", "🐷", "rollpig"):
        assert pat.search(intended) is not None, f"Should still match {intended!r}"


def test_steamwho_removed_bare_zaiganma_alias_and_converged_to_steam_group():
    src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DynamicsGamesPlugin"
    )
    methods = {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    # sm_steamwho must NOT carry bare alias "在干嘛"
    steamwho_decs = [ast.unparse(d) for d in methods["sm_steamwho"].decorator_list]
    assert not any("在干嘛" in d for d in steamwho_decs), steamwho_decs

    # sm_who (under @steam_group) carries "在干嘛" -> `/steam 在干嘛`
    who_decs = [ast.unparse(d) for d in methods["sm_who"].decorator_list]
    assert any("steam_group.command" in d and "在干嘛" in d for d in who_decs), who_decs

    # activity_group no longer hijacks /共创; shortcut_creation owns it
    act_group_decs = [ast.unparse(d) for d in methods["activity_group"].decorator_list]
    assert not any("共创" in d for d in act_group_decs), act_group_decs
    creation_decs = [ast.unparse(d) for d in methods["shortcut_creation"].decorator_list]
    assert any("共创" in d for d in creation_decs), creation_decs

    # bj_leave registered for /退出21点
    leave_decs = [ast.unparse(d) for d in methods["bj_leave"].decorator_list]
    assert any("退出21点" in d for d in leave_decs), leave_decs

    assert "/steam 在干嘛" in STEAM_HELP_TEXT


# ---------------------------------------------------------------------------
# 3. Blackjack 21: /退出21点, host transfer, auto-dissolve, non-QQ & soft penalty
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blackjack_leave_host_transfer_and_auto_dissolve():
    ctx = SimpleNamespace(get_config=lambda: {}, get_all_stars=lambda: [])
    bj = Blackjack21Plugin(ctx, {"waiting_timeout_seconds": 600})
    umo = "aiocqhttp:GroupMessage:7101"
    ev1 = _Event(umo=umo, group_id="7101", uid="101", name="房主甲")
    ev2 = _Event(umo=umo, group_id="7101", uid="102", name="玩家乙")
    ev3 = _Event(umo=umo, group_id="7101", uid="103", name="玩家丙")

    try:
        await _drain(bj.cmd_open(ev1))
        await _drain(bj.cmd_join(ev2))
        await _drain(bj.cmd_join(ev3))
        assert list(bj.rooms[umo].players.keys()) == ["101", "102", "103"]

        # Non-host leaves
        r_leave3 = _result_text((await _drain(bj.cmd_leave(ev3)))[0])
        assert "已退出21点房间" in r_leave3
        assert list(bj.rooms[umo].players.keys()) == ["101", "102"]

        # Host leaves -> transfers automatically to 102
        r_leave1 = _result_text((await _drain(bj.cmd_leave(ev1)))[0])
        assert "房主已自动移交" in r_leave1
        assert bj.rooms[umo].host_id == "102"
        assert bj.rooms[umo].players["102"].is_host is True

        # Last remaining player leaves -> room auto-dissolves & timer/lock cleaned up
        r_leave2 = _result_text((await _drain(bj.cmd_leave(ev2)))[0])
        assert "自动解散" in r_leave2
        assert umo not in bj.rooms
        assert umo not in bj.waiting_tasks
        assert umo not in bj._room_locks

        # Leaving while playing is rejected cleanly
        await _drain(bj.cmd_open(ev1))
        await _drain(bj.cmd_join(ev2))
        bj.rooms[umo].phase = "playing"
        r_playing = _result_text((await _drain(bj.cmd_leave(ev2)))[0])
        assert "已经开始" in r_playing
        assert "102" in bj.rooms[umo].players
    finally:
        await bj.terminate()


@pytest.mark.asyncio
async def test_blackjack_non_qq_platform_no_hardcoded_at_and_soft_penalty():
    ctx = SimpleNamespace(get_config=lambda: {}, get_all_stars=lambda: [])
    bj = Blackjack21Plugin(ctx, {"mute_per_point": 10, "max_mute_seconds": 120})
    umo = "telegram:GroupMessage:-100998877"
    ev1 = _Event(
        umo=umo,
        group_id="-100998877",
        uid="tg_alice",
        name="Alice",
        platform_id="telegram",
        bot=SimpleNamespace(),  # no set_group_ban
    )
    ev2 = _Event(
        umo=umo,
        group_id="-100998877",
        uid="tg_bob",
        name="Bob",
        platform_id="telegram",
        bot=SimpleNamespace(),
    )

    try:
        open_res = (await _drain(bj.cmd_open(ev1)))[0]
        # Must NOT contain At(qq=...) on Telegram!
        assert not any(isinstance(c, At) for c in open_res.chain)
        assert "@Alice" in _result_text(open_res)

        join_res = (await _drain(bj.cmd_join(ev2)))[0]
        assert not any(isinstance(c, At) for c in join_res.chain)
        assert "@Bob" in _result_text(join_res)

        # Force deterministic playing hands and settle
        room = bj.rooms[umo]
        room.phase = "playing"
        room.round_no = 1
        room.players["tg_alice"].cards = ["♠10", "♥9"]  # 19 pts
        room.players["tg_bob"].cards = ["♣10", "♦5"]  # 15 pts
        await _drain(bj.cmd_stand(ev1))
        settle_res = (await _drain(bj.cmd_stand(ev2)))[0]

        assert not any(isinstance(c, At) for c in settle_res.chain)
        settle_text = _result_text(settle_res)
        assert "权限不足" not in settle_text
        assert "仅计分" in settle_text
        assert "趣味软惩罚" in settle_text
        assert "@Alice" in settle_text and "@Bob" in settle_text
    finally:
        await bj.terminate()


# ---------------------------------------------------------------------------
# 4. Series contracts: set_series_scope_enabled, activate_series_runtime,
#    get_active_game_state, create_activity_from_companion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_series_scope_toggle_optimistic_lock_and_activate_runtime(
    tmp_path: pathlib.Path,
):
    excluded_umo = "aiocqhttp:GroupMessage:9999"

    class _ChatOwner:
        def get_series_shared_config(self):
            return {"exclude_groups": [excluded_umo]}

    stars = [
        SimpleNamespace(
            name="astrbot_plugin_chat_dynamics",
            activated=True,
            star_cls=_ChatOwner(),
        )
    ]
    ctx = SimpleNamespace(
        get_all_stars=lambda: stars,
        get_registered_star=lambda n: stars[0] if n == "astrbot_plugin_chat_dynamics" else None,
        get_config=lambda: {},
    )
    cfg = _SavableConfig({"activities": {"enabled": True}})
    plugin = DynamicsGamesPlugin(ctx, cfg)
    plugin.store = ActivityStore(tmp_path / "scope.db")
    plugin.coordinator = ActivityCoordinator(plugin.store, ctx, cfg)
    plugin._ready = True
    plugin.engines = {"rollpig": SimpleNamespace(roll_pig_wake_free=AsyncMock())}

    umo_a = "aiocqhttp:GroupMessage:1001"
    umo_b = "aiocqhttp:GroupMessage:1002"

    try:
        st0 = plugin.get_series_scope_status(umo_a)
        assert st0["umo"] == umo_a
        assert st0["enabled"] is True
        assert st0["writable"] is True
        assert st0["mode"] == "denylist"
        assert st0["detail"] == st0["summary"]
        v0 = st0["version"]

        # Disable umo_a with matching version
        r1 = await plugin.set_series_scope_enabled(umo_a, False, expected_version=v0)
        assert r1["ok"] is True
        assert r1["enabled"] is False
        assert r1["version"] != v0
        assert cfg.save_count == 1
        assert plugin.get_series_scope_status(umo_a)["enabled"] is False
        assert plugin.get_series_scope_status(umo_b)["enabled"] is True

        # Stale version rejected with conflict=True & reason="version_conflict"
        r_stale = await plugin.set_series_scope_enabled(umo_a, True, expected_version=v0)
        assert r_stale["ok"] is False
        assert r_stale["conflict"] is True
        assert r_stale["reason"] == "version_conflict"
        assert r_stale["version"] == r1["version"]

        # Guards honor per-group disable for umo_a while keeping umo_b active
        ev_a = _Event(umo=umo_a, group_id="1001")
        ev_b = _Event(umo=umo_b, group_id="1002")
        eng_a, reply_a = plugin._guard_event(ev_a, "rollpig", explicit=True)
        assert eng_a is None and "停用" in str(reply_a)
        assert plugin._guard_passive_event(ev_a, "rollpig") is None
        eng_b, reply_b = plugin._guard_event(ev_b, "rollpig", explicit=True)
        assert eng_b is not None and reply_b is None

        # Re-enable umo_a with current version
        r2 = await plugin.set_series_scope_enabled(umo_a, True, expected_version=r1["version"])
        assert r2["ok"] is True and r2["enabled"] is True
        assert plugin.get_series_scope_status(umo_a)["enabled"] is True

        # Excluded by core refuses enable
        r_ex = await plugin.set_series_scope_enabled(excluded_umo, True)
        assert r_ex["ok"] is False and r_ex["reason"] == "excluded_by_core"

        # activate_series_runtime re-enables global activities gate
        cfg["activities"]["enabled"] = False
        assert plugin.activities_enabled() is False
        act_res = plugin.activate_series_runtime()
        assert act_res["ok"] is True
        assert act_res["activated"] is True
        assert act_res["ready"] is True
        assert plugin.activities_enabled() is True

        # Legacy conflict blocks activate_series_runtime
        plugin._legacy_conflicts = ["astrbot_plugin_rollpig"]
        conflict_res = plugin.activate_series_runtime()
        assert conflict_res["ok"] is False
        assert conflict_res["reason"] == "legacy_conflicts"
        plugin._legacy_conflicts = []
    finally:
        plugin.store.close()


@pytest.mark.asyncio
async def test_active_game_state_and_create_activity_from_companion(
    tmp_path: pathlib.Path,
):
    ctx = SimpleNamespace(
        get_all_stars=lambda: [],
        get_registered_star=lambda _n: None,
        get_config=lambda: {},
    )
    cfg = _SavableConfig({"activities": {"enabled": True}})
    plugin = DynamicsGamesPlugin(ctx, cfg)
    plugin.store = ActivityStore(tmp_path / "companion.db")
    plugin.coordinator = ActivityCoordinator(plugin.store, ctx, cfg)
    plugin._ready = True

    umo = "aiocqhttp:GroupMessage:5005"
    tg_room_map: dict[str, TGRoom] = {}
    bj_room_map: dict[str, BJRoom] = {}
    sm_store = SimpleNamespace(
        list_group_players=lambda gid: ["76561198000000001"] if gid == "5005" else [],
        load_active_sessions=lambda: {
            "76561198000000001": SimpleNamespace(state="playing", game_name="Elden Ring")
        },
    )
    plugin.engines = {
        "truth_game": SimpleNamespace(room_manager=SimpleNamespace(rooms=tg_room_map)),
        "blackjack21": SimpleNamespace(rooms=bj_room_map),
        "rollpig": SimpleNamespace(),
        "what_to_eat": SimpleNamespace(),
        "steam_monitor": SimpleNamespace(store=sm_store),
    }

    try:
        # 1. Idle group (only background Steam playing -> does not falsely set chat room active)
        st_idle = plugin.get_active_game_state(umo)
        assert st_idle["active"] is False
        assert st_idle["engines"] == []
        assert st_idle["activities"] == []
        assert st_idle["companion_mode_hint"] == ""
        assert st_idle["engine_rooms"]["steam_monitor"]["playing_count"] == 1
        assert "Elden Ring" in st_idle["engine_rooms"]["steam_monitor"]["playing_games"]

        # 2. create_activity_from_companion (e.g. Horizons bookshelf -> reading club)
        c_res = await plugin.create_activity_from_companion(
            umo,
            "reading",
            "三体共读计划",
            description="跟随书架进度每周共读三章",
            creator_name="见闻书架",
            source_plugin="astrbot_plugin_dynamics_horizons",
            meta={"stages": ["招募", "共读", "复盘"], "target_count": 8},
        )
        assert c_res["ok"] is True
        assert c_res["created"] is True
        assert c_res["activity_id"].startswith("G")
        assert c_res["activity"]["category"] == "reading"
        assert c_res["activity"]["target_count"] == 8
        assert "astrbot_plugin_dynamics_horizons" in c_res["activity"]["description"]

        # Duplicate call deduplicates cleanly
        c_dup = await plugin.create_activity_from_companion(
            umo,
            "reading",
            "三体共读计划",
            source_plugin="astrbot_plugin_dynamics_horizons",
        )
        assert c_dup["ok"] is True
        assert c_dup["created"] is False
        assert c_dup["reason"] == "already_exists"
        assert c_dup["activity_id"] == c_res["activity_id"]

        # Active game state reflects the open activity
        st_act = plugin.get_active_game_state(umo)
        assert st_act["active"] is True
        assert len(st_act["activities"]) == 1
        assert st_act["activities"][0]["title"] == "三体共读计划"
        assert "activity_active" in st_act["companion_mode_hint"]

        # 3. Live truth_game waiting room & blackjack21 playing room
        tg_room = TGRoom(group_id="5005", host_id="1", phase="waiting", umo=umo, room_key=umo)
        tg_room.players["1"] = TGPlayer(user_id="1", nickname="甲")
        tg_room_map[umo] = tg_room

        st_wait = plugin.get_active_game_state(umo)
        assert st_wait["active"] is True
        assert st_wait["engines"] == ["truth_game"]
        assert "room_waiting" in st_wait["companion_mode_hint"]

        bj_room = BJRoom(group_id="5005", host_id="1", phase="playing", room_key=umo, round_no=2)
        bj_room.players["1"] = Player(user_id="1", is_host=True)
        bj_room.players["2"] = Player(user_id="2")
        bj_room_map[umo] = bj_room

        st_play = await plugin.get_active_game_state_async(umo)
        assert st_play["active"] is True
        assert set(st_play["engines"]) == {"truth_game", "blackjack21"}
        assert st_play["engine_rooms"]["blackjack21"]["round_no"] == 2
        assert "room_playing" in st_play["companion_mode_hint"]
    finally:
        plugin.store.close()
