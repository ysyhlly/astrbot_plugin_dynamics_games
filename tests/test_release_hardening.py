"""Regression tests for multi-adapter 21-point isolation, legacy standby guards, and release docs."""

from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.engines.blackjack21.main import (  # noqa: E402
    Blackjack21Plugin,
)
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin  # noqa: E402


class _MockBot:
    def __init__(self) -> None:
        self.bans: list[tuple[int, int, int]] = []
        self.sent_group_msgs: list[tuple[int, Any]] = []

    async def set_group_ban(self, *, group_id: int, user_id: int, duration: int) -> None:
        self.bans.append((group_id, user_id, duration))

    async def send_group_msg(self, *, group_id: int, message: Any) -> None:
        self.sent_group_msgs.append((group_id, message))


class _MockEvent:
    def __init__(
        self,
        *,
        group_id: str = "1001",
        uid: str = "10001",
        umo: str = "",
        platform_id: str = "",
        is_admin: bool = False,
        bot: _MockBot | None = None,
    ) -> None:
        self._group_id = group_id
        self._uid = uid
        self.unified_msg_origin = umo
        self.platform_meta = SimpleNamespace(id=platform_id, name=platform_id) if platform_id else None
        self._is_admin = is_admin
        self.bot = bot
        self.sent: list[Any] = []
        self.message_str = ""

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return f"user-{self._uid}"

    def is_admin(self) -> bool:
        return self._is_admin

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result: Any) -> None:
        self.sent.append(result)


async def _drain(gen) -> list[Any]:
    out = []
    async for item in gen:
        out.append(item)
    return out


def test_blackjack_multi_adapter_same_group_id_isolated(tmp_path: pathlib.Path):
    async def run() -> None:
        store = ActivityStore(tmp_path / "bj_isolation.db")
        ctx = SimpleNamespace(get_config=lambda: {}, get_all_stars=lambda: [])
        coord = ActivityCoordinator(
            store,
            ctx,
            {"activities": {"memory_sync_enabled": False, "chronicle_sync_enabled": False}},
        )
        bj = Blackjack21Plugin(ctx, {"mute_per_point": 10, "max_mute_seconds": 120})
        bj.outcome_callback = coord.record_minigame_outcome

        bot_a = _MockBot()
        bot_b = _MockBot()
        umo_a = "aiocqhttp:GroupMessage:1001"
        umo_b = "qq_official:GroupMessage:1001"

        try:
            # Adapter A opens & fills room in group 1001
            ev_a1 = _MockEvent(group_id="1001", uid="101", umo=umo_a, bot=bot_a)
            ev_a2 = _MockEvent(group_id="1001", uid="102", umo=umo_a, bot=bot_a)
            await _drain(bj.cmd_open(ev_a1))
            await _drain(bj.cmd_join(ev_a2))

            # Adapter B opens & fills room in the SAME native group_id "1001"
            ev_b1 = _MockEvent(group_id="1001", uid="201", umo=umo_b, bot=bot_b)
            ev_b2 = _MockEvent(group_id="1001", uid="202", umo=umo_b, bot=bot_b)
            await _drain(bj.cmd_open(ev_b1))
            await _drain(bj.cmd_join(ev_b2))

            # Rooms, locks, and waiting timers must all be keyed by full session UMO
            assert set(bj.rooms.keys()) == {umo_a, umo_b}
            assert set(bj.waiting_tasks.keys()) == {umo_a, umo_b}
            assert bj._room_lock(umo_a) is not bj._room_lock(umo_b)
            assert set(bj.rooms[umo_a].players.keys()) == {"101", "102"}
            assert set(bj.rooms[umo_b].players.keys()) == {"201", "202"}

            # Start both rooms; verify round timers are isolated
            await _drain(bj.cmd_deal(ev_a1))
            await _drain(bj.cmd_deal(ev_b1))
            if umo_a in bj.rooms:
                assert umo_a in bj.round_tasks
            if umo_b in bj.rooms:
                assert umo_b in bj.round_tasks

            # Ending Adapter A's room must NOT touch Adapter B's room or timer
            await _drain(bj.cmd_end(ev_a1))
            assert umo_a not in bj.rooms
            assert umo_a not in bj.round_tasks
            assert umo_b in bj.rooms
            assert bj.rooms[umo_b].group_id == "1001"

            # Force deterministic hands on Adapter B and settle via both players standing
            room_b = bj.rooms[umo_b]
            room_b.players["201"].cards = ["♠10", "♥9"]  # 19 pts (winner)
            room_b.players["201"].stood = False
            room_b.players["202"].cards = ["♣10", "♦5"]  # 15 pts (diff=4 -> 40s ban)
            room_b.players["202"].stood = False
            room_b.round_actions.clear()
            await _drain(bj.cmd_stand(ev_b1))
            await _drain(bj.cmd_stand(ev_b2))

            assert umo_b not in bj.rooms
            # Platform ban must use native integer group_id 1001 (NOT full UMO)
            assert bot_b.bans == [(1001, 202, 40)]
            assert bot_a.bans == []

            # Archived outcome must belong strictly to Adapter B's UMO, not Adapter A's
            outcomes_b = store.list_outcomes(umo=umo_b, limit=10)
            outcomes_a = store.list_outcomes(umo=umo_a, limit=10)
            assert len(outcomes_b) == 1
            assert outcomes_b[0]["umo"] == umo_b
            assert outcomes_a == []
        finally:
            await bj.terminate()
            store.close()

    asyncio.run(run())


def test_blackjack_platform_meta_fallback_isolation():
    """Events without unified_msg_origin still isolate by platform_meta.id + group_id."""
    async def run() -> None:
        ctx = SimpleNamespace(get_config=lambda: {})
        bj = Blackjack21Plugin(ctx, {})
        try:
            ev_p1 = _MockEvent(group_id="5555", uid="1", platform_id="napcat_1")
            ev_p2 = _MockEvent(group_id="5555", uid="2", platform_id="napcat_2")
            await _drain(bj.cmd_open(ev_p1))
            await _drain(bj.cmd_open(ev_p2))
            assert set(bj.rooms.keys()) == {
                "napcat_1:GroupMessage:5555",
                "napcat_2:GroupMessage:5555",
            }
            assert bj.rooms["napcat_1:GroupMessage:5555"].group_id == "5555"
            assert bj.rooms["napcat_2:GroupMessage:5555"].group_id == "5555"
        finally:
            await bj.terminate()

    asyncio.run(run())


def test_legacy_conflict_standby_command_dispatch_never_raises_keyerror():
    async def run() -> None:
        ctx = SimpleNamespace(
            get_registered_star=lambda n: SimpleNamespace(activated=True)
            if n == "astrbot_plugin_rollpig"
            else None,
            get_config=lambda: {},
        )
        plugin = DynamicsGamesPlugin(ctx, {})
        await plugin.initialize()

        assert plugin._ready is False
        assert plugin._legacy_conflicts == ["astrbot_plugin_rollpig"]
        assert plugin.engines == {}

        ev = _MockEvent(group_id="1001", uid="42", umo="aiocqhttp:GroupMessage:1001")

        # Conflicting legacy engine (rollpig) yields silently to avoid duplicate replies
        rp_out = await _drain(plugin.rp_roll_pig(ev))
        assert rp_out == []
        await plugin.rp_roll_pig_wake_free(ev)
        assert ev.sent == []

        # Non-conflicting entertainment commands return friendly standby notice, never KeyError
        command_handlers = [
            plugin.tg_create,
            plugin.tg_join,
            plugin.tg_start,
            plugin.tg_next,
            plugin.tg_skip,
            plugin.tg_leave,
            plugin.tg_end,
            plugin.tg_status,
            plugin.tg_help,
            plugin.bj_help,
            plugin.bj_open,
            plugin.bj_join,
            plugin.bj_deal,
            plugin.bj_hit,
            plugin.bj_stand,
            plugin.bj_status,
            plugin.bj_end,
            plugin.wte_taste_show,
            plugin.wte_taste_like,
            plugin.wte_taste_dislike,
            plugin.wte_taste_allergy,
            plugin.wte_taste_clear,
        ]
        for handler in command_handlers:
            replies = await _drain(handler(ev))
            assert len(replies) == 1, f"{handler.__name__} should yield standby notice"
            assert "待机模式" in str(replies[0])
            assert "astrbot_plugin_rollpig" in str(replies[0])

        # Passive listeners stay silent in standby mode
        assert await _drain(plugin.tg_on_group_message(ev)) == []
        assert await _drain(plugin.wte_on_message(ev)) == []

        # Uninitialized plugin without legacy conflicts also guards cleanly without KeyError
        cold = DynamicsGamesPlugin(SimpleNamespace(get_config=lambda: {}), {})
        cold_replies = await _drain(cold.bj_open(ev))
        assert len(cold_replies) == 1
        assert "尚未就绪" in str(cold_replies[0])

    asyncio.run(run())


def test_readme_migration_guide_accuracy():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "100% 保留" not in readme
    for required_path in (
        "data/plugin_data/astrbot_plugin_rollpig/rollpig_today.json",
        "data/plugin_data/astrbot_plugin_what_to_eat/taste_memory.json",
        "data/plugins/astrbot_plugin_rollpig/resource/pig.json",
        "data/plugins/astrbot_plugin_dynamics_games/engines/rollpig/resource/pig.json",
        "data/plugins/astrbot_plugin_rollpig/resource/font/",
        "data/plugins/astrbot_plugin_dynamics_games/engines/rollpig/resource/font/",
        "data/plugins/astrbot_plugin_what_to_eat/images/",
        "data/plugins/astrbot_plugin_dynamics_games/engines/what_to_eat/images/",
        "data/config/astrbot_plugin_dynamics_games_config.json",
    ):
        assert required_path in readme, f"Missing migration path in README: {required_path}"
