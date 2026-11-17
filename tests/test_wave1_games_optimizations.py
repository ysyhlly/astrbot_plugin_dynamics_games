"""Wave 1 optimization tests for astrbot_plugin_dynamics_games."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import subprocess
import sys
import time
from types import SimpleNamespace

from PIL import Image, ImageFont

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games.bridges.chronicle_bridge import (  # noqa: E402
    chronicle_status,
    find_chronicle_plugin,
    sync_outcome_to_chronicle,
)
from astrbot_plugin_dynamics_games.bridges.memory_bridge import (  # noqa: E402
    find_memory_plugin,
    memory_status,
    sync_experience_to_memory,
)
from astrbot_plugin_dynamics_games.core.commands import (  # noqa: E402
    handle_cleanup_command,
    handle_complete_command,
    handle_create_command,
    handle_join_command,
    handle_submit_command,
)
from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.engines.blackjack21.main import (  # noqa: E402
    Blackjack21Plugin,
    Player,
    Room,
)
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.renderer import (  # noqa: E402
    AvatarBlock,
    CanvasSpec,
)
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.text import TextMetrics  # noqa: E402
from astrbot_plugin_dynamics_games.engines.truth_game.core import lm_bridge  # noqa: E402
from astrbot_plugin_dynamics_games.engines.what_to_eat.rate_limiter import (  # noqa: E402
    RateLimiter,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.taste_memory import (  # noqa: E402
    TasteMemoryStore,
)
from test_dynamics_games import (  # noqa: E402
    FakeAstrBotContext,
    FakeChronicleCompanion,
    FakeEvent,
    FakeMemoryCompanion,
)


def test_rollpig_fonts_unchanged_and_compressed_pngs_valid():
    expected_fonts = {
        "可爱字体.ttf": "28D6BDBF9452BA98DE01DD6C3091BDB48ACFC51EABAFF73FE459FFACECFFA7FE",
        "荆南麦圆体.otf": "F2BD10A109F5E0D7FC39E7A0ADFE0DD55DC8297522558B4B577B93A505F86F0D",
    }
    font_dir = PLUGIN_ROOT / "engines" / "rollpig" / "resource" / "font"
    for fname, digest in expected_fonts.items():
        actual = hashlib.sha256((font_dir / fname).read_bytes()).hexdigest().upper()
        assert actual == digest

    png_dir = PLUGIN_ROOT / "engines" / "rollpig" / "resource" / "image"
    pngs = sorted(png_dir.glob("*.png"))
    assert len(pngs) == 96
    for p in pngs + [PLUGIN_ROOT / "engines" / "what_to_eat" / "logo.png"]:
        with Image.open(p) as im:
            im.verify()

    # Palette PNG with transparency must load as RGBA for alpha mask compositing.
    sample = AvatarBlock(
        image_path=pngs[0],
        spec=CanvasSpec(),
        metrics=TextMetrics(),
        error_font=ImageFont.load_default(),
        logger=logging.getLogger("test"),
    )
    sample.measure(800)
    assert sample._image is not None
    assert sample._image.mode in {"RGBA", "RGB"}


def test_stale_ttl_cleanup_command_and_checked_create(tmp_path: pathlib.Path):
    async def run():
        store = ActivityStore(tmp_path / "games.db")
        try:
            ctx = FakeAstrBotContext()
            coord = ActivityCoordinator(
                store,
                ctx,
                {"activities": {"max_active_per_group": 1, "stale_expire_hours": 1}},
            )
            umo = "aiocqhttp:GroupMessage:1001"
            ev_member = FakeEvent(umo=umo, group_id="1001", uid="11", name="成员")
            ev_admin = FakeEvent(umo=umo, group_id="1001", uid="99", name="管理员", is_admin=True)

            # Create an old stale recruiting activity (>2h ago).
            old_ts = time.time() - 7200
            stale = store.create_activity(
                umo=umo,
                group_id="1001",
                category="reading",
                title="长期未推进旧共读",
                creator_uid="11",
                creator_name="成员",
                created_at=old_ts,
            )

            # Non-admin blocked from manual cleanup.
            deny_msg = await handle_cleanup_command(coord, ev_member, "1")
            assert "管理员" in deny_msg

            # Admin manual cleanup cancels stale recruiting activity.
            ok_msg = await handle_cleanup_command(coord, ev_admin, "1")
            assert stale["id"] in ok_msg
            refreshed = store.get_activity(stale["id"])
            assert refreshed is not None and refreshed["status"] == "cancelled"

            # Checked creation blocks once open limit (1) is reached.
            ok1, _, _ = await coord.create_activity(
                umo=umo,
                group_id="1001",
                category_raw="共读",
                title="活跃共读",
                creator_uid="11",
                creator_name="成员",
            )
            ok2, msg2, _ = await coord.create_activity(
                umo=umo,
                group_id="1001",
                category_raw="挑战",
                title="超出上限活动",
                creator_uid="11",
                creator_name="成员",
            )
            assert ok1 is True and ok2 is False and "上限" in msg2
        finally:
            store.close()

    asyncio.run(run())


def test_unified_sync_retry_backoff_and_timeout_handling(tmp_path: pathlib.Path):
    async def run():
        store = ActivityStore(tmp_path / "sync.db")
        try:
            # Start with slow/unavailable bridges so completion enters pending retry queue.
            class _SlowMem(FakeMemoryCompanion):
                async def record_activity_experience(self, payload):
                    await asyncio.sleep(0.2)
                    return {"recorded": True, "doc_id": 1}

            ctx = FakeAstrBotContext()
            coord = ActivityCoordinator(
                store,
                ctx,
                {"activities": {"memory_sync_enabled": True, "chronicle_sync_enabled": True}},
            )
            umo = "aiocqhttp:GroupMessage:2002"
            ev1 = FakeEvent(umo=umo, group_id="2002", uid="1", name="甲")
            ev2 = FakeEvent(umo=umo, group_id="2002", uid="2", name="乙")

            await handle_create_command(coord, ev1, "共创 协同作品 目标=2")
            act = store.list_activities(umo=umo, status="open")[0]
            await handle_join_command(coord, ev2, act["id"])
            await handle_submit_command(coord, ev1, f"{act['id']} 片段A")
            await handle_submit_command(coord, ev2, f"{act['id']} 片段B")
            await handle_complete_command(coord, ev1, f"{act['id']} 完成")

            # Second transient failure triggers exponential backoff (>=30s), while immediate 1st retry still recovers on reload.
            await coord.flush_truth_outcomes()
            assert store.pending_outcome_syncs(memory=True, chronicle=True, now=time.time() + 100) == [act["id"]]
            assert store.pending_outcome_syncs(memory=True, chronicle=True, now=time.time()) == []

            # Simulate recovery with live companions -> unified flush succeeds without truth_room_outcomes.
            mem = FakeMemoryCompanion()
            chr_plugin = FakeChronicleCompanion()
            coord.context = FakeAstrBotContext(memory=mem, chronicle=chr_plugin)
            with store.connection(write=True) as db:
                db.execute("UPDATE outcomes SET next_sync_at=0 WHERE activity_id=?", (act["id"],))
            await coord.flush_truth_outcomes()
            outcome = store.get_outcome(act["id"])
            assert outcome is not None
            assert outcome["memory_doc_id"] > 0
            assert outcome["chronicle_event_id"] > 0

            # Timeout safety on slow companion bridge.
            slow_ctx = FakeAstrBotContext(memory=_SlowMem())
            res = await sync_experience_to_memory(
                slow_ctx,
                umo=umo,
                activity_id="G999",
                category="reading",
                category_label="组织共读",
                title="超时测试",
                event_kind="completed",
                summary="s",
                participants=[{"uid": "1", "name": "甲"}],
                timeout=0.05,
            )
            assert res["ok"] is False and res["status"] == "timeout"

            slow_chr = FakeChronicleCompanion()

            async def _hang(*a, **kw):
                await asyncio.sleep(0.2)
                return {"admitted": True, "event_id": 1}

            slow_chr.record_valuable_outcome = _hang
            res_chr = await sync_outcome_to_chronicle(
                FakeAstrBotContext(chronicle=slow_chr),
                umo=umo,
                activity={"id": "G998", "category": "creation", "title": "t", "status": "completed", "creator_uid": "1"},
                participants=[{"uid": "1", "name": "甲", "contributions_count": 1}, {"uid": "2", "name": "乙", "contributions_count": 1}],
                contributions=[],
                summary="s",
                valuable=True,
                timeout=0.05,
            )
            assert res_chr["ok"] is False and res_chr["status"] == "timeout"
        finally:
            store.close()

    asyncio.run(run())


def test_bridges_respect_terminating_and_closed_and_ready_checks():
    mem = FakeMemoryCompanion()
    mem._terminating = True
    chr_p = FakeChronicleCompanion()
    chr_p._closed = True
    ctx = FakeAstrBotContext(memory=mem, chronicle=chr_p)
    assert find_memory_plugin(ctx) is None
    assert find_chronicle_plugin(ctx) is None
    assert lm_bridge.detect_lm(ctx) is None

    # Ready requires actual memory_engine / capabilities ready.
    uninit = SimpleNamespace(initializer=SimpleNamespace(is_initialized=True, memory_engine=None))
    ctx_uninit = SimpleNamespace(
        get_all_stars=lambda: [
            SimpleNamespace(name="astrbot_plugin_dynamics_memory", activated=True, star_cls=uninit)
        ]
    )
    assert memory_status(ctx_uninit)["ready"] is False
    chr_ok = FakeChronicleCompanion()
    assert chronicle_status(FakeAstrBotContext(chronicle=chr_ok))["ready"] is True


def test_blackjack_room_lock_rate_limiter_prune_and_taste_flush(tmp_path: pathlib.Path):
    async def run():
        bj = Blackjack21Plugin(SimpleNamespace(get_config=lambda: {}), {})
        gid = "grp-concurrent"
        room = Room(group_id=gid, host_id="1", phase="playing")
        room.players["1"] = Player(user_id="1", cards=["♠2"])
        room.players["2"] = Player(user_id="2", cards=["♥3"])
        room.deck = ["♣4", "♦5", "♠6", "♥7"]
        bj.rooms[gid] = room

        async def pick(uid: str, action: str):
            ev = SimpleNamespace(
                get_group_id=lambda: gid,
                get_sender_id=lambda: uid,
                plain_result=lambda t: t,
                bot=None,
            )
            gen = bj.cmd_hit(ev) if action == "hit" else bj.cmd_stand(ev)
            async for _ in gen:
                pass

        await asyncio.gather(pick("1", "stand"), pick("2", "hit"))
        assert room.round_no >= 1
        await bj.terminate()

    asyncio.run(run())

    rl = RateLimiter(max_responses=3, window_seconds=1)
    for i in range(25):
        rl._response_history[f"old-{i}"] = [time.time() - 10]
    rl._last_global_cleanup = 0
    rl.can_respond("fresh")
    assert all(not k.startswith("old-") for k in rl._response_history)

    tm = TasteMemoryStore(data_dir=tmp_path)
    tm.add_likes("u1", ["火锅"])
    tm.flush()
    assert (tmp_path / "taste_memory.json").is_file()


def test_import_time_conflict_degrades_gracefully_at_initialize():
    code = """
from types import SimpleNamespace
from astrbot.core.star.star import star_registry
star_registry.append(SimpleNamespace(name='astrbot_plugin_rollpig', activated=True, star_cls=object()))
import asyncio
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin
ctx = SimpleNamespace(get_registered_star=lambda n: SimpleNamespace(activated=True) if n == 'astrbot_plugin_rollpig' else None, get_config=lambda: {})
plugin = DynamicsGamesPlugin(ctx, {})
asyncio.run(plugin.initialize())
assert plugin._ready is False
assert plugin._legacy_conflicts == ['astrbot_plugin_rollpig']
"""
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PLUGIN_ROOT.parent,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
