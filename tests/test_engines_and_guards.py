"""Comprehensive host-free unit tests for built-in engines, bridges, guards, and health hooks."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import random
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games import series_panels  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.engines.blackjack21.main import (  # noqa: E402
    Blackjack21Plugin,
    Player,
    Room as BJRoom,
)
from astrbot_plugin_dynamics_games.engines.rollpig.main import (  # noqa: E402
    DATA_FILE_NAME as ROLLPIG_DATA_FILE,
    resolve_rollpig_data_dir,
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
from astrbot_plugin_dynamics_games.engines.truth_game.core import lm_bridge  # noqa: E402
from astrbot_plugin_dynamics_games.engines.truth_game.core.commands import (  # noqa: E402
    cmd_create,
    cmd_end,
    cmd_join,
    cmd_start,
    cmd_status,
)
from astrbot_plugin_dynamics_games.engines.truth_game.core.room import (  # noqa: E402
    RoomManager,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.taste_memory import (  # noqa: E402
    TasteMemoryStore,
)
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin  # noqa: E402


def _make_chat_ctx(
    *,
    exclude_groups: list[str] | None = None,
    in_quiet_hours: bool = False,
    reply_provider: str = "",
    provider_map: dict[str, object] | None = None,
):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": reply_provider,
                "exclude_groups": exclude_groups or [],
                "quiet_hours": {
                    "daily_rhythm_enabled": True,
                    "in_quiet_hours": in_quiet_hours,
                },
            }

    stars = {
        "chat": SimpleNamespace(
            name="astrbot_plugin_chat_dynamics",
            activated=True,
            star_cls=ChatOwner(),
        )
    }
    pm = (
        SimpleNamespace(inst_map=dict(provider_map), provider_insts=list(provider_map.values()))
        if provider_map is not None
        else None
    )
    get_by_id_calls: list[str] = []

    def _get_provider_by_id(pid: str):
        get_by_id_calls.append(pid)
        return (provider_map or {}).get(pid)

    ctx = SimpleNamespace(
        get_all_stars=lambda: stars,
        get_registered_star=lambda name: stars.get("chat")
        if name == "astrbot_plugin_chat_dynamics"
        else None,
        get_config=lambda: {},
        get_provider_by_id=_get_provider_by_id,
        _get_by_id_calls=get_by_id_calls,
    )
    if pm is not None:
        ctx.provider_manager = pm
    return ctx


class _SimpleEvent:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:GroupMessage:1001",
        group_id: str = "1001",
        uid: str = "101",
        name: str = "测试员",
        is_admin: bool = False,
        bot=None,
    ) -> None:
        self.unified_msg_origin = umo
        self._gid = group_id
        self._uid = uid
        self._name = name
        self._admin = is_admin
        self.bot = bot
        self.sent: list = []
        self.message_str = ""

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return self._name

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result) -> None:
        self.sent.append(result)


async def _drain(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 1. truth_game lm_bridge: dynamics_memory priority, dict stars, _terminating
# ---------------------------------------------------------------------------
def test_truth_lm_bridge_dynamics_memory_only_dict_stars_and_terminating(monkeypatch):
    monkeypatch.setattr(lm_bridge, "_lm_get_active", None)
    monkeypatch.delitem(sys.modules, "astrbot_plugin_livingmemory.api.public", raising=False)
    monkeypatch.delitem(sys.modules, "astrbot_plugin_dynamics_memory.api.public", raising=False)

    dm_instance = SimpleNamespace(
        initializer=SimpleNamespace(is_initialized=False, memory_engine=None)
    )
    stars_dict = {
        "astrbot_plugin_dynamics_memory": SimpleNamespace(
            name="astrbot_plugin_dynamics_memory",
            activated=True,
            star_cls=dm_instance,
        )
    }
    ctx = SimpleNamespace(
        get_registered_star=lambda _a: None,
        get_all_stars=lambda: stars_dict,
    )

    # Dict-shaped get_all_stars resolves dynamics_memory cleanly
    assert lm_bridge.detect_lm(ctx) is dm_instance
    # lm_unavailable_reason checks detect_lm(ctx) first: must NOT claim "未加载"
    reason = lm_bridge.lm_unavailable_reason(ctx)
    assert "尚未初始化" in reason
    assert "未加载" not in reason

    # Hot-reload _terminating / _closed must be rejected on every path
    dm_instance._terminating = True
    assert lm_bridge.detect_lm(ctx) is None
    dm_instance._terminating = False
    dm_instance._closed = True
    assert lm_bridge.detect_lm(ctx) is None

    # Facade module for dynamics_memory takes priority over livingmemory
    dm_facade = types.ModuleType("astrbot_plugin_dynamics_memory.api.public")
    dm_fresh = SimpleNamespace(name="dm_fresh")
    dm_facade.find_plugin = lambda _c: dm_fresh
    monkeypatch.setitem(sys.modules, "astrbot_plugin_dynamics_memory.api.public", dm_facade)

    lm_facade = types.ModuleType("astrbot_plugin_livingmemory.api.public")
    lm_old = SimpleNamespace(name="lm_old")
    lm_facade.find_plugin = lambda _c: lm_old
    monkeypatch.setitem(sys.modules, "astrbot_plugin_livingmemory.api.public", lm_facade)

    assert lm_bridge.detect_lm(SimpleNamespace()) is dm_fresh
    # Terminating facade plugin is skipped and falls through to usable fallback
    dm_fresh._terminating = True
    assert lm_bridge.detect_lm(SimpleNamespace()) is lm_old


# ---------------------------------------------------------------------------
# 2. truth_game UMO isolation, legacy group_id lookup & quiet_hours broadcast
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_truth_room_umo_isolation_legacy_lookup_and_quiet_hours():
    quiet_ctx = _make_chat_ctx(in_quiet_hours=True)
    rooms = RoomManager(
        {
            "bot_ask_enabled": True,
            "waiting_timeout_seconds": 0.05,
            "_waiting_check_interval": 0.01,
        },
        context=quiet_ctx,
    )
    bot_a = SimpleNamespace(send=AsyncMock())
    bot_b = SimpleNamespace(send=AsyncMock())

    ev_a1 = _SimpleEvent(umo="aiocqhttp:GroupMessage:7001", group_id="7001", uid="1", name="甲", bot=bot_a)
    ev_a2 = _SimpleEvent(umo="aiocqhttp:GroupMessage:7001", group_id="7001", uid="2", name="乙", bot=bot_a)
    ev_b1 = _SimpleEvent(umo="qq_official:GroupMessage:7001", group_id="7001", uid="9", name="丙", bot=bot_b)

    try:
        # Adapter A creates room; legacy bare group_id lookup succeeds while unambiguous
        assert "已创建" in (await cmd_create(rooms, ev_a1)).text()
        assert rooms.get_room("7001") is rooms.rooms[ev_a1.unified_msg_origin]
        assert rooms.status("7001")["umo"] == ev_a1.unified_msg_origin

        # Adapter B with SAME group_id creates its own room without being blocked
        assert "已创建" in (await cmd_create(rooms, ev_b1)).text()
        assert set(rooms.rooms.keys()) == {
            ev_a1.unified_msg_origin,
            ev_b1.unified_msg_origin,
        }
        # Ambiguous bare group_id returns None when 2 adapters share the same group_id
        assert rooms.get_room("7001") is None

        # Join & start on Adapter A does not affect Adapter B
        assert "报名成功" in (await cmd_join(rooms, ev_a2)).text()
        assert "游戏开始" in (await cmd_start(rooms, ev_a1)).text()
        assert "等待报名中" in (await cmd_status(rooms, ev_b1)).text()

        # Adapter B's waiting timeout expires during quiet hours: room closes cleanly, broadcast suppressed!
        await asyncio.wait_for(rooms._waiting_tasks[ev_b1.unified_msg_origin], 1.0)
        assert ev_b1.unified_msg_origin not in rooms.rooms
        bot_b.send.assert_not_awaited()

        # Adapter A still playing and can end cleanly
        assert "结束" in (await cmd_end(rooms, ev_a1)).text()
        assert ev_a1.unified_msg_origin not in rooms.rooms
    finally:
        await rooms.terminate()


# ---------------------------------------------------------------------------
# 3. blackjack21 game flow, timeout outside lock, lock reclaim, quiet_hours, terminate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blackjack_timeout_io_outside_lock_reclaim_quiet_and_terminate():
    ctx_awake = _make_chat_ctx(in_quiet_hours=False)
    bj = Blackjack21Plugin(ctx_awake, {"round_timeout_seconds": 5, "waiting_timeout_seconds": 60})
    umo = "aiocqhttp:GroupMessage:5001"
    lock_states: dict[str, bool] = {}

    async def _outcome_cb(**kwargs):
        lock = bj._room_locks.get(umo)
        lock_states["outcome_locked"] = bool(lock is not None and lock.locked())
        lock_states["outcome_umo"] = kwargs.get("umo")

    bj.outcome_callback = _outcome_cb
    ev1 = _SimpleEvent(umo=umo, group_id="5001", uid="101")
    ev2 = _SimpleEvent(umo=umo, group_id="5001", uid="102")

    async def _send_check(res):
        lock = bj._room_locks.get(umo)
        lock_states["send_locked"] = bool(lock is not None and lock.locked())
        ev1.sent.append(res)

    ev1.send = _send_check  # type: ignore[method-assign]

    try:
        # Prepare active playing room on round 1 where both players time out -> settle!
        room = BJRoom(group_id="5001", host_id="101", phase="playing", room_key=umo, round_no=1)
        room.reply_event = ev1
        room.players["101"] = Player(user_id="101", is_host=True, cards=["♠10", "♥9"])
        room.players["102"] = Player(user_id="102", cards=["♣10", "♦7"])
        bj.rooms[umo] = room

        # Arm a very short timeout via monkeypatching _round_timeout
        bj._round_timeout = lambda: 0.02  # type: ignore[method-assign]
        bj._arm_round_timer(room)
        await asyncio.wait_for(bj.round_tasks[umo], 1.0)

        # Room settled, lock reclaimed, and BOTH outcome_callback and _send_group ran OUTSIDE lock!
        assert umo not in bj.rooms
        assert umo not in bj._room_locks
        assert lock_states["outcome_locked"] is False
        assert lock_states["send_locked"] is False
        assert lock_states["outcome_umo"] == umo
        assert len(ev1.sent) == 1

        # cmd_end also reclaims _room_locks
        await _drain(bj.cmd_open(ev2))
        assert umo in bj.rooms and umo in bj._room_locks
        await _drain(bj.cmd_end(ev2))
        assert umo not in bj.rooms and umo not in bj._room_locks

        # Quiet hours suppresses proactive broadcast on timeout
        bj.context = _make_chat_ctx(in_quiet_hours=True)
        ev_q = _SimpleEvent(umo="aiocqhttp:GroupMessage:5002", group_id="5002", uid="201")
        room_q = BJRoom(
            group_id="5002",
            host_id="201",
            phase="playing",
            room_key=ev_q.unified_msg_origin,
            round_no=1,
        )
        room_q.reply_event = ev_q
        room_q.players["201"] = Player(user_id="201", is_host=True, cards=["♠10", "♥8"])
        room_q.players["202"] = Player(user_id="202", cards=["♣10", "♦6"])
        bj.rooms[ev_q.unified_msg_origin] = room_q
        bj._arm_round_timer(room_q)
        await asyncio.wait_for(bj.round_tasks[ev_q.unified_msg_origin], 1.0)
        assert ev_q.sent == []

        # terminate() cancels tasks via gather and clears rooms & locks
        await _drain(bj.cmd_open(ev1))
        assert bj.rooms and bj.waiting_tasks and bj._room_locks
        await bj.terminate()
        assert bj.rooms == {}
        assert bj._room_locks == {}
        assert bj.waiting_tasks == {}
        assert bj.round_tasks == {}
    finally:
        await bj.terminate()


# ---------------------------------------------------------------------------
# 4. what_to_eat allergen/dislike filtering & one-time legacy data migration
# ---------------------------------------------------------------------------
def test_what_to_eat_allergen_filter_and_taste_migration(tmp_path: pathlib.Path):
    legacy_dir = tmp_path / "legacy_wte"
    new_dir = tmp_path / "new_dg_wte"
    legacy_dir.mkdir(parents=True)
    legacy_payload = {
        "version": 1,
        "updated_at": 1700000000.0,
        "users": {
            "u1": {
                "user_id": "u1",
                "nickname": "阿花",
                "likes": ["牛肉面"],
                "dislikes": ["香菜"],
                "allergies": ["花生", "虾"],
                "food_scores": {},
                "recent": [],
            }
        },
    }
    legacy_file = legacy_dir / "taste_memory.json"
    legacy_file.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

    # Migration copies legacy data once into new_dir while preserving legacy_file
    store = TasteMemoryStore(data_dir=new_dir, legacy_data_dir=legacy_dir)
    assert (new_dir / "taste_memory.json").is_file()
    assert legacy_file.is_file()
    prof = store.get_profile("u1")
    assert prof.nickname == "阿花"
    assert "花生" in prof.allergies and "香菜" in prof.dislikes

    # Allergen and dislike filtering across repeated picks
    rng = random.Random(42)
    candidates = ["宫保鸡丁花生", "白灼虾", "香菜拌牛肉", "红烧牛肉面"]
    for _ in range(30):
        picked = store.pick_food("u1", candidates, rng=rng)
        assert picked == "红烧牛肉面"

    # When only disliked + allergen foods exist, dislikes relax while allergens remain blocked
    only_hard = ["宫保鸡丁花生", "白灼虾", "香菜拌牛肉"]
    for _ in range(20):
        picked = store.pick_food("u1", only_hard, rng=rng)
        assert picked == "香菜拌牛肉"

    # Non-empty new target is NEVER overwritten by older legacy data on subsequent startup
    store.add_likes("u1", ["拉面"])
    store.flush()
    reloaded = TasteMemoryStore(data_dir=new_dir, legacy_data_dir=legacy_dir)
    assert "拉面" in reloaded.get_profile("u1").likes


# ---------------------------------------------------------------------------
# 5. rollpig same-day idempotency & one-time legacy data migration
# ---------------------------------------------------------------------------
def test_rollpig_same_day_idempotency_and_data_migration(tmp_path: pathlib.Path):
    res_dir = PLUGIN_ROOT / "engines" / "rollpig" / "resource"
    noop_logger = SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    repo = PigRepository(res_dir / "pig.json", res_dir / "image", noop_logger)
    expected_pig = repo.pigs[0]
    expected_pig_id = expected_pig.id

    legacy_dir = tmp_path / "legacy_rp"
    new_dir = tmp_path / "new_dg_rp"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / ROLLPIG_DATA_FILE
    legacy_data = {"date": "2026-11-23", "records": {"u42": expected_pig.to_mapping()}}
    legacy_file.write_text(json.dumps(legacy_data, ensure_ascii=False), encoding="utf-8")

    resolved = resolve_rollpig_data_dir(new_dir=new_dir, legacy_dir=legacy_dir)
    migrated = resolved / ROLLPIG_DATA_FILE
    assert migrated.is_file()
    assert legacy_file.is_file()
    assert json.loads(migrated.read_text(encoding="utf-8")) == legacy_data

    # Same-day idempotency + next-day rollover
    today = [dt.date(2026, 11, 23)]
    service = RollPigService(BoardStore(migrated, noop_logger), repo, clock=lambda: today[0])

    # Migrated user keeps exact same pig id on the migrated date
    p1 = service.draw("u42")
    p2 = service.draw("u42")
    assert p1 is not None and p2 is not None
    assert p1.id == expected_pig_id and p2.id == expected_pig_id

    # Fresh user gets deterministic same-day result across calls
    u_first = service.draw("u99")
    u_second = service.draw("u99")
    assert u_first is not None and u_second is not None
    assert u_first.id == u_second.id


# ---------------------------------------------------------------------------
# 6. main.py passive listener exclude_groups + quiet_hours & get_series_health()
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_main_passive_guards_and_series_health(tmp_path: pathlib.Path):
    excluded_umo = "aiocqhttp:GroupMessage:8001"
    allowed_umo = "aiocqhttp:GroupMessage:8002"

    # A. Exclude-group blocks all 3 passive listeners (including tg_on_group_message!)
    ctx_ex = _make_chat_ctx(exclude_groups=[excluded_umo], in_quiet_hours=False)
    plugin = DynamicsGamesPlugin(
        ctx_ex,
        {
            "activities": {"enabled": True},
            "modules": {
                "enable_truth_game": True,
                "enable_blackjack21": True,
                "enable_rollpig": True,
                "enable_what_to_eat": True,
            },
            "what_to_eat": {"llm_enabled": True, "llm_provider_id": "missing-p"},
            "truth_game": {"question_source": "llm_first", "llm_provider_id": "ok-p"},
        },
    )
    plugin.store = ActivityStore(tmp_path / "guards.db")
    plugin._ready = True

    async def _gen_yield(_ev, *a, **kw):
        yield "fired"

    tg_eng = SimpleNamespace(
        on_group_message=_gen_yield,
        cmd_help=_gen_yield,
        room_manager=SimpleNamespace(rooms={}),
    )
    rp_eng = SimpleNamespace(roll_pig_wake_free=AsyncMock())
    wte_eng = SimpleNamespace(on_what_to_eat=_gen_yield)
    bj_eng = SimpleNamespace(rooms={})
    plugin.engines = {
        "truth_game": tg_eng,
        "blackjack21": bj_eng,
        "rollpig": rp_eng,
        "what_to_eat": wte_eng,
    }

    try:
        ev_ex = _SimpleEvent(umo=excluded_umo, group_id="8001")
        ev_ok = _SimpleEvent(umo=allowed_umo, group_id="8002")

        assert await _drain(plugin.tg_on_group_message(ev_ex)) == []
        await plugin.rp_roll_pig_wake_free(ev_ex)
        rp_eng.roll_pig_wake_free.assert_not_awaited()
        assert await _drain(plugin.wte_on_message(ev_ex)) == []

        # Allowed group outside quiet hours fires all 3 passive listeners
        assert await _drain(plugin.tg_on_group_message(ev_ok)) == ["fired"]
        await plugin.rp_roll_pig_wake_free(ev_ok)
        assert rp_eng.roll_pig_wake_free.await_count == 1
        assert await _drain(plugin.wte_on_message(ev_ok)) == ["fired"]

        # B. Quiet hours suppresses ALL 3 passive listeners even on allowed group,
        # while explicit command still executes!
        plugin.context = _make_chat_ctx(
            exclude_groups=[excluded_umo],
            in_quiet_hours=True,
            provider_map={"ok-p": SimpleNamespace(id="ok-p")},
        )
        assert await _drain(plugin.tg_on_group_message(ev_ok)) == []
        await plugin.rp_roll_pig_wake_free(ev_ok)
        assert rp_eng.roll_pig_wake_free.await_count == 1  # unchanged
        assert await _drain(plugin.wte_on_message(ev_ok)) == []
        # Explicit command still works during quiet hours
        assert await _drain(plugin.tg_help(ev_ok)) == ["fired"]

        # C. get_series_health() uses find_provider_silently (no cold-start get_provider_by_id!)
        health = series_panels.read_plugin_health("astrbot_plugin_dynamics_games", plugin)
        assert health is not None
        assert health["ready"] is True
        assert health["degraded"] is True  # missing-p triggers degraded issue
        by_role = {p["role"]: p for p in health["providers"]}
        assert by_role["truth_game_llm"]["status"] == "ok"
        assert by_role["what_to_eat_llm"]["status"] == "missing"
        # Crucially: provider_manager existed, so context.get_provider_by_id was NEVER called!
        assert plugin.context._get_by_id_calls == []
    finally:
        plugin.store.close()
