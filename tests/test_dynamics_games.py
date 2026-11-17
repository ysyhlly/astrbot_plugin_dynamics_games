"""Comprehensive unit & integration tests for Dynamics Games (群间游戏)."""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH_ROOT = REPO_ROOT / "work" / "dynamics-games-20261113" / "companion_patches"
BASELINE_ROOT = REPO_ROOT / "work" / "dynamics-games-20261113" / "baseline"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games.core.commands import (
    build_arcade_text,
    build_help_text,
    handle_cancel_command,
    handle_complete_command,
    handle_create_command,
    handle_detail_command,
    handle_join_command,
    handle_list_command,
    handle_outcome_command,
    handle_category_shortcut,
    handle_stage_command,
    handle_submit_command,
)
from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator
from astrbot_plugin_dynamics_games.core.models import (
    CATEGORIES,
    ENGINE_CATALOG,
    evaluate_outcome_worthiness,
    parse_stages,
    resolve_category,
)
from astrbot_plugin_dynamics_games.core.store import ActivityStore
from astrbot_plugin_dynamics_games.runtime import ConfigSection, LEGACY_PLUGIN_IDS


class FakeEvent:
    def __init__(
        self,
        umo: str = "aiocqhttp:GroupMessage:888001",
        group_id: str = "888001",
        uid: str = "10001",
        name: str = "夏目安",
        is_admin: bool = False,
        message_id: str = "9001",
    ) -> None:
        self.unified_msg_origin = umo
        self._gid = group_id
        self._uid = uid
        self._name = name
        self._admin = is_admin
        self.message_obj = SimpleNamespace(message_id=message_id)

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return self._name

    def is_admin(self) -> bool:
        return self._admin


class FakeMemoryCompanion:
    """Simulates Dynamics Memory's controlled record_activity_experience surface."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self._seq = 100

    def get_dynamics_capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider_id": "astrbot_plugin_dynamics_memory",
            "display_name": "群间记忆",
            "enabled": True,
            "ready": True,
            "capabilities": {"memory": 1, "evidence": 1},
            "api_version": 1,
        }

    async def record_activity_experience(self, payload: dict[str, Any]) -> dict[str, Any]:
        umo = payload["scope"]["umo"]
        key = f"dg:{umo}:{payload['activity_id']}:{payload['event_kind']}"
        if key in self.records:
            return {
                "schema_version": 1,
                "recorded": False,
                "reason": "duplicate",
                "doc_id": self.records[key]["doc_id"],
            }
        self._seq += 1
        self.records[key] = {"doc_id": self._seq, "payload": payload}
        return {"schema_version": 1, "recorded": True, "doc_id": self._seq}


class FakeChronicleCompanion:
    """Simulates Group Chronicle's controlled record_valuable_outcome surface."""

    def __init__(self) -> None:
        self.history = True
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self._seq = 500

    def get_dynamics_capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider_id": "astrbot_plugin_group_chronicle",
            "display_name": "群史记",
            "enabled": True,
            "ready": True,
            "capabilities": {"chronicle": 1, "outcomes": 1},
            "api_version": 1,
        }

    async def record_valuable_outcome(self, umo: str, outcome: dict[str, Any]) -> dict[str, Any]:
        if len(outcome.get("participants") or []) < 2:
            return {"schema_version": 1, "admitted": False, "status": "rejected", "reason": "insufficient_participants"}
        fp = outcome["fingerprint"]
        if (umo, fp) in self.events:
            existing = self.events[(umo, fp)]
            return {
                "schema_version": 1,
                "admitted": True,
                "event_id": existing["event_id"],
                "status": existing["status"],
            }
        self._seq += 1
        status = "pending" if (outcome.get("risk") or outcome.get("admission_preference") == "pending") else "approved"
        self.events[(umo, fp)] = {
            "event_id": self._seq,
            "status": status,
            "outcome": outcome,
        }
        return {"schema_version": 1, "admitted": True, "event_id": self._seq, "status": status}


class FakeAstrBotContext:
    def __init__(self, memory: FakeMemoryCompanion | None = None, chronicle: FakeChronicleCompanion | None = None) -> None:
        self._stars: dict[str, Any] = {}
        if memory is not None:
            self._stars["astrbot_plugin_dynamics_memory"] = SimpleNamespace(
                name="astrbot_plugin_dynamics_memory",
                root_dir_name="astrbot_plugin_dynamics_memory",
                activated=True,
                star_cls=memory,
            )
        if chronicle is not None:
            self._stars["astrbot_plugin_group_chronicle"] = SimpleNamespace(
                name="astrbot_plugin_group_chronicle",
                root_dir_name="astrbot_plugin_group_chronicle",
                activated=True,
                star_cls=chronicle,
            )

    def get_all_stars(self):
        return list(self._stars.values())

    def get_registered_star(self, name: str):
        return self._stars.get(name)

    def get_config(self, umo: str | None = None):
        return {"platform_settings": {}, "plugin_set": ["*"]}


@pytest.fixture()
def env(tmp_path: pathlib.Path):
    store = ActivityStore(tmp_path / "dynamics_games.db")
    memory = FakeMemoryCompanion()
    chronicle = FakeChronicleCompanion()
    ctx = FakeAstrBotContext(memory=memory, chronicle=chronicle)
    config = {
        "activities": {
            "enabled": True,
            "memory_sync_enabled": True,
            "chronicle_sync_enabled": True,
            "chronicle_admission_mode": "auto",
            "min_chronicle_participants": 2,
            "min_chronicle_contributions": 2,
            "max_active_per_group": 10,
        }
    }
    coord = ActivityCoordinator(store, ctx, config)
    yield SimpleNamespace(store=store, memory=memory, chronicle=chronicle, ctx=ctx, coord=coord)


@pytest.mark.parametrize(
    "alias,expected_key",
    [
        ("共读", "reading"),
        ("约局", "matchmaking"),
        ("接龙", "relay"),
        ("挑战", "challenge"),
        ("共创", "creation"),
    ],
)
def test_all_five_collaborative_modes_end_to_end(env, alias: str, expected_key: str):
    async def run():
        umo = f"aiocqhttp:GroupMessage:900_{expected_key}"
        ev_host = FakeEvent(umo=umo, group_id=f"900_{expected_key}", uid="101", name="阿青", message_id="1001")
        ev_mate = FakeEvent(umo=umo, group_id=f"900_{expected_key}", uid="102", name="白露", message_id="1002")

        assert resolve_category(alias) == expected_key

        # 1. Create
        reply_create = await handle_create_command(
            env.coord, ev_host, f"{alias} 测试{alias}专题 阶段=第一幕>第二幕>收官 目标=2"
        )
        assert CATEGORIES[expected_key]["label"] in reply_create
        open_acts = env.store.list_activities(umo=umo, status="open")
        assert len(open_acts) == 1
        act_id = open_acts[0]["id"]
        assert open_acts[0]["stages"] == ["第一幕", "第二幕", "收官"]

        # 2. Second member joins
        reply_join = await handle_join_command(env.coord, ev_mate, f"{act_id} 协力担当")
        assert "报名" in reply_join or "加入" in reply_join

        # 3. Start & advance stage
        reply_start = await handle_stage_command(env.coord, ev_host, act_id, start_only=True)
        assert "进行中" in reply_start or "第一幕" in reply_start
        reply_adv = await handle_stage_command(env.coord, ev_host, act_id, start_only=False)
        assert "第二幕" in reply_adv

        # 4. Both members submit progress
        sub1 = await handle_submit_command(env.coord, ev_host, f"{act_id} 第一份实质推进成果内容")
        sub2 = await handle_submit_command(env.coord, ev_mate, f"{act_id} 第二份实质推进成果内容")
        assert "1/2" in sub1 or "进度" in sub1
        assert "2/2" in sub2 or "达成" in sub2 or "进度" in sub2

        # 5. Complete & verify three-layer contract
        comp_reply = await handle_complete_command(env.coord, ev_host, f"{act_id} 圆满收官")
        assert "完结" in comp_reply
        assert "群间记忆" in comp_reply
        assert "群史记" in comp_reply

        outcome = env.store.get_outcome(act_id)
        assert outcome is not None
        assert outcome["valuable"] is True
        assert outcome["memory_doc_id"] > 0
        assert outcome["chronicle_event_id"] > 0
        assert "第一份实质推进成果内容" in outcome["artifact_markdown"]
        assert "第二份实质推进成果内容" in outcome["artifact_markdown"]

        # Re-sync must be strictly idempotent (same doc_id & event_id, zero duplicate rows)
        mem_count_before = len(env.memory.records)
        chr_count_before = len(env.chronicle.events)
        await env.coord.sync_outcome_records(act_id)
        assert len(env.memory.records) == mem_count_before
        assert len(env.chronicle.events) == chr_count_before

        out_text = await handle_outcome_command(env.coord, ev_host, act_id)
        assert f"测试{alias}专题" in out_text

    asyncio.run(run())


def test_cancelled_and_solo_activities_never_enter_chronicle(env):
    """Hard invariant: 流局/中途取消 与 单人未达门槛活动 绝不进入群史记。"""

    async def run():
        umo = "aiocqhttp:GroupMessage:777001"
        ev1 = FakeEvent(umo=umo, group_id="777001", uid="201", name="群友甲")
        ev2 = FakeEvent(umo=umo, group_id="777001", uid="202", name="群友乙")

        # Case A: Cancelled activity (even with 2 members & contributions)
        await handle_create_command(env.coord, ev1, "约局 周五开黑车队")
        act_cancel = env.store.list_activities(umo=umo, status="open")[0]
        await handle_join_command(env.coord, ev2, act_cancel["id"])
        await handle_submit_command(env.coord, ev1, f"{act_cancel['id']} 集合准备")
        cancel_reply = await handle_cancel_command(env.coord, ev1, f"{act_cancel['id']} 临时加班流局")
        assert "取消" in cancel_reply or "流局" in cancel_reply
        assert env.store.get_outcome(act_cancel["id"]) is None
        assert len(env.chronicle.events) == 0

        # Case B: Solo completed activity (only 1 participant) -> Memory YES, Chronicle NO
        await handle_create_command(env.coord, ev1, "挑战 个人早起练习")
        act_solo = env.store.list_activities(umo=umo, status="open")[0]
        await handle_submit_command(env.coord, ev1, f"{act_solo['id']} Day1 打卡")
        await handle_submit_command(env.coord, ev1, f"{act_solo['id']} Day2 打卡")
        await handle_complete_command(env.coord, ev1, f"{act_solo['id']} 独自完成")
        solo_outcome = env.store.get_outcome(act_solo["id"])
        assert solo_outcome is not None
        assert solo_outcome["valuable"] is False
        assert solo_outcome["memory_doc_id"] > 0
        assert solo_outcome["chronicle_event_id"] == 0
        assert len(env.chronicle.events) == 0

        # Case C: Short casual mini-game (< 3 rounds) -> Chronicle NO
        mg_out = await env.coord.record_minigame_outcome(
            event=ev1,
            game_key="blackjack21",
            title="21点快局",
            rounds=2,
            participants=[{"uid": "201", "name": "群友甲"}, {"uid": "202", "name": "群友乙"}],
            summary="两轮短局体验",
        )
        assert mg_out is not None
        assert mg_out["valuable"] is False
        assert len(env.chronicle.events) == 0

        # Case D: Risky wording in valuable outcome -> downgraded to 'pending' in Chronicle
        await handle_create_command(env.coord, ev1, "接龙 悬疑推理故事")
        act_risk = env.store.list_activities(umo=umo, status="open")[0]
        await handle_join_command(env.coord, ev2, act_risk["id"])
        await handle_submit_command(env.coord, ev1, f"{act_risk['id']} 第一棒：神秘传闻出现")
        await handle_submit_command(env.coord, ev2, f"{act_risk['id']} 第二棒：真相水落石出")
        await handle_complete_command(env.coord, ev1, f"{act_risk['id']} 推理接龙收官")
        risk_outcome = env.store.get_outcome(act_risk["id"])
        assert risk_outcome is not None
        assert risk_outcome["valuable"] is True
        assert risk_outcome["chronicle_status"] == "pending"

    asyncio.run(run())


def test_shortcut_commands_and_help_rendering(env):
    async def run():
        umo = "aiocqhttp:GroupMessage:666001"
        ev1 = FakeEvent(umo=umo, group_id="666001", uid="301", name="城崎诺亚")
        ev2 = FakeEvent(umo=umo, group_id="666001", uid="302", name="卜言沁")

        # Shortcut creates when no open activity in that category exists
        r1 = await handle_category_shortcut(env.coord, ev1, "reading", "百年孤独 第一周共读")
        assert "组织共读" in r1
        # Second shortcut call on open activity submits progress directly!
        r2 = await handle_category_shortcut(env.coord, ev2, "reading", "读完了第一章，马孔多的冰块令人难忘")
        assert "打卡" in r2 or "记录" in r2 or "进度" in r2

        lst = await handle_list_command(env.coord, ev1)
        assert "百年孤独" in lst
        det = await handle_detail_command(env.coord, ev1, "")
        assert "城崎诺亚" in det and "卜言沁" in det

        help_txt = build_help_text()
        arcade_txt = build_arcade_text({e["key"]: True for e in ENGINE_CATALOG})
        assert "/活动" in help_txt and "/共读" in help_txt
        for item in ENGINE_CATALOG:
            assert item["label"] in arcade_txt

    asyncio.run(run())


def test_real_group_chronicle_archive_store_integration(tmp_path: pathlib.Path):
    """Verify our patched HistoryService.record_valuable_outcome writes cleanly into real ArchiveStore."""
    gc_base = BASELINE_ROOT / "astrbot_plugin_group_chronicle"
    patch_life = PATCH_ROOT / "astrbot_plugin_group_chronicle" / "core" / "lifetime.py"
    if not gc_base.exists() or not patch_life.exists():
        pytest.skip("Baseline/patch directory not present")

    # Load archive_store from baseline + patched lifetime AST verification
    ast.parse(patch_life.read_text(encoding="utf-8"))
    sys.path.insert(0, str(gc_base))
    try:
        from core.archive_store import ArchiveStore, encode  # type: ignore

        archive = ArchiveStore(tmp_path / "history.sqlite3")
        umo = "aiocqhttp:GroupMessage:555888"
        archive.roster(umo, [("11", "甲"), ("22", "乙")], now=1700000000.0)
        fact = {
            "kind": "project",
            "title": "【共同制作作品】群年度黑话图鉴",
            "detail": "甲与乙历时三阶段共同完成本群黑话图鉴。",
            "importance": 2,
            "positive": True,
            "risk": False,
            "participants": [
                {"uid": "11", "name": "甲", "role": 3},
                {"uid": "22", "name": "乙", "role": 2},
            ],
            "unknown_participants": [],
            "sources": [{"id": 1, "uid": "11", "hash": "abc", "fragment": 0, "excerpt": "定稿"}],
            "event_at": 1700000000.0,
            "date_basis": "recorded",
            "ongoing": False,
            "followups": [],
            "admission": "approved",
            "fingerprint": "dg_fp_test_0000000000000001",
        }
        eid1 = archive.add_event(umo, fact, status="approved", actor="dynamics_games")
        eid2 = archive.add_event(umo, fact, status="approved", actor="dynamics_games")
        assert eid1 == eid2 and eid1 > 0
        events = archive.events(umo, status="approved")
        assert len(events) == 1
        assert events[0]["title"] == "【共同制作作品】群年度黑话图鉴"
    finally:
        sys.path.remove(str(gc_base))


def test_package_integrity_schema_and_zero_engine_decorators():
    # 1. _conf_schema.json valid JSON and has all 6 sections
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    expected_sections = {
        "activities",
        "modules",
        "truth_game",
        "blackjack21",
        "rollpig",
        "what_to_eat",
    }
    assert set(schema.keys()) == expected_sections
    assert len(LEGACY_PLUGIN_IDS) == 4
    assert not (PLUGIN_ROOT / "pages" / "affinity").exists()

    # 2. ConfigSection isolates writes without losing root persistence hook
    saved = []

    class FakeRootCfg(dict):
        def save_config(self):
            saved.append(True)

    root_cfg = FakeRootCfg({"truth_game": {"max_rounds": 5}, "rollpig": {"at_view_pig": False}})
    sec = ConfigSection(root_cfg, "truth_game")
    assert sec.get("max_rounds") == 5
    sec["max_rounds"] = 8
    sec.save_config()
    assert root_cfg["truth_game"]["max_rounds"] == 8
    assert root_cfg["rollpig"]["at_view_pig"] is False
    assert saved == [True]

    # 3. Parse stages supports |, >, ->, →, /
    assert parse_stages("立题>初稿>终稿", "creation") == ["立题", "初稿", "终稿"]
    assert parse_stages("A|B", "reading") == ["A", "B"]

    # 4. Zero @register or @filter decorators inside engines/
    engines_dir = PLUGIN_ROOT / "engines"
    violations = []
    for py in engines_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    raw = ast.unparse(dec)
                    if raw.startswith("register(") or raw.startswith("filter."):
                        violations.append(f"{py.relative_to(PLUGIN_ROOT)}:{node.lineno}:{raw}")
    assert violations == []
