"""Batch 3 runtime, lock decoupling, lazy resolution & offload regressions for Dynamics Games.

Covers:
- P1-8 truth_game:
  * Memory retrieval & LLM question generation run OUTSIDE room._lock with timeout,
    so slow retrieval/LLM never blocks concurrent room operations (e.g. leave/end).
  * lm_bridge resolves facade modules lazily on every call so LivingMemory/DynamicsMemory
    hot-reload does not leave stale pre-reload function references.
  * Question bank selection respects asked_history deduplication until exhausted.
- P1-8 what_to_eat:
  * ImageManager resolves relative uploaded_images paths against plugin data / host root
    (fixing engines/what_to_eat subdir mismatch) and blocks path traversal.
  * TasteMemoryStore enforces a recency time window & group scoping on casual feedback
    so stale recommendations are never falsely blacklisted days later.
  * Lazy series defaults inheritance (provider & persona) at call time instead of freezing
    empty/stale values during __init__.
- P1-8 main-loop offload:
  * WebConsole.toggle_module offloads set_module_enabled/save_config to worker thread;
    async summary/scope/search/journal wrappers execute SQLite off the event loop.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_games.core.web_console import GamesWebConsole
from astrbot_plugin_dynamics_games.engines.truth_game.core import (
    flow as truth_flow,
    lm_bridge as truth_lm_bridge,
    memory_enrich as truth_memory,
    question_gen as truth_qgen,
)
from astrbot_plugin_dynamics_games.engines.truth_game.core.room import (
    Player,
    Room,
    RoomManager,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.image_manager import ImageManager
from astrbot_plugin_dynamics_games.engines.what_to_eat.main import WhatToEatPlugin
from astrbot_plugin_dynamics_games.engines.what_to_eat.taste_memory import (
    TasteMemoryStore,
)


# ---------------------------------------------------------------------------
# truth_game: lock-free retrieval + timeout, lazy lm_bridge, asked_history bank dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_8_truth_game_retrieval_runs_outside_room_lock_and_respects_timeout(
    monkeypatch,
):
    lock_state = {"during_enrich": None, "during_build": None}

    mgr = RoomManager(config={"bot_ask_enabled": True, "bot_ask_frequency": 1.0, "min_players": 2})
    room = Room(group_id="g1", host_id="u1", umo="qq:GroupMessage:g1", phase="playing")
    room.players["u1"] = Player(user_id="u1", nickname="甲")
    room.players["u2"] = Player(user_id="u2", nickname="乙")
    mgr.rooms[room.room_key] = room

    async def spy_enrich(context, umo, askee_id, askee_name, memory_k=5, enabled=True, timeout_seconds=5.0):
        lock_state["during_enrich"] = room._lock.locked()
        return ["喜欢猫"]

    async def spy_build(**kwargs):
        lock_state["during_build"] = room._lock.locked()
        return "你最喜欢哪种猫？"

    monkeypatch.setattr(truth_flow, "enrich_for_askee", spy_enrich)
    monkeypatch.setattr(truth_flow, "build_question", spy_build)

    hook = truth_flow.build_advance_hook(
        context=None,
        config={"bot_ask_enabled": True, "bot_ask_frequency": 1.0, "enable_livingmemory": True},
        manager=mgr,
    )
    mgr.advance_hook = hook

    async with room.locked():
        ok, text = await mgr._do_advance(room)
    assert ok is True
    assert text == "你最喜欢哪种猫？"
    assert lock_state["during_enrich"] is False
    assert lock_state["during_build"] is False
    assert room._lock.locked() is False

    # Timeout guard in enrich_for_askee prevents slow search from hanging
    class SlowEngine:
        async def search_memories(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(truth_memory, "detect_lm", lambda ctx: object())
    monkeypatch.setattr(truth_memory, "ensure_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(truth_memory, "get_engine", lambda p: SlowEngine())

    t0 = time.monotonic()
    res = await truth_memory.enrich_for_askee(
        context=object(),
        umo="qq:GroupMessage:g1",
        askee_id="u1",
        askee_name="甲",
        timeout_seconds=0.1,
    )
    assert res == []
    assert (time.monotonic() - t0) < 1.0


@pytest.mark.asyncio
async def test_p1_8_truth_game_cancelled_reacquire_does_not_release_other_tasks_lock():
    room = Room(group_id="g_cancel", host_id="u1", umo="qq:GroupMessage:g_cancel", phase="playing")
    a_released = asyncio.Event()
    b_acquired = asyncio.Event()
    b_allow_exit = asyncio.Event()
    a_io_done = asyncio.Event()

    async def task_a():
        async with room.locked():
            async with room.unlocked_for_io():
                a_released.set()
                await b_acquired.wait()
                a_io_done.set()
            # Unreachable when cancelled during reacquire

    async def task_b():
        await a_released.wait()
        async with room.locked():
            b_acquired.set()
            await b_allow_exit.wait()
            # Lock MUST still be owned by task_b (not prematurely released by cancelled task_a!)
            assert room._lock.locked() is True
            assert room._lock_owner is asyncio.current_task()

    ta = asyncio.create_task(task_a())
    tb = asyncio.create_task(task_b())
    await a_io_done.wait()
    # Yield briefly so task_a enters finally: await self._lock.acquire() while task_b holds _lock
    await asyncio.sleep(0.02)
    ta.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ta

    # Task B MUST still hold the lock cleanly without RuntimeError
    assert room._lock.locked() is True
    assert room._lock_owner is tb
    b_allow_exit.set()
    await tb
    assert room._lock.locked() is False
    assert room._lock_owner is None


def test_p1_8_truth_game_lm_bridge_lazy_resolution_survives_hot_reload(monkeypatch):
    mod_name = "astrbot_plugin_dynamics_memory.api.public"
    inst_v1 = SimpleNamespace(version=1, _terminating=False, _closed=False)
    inst_v2 = SimpleNamespace(version=2, _terminating=False, _closed=False)

    mod_v1 = types.ModuleType(mod_name)
    mod_v1.get_active_plugin = lambda: inst_v1
    monkeypatch.setitem(sys.modules, mod_name, mod_v1)
    assert truth_lm_bridge.detect_lm(None) is inst_v1

    # Simulate LivingMemory/DynamicsMemory hot-reload replacing the module in sys.modules
    mod_v2 = types.ModuleType(mod_name)
    mod_v2.get_active_plugin = lambda: inst_v2
    monkeypatch.setitem(sys.modules, mod_name, mod_v2)
    assert truth_lm_bridge.detect_lm(None) is inst_v2


@pytest.mark.asyncio
async def test_p1_8_truth_game_bank_dedup_respects_asked_history(monkeypatch):
    small_bank = ["问题A", "问题B", "问题C"]
    monkeypatch.setattr(truth_qgen, "load_bank", lambda kind: list(small_bank))

    asked: list[str] = []
    for _ in range(3):
        q = await truth_qgen.build_question(
            context=None,
            umo="qq:GroupMessage:g1",
            config={"question_source": "bank_only"},
            asked_history=asked,
        )
        assert q not in asked
        asked.append(q)
    assert set(asked) == set(small_bank)


# ---------------------------------------------------------------------------
# what_to_eat: uploaded_images path resolution, taste_memory window, lazy inheritance
# ---------------------------------------------------------------------------


def test_p1_8_what_to_eat_uploaded_images_resolves_host_root_and_blocks_traversal(
    tmp_path: Path, monkeypatch
):
    host_root = tmp_path / "astrbot_plugin_dynamics_games"
    engine_dir = host_root / "engines" / "what_to_eat"
    engine_dir.mkdir(parents=True)
    # File uploaded by AstrBot lives under host plugin root's files/, NOT engines/what_to_eat/files/
    uploaded_file = host_root / "files" / "uploads" / "麻辣烫.jpg"
    uploaded_file.parent.mkdir(parents=True)
    uploaded_file.write_bytes(b"IMG")

    mgr = ImageManager(
        {"uploaded_images": ["files/uploads/麻辣烫.jpg", "../../outside_secret.jpg"]},
        str(engine_dir),
    )
    resolved = mgr.get_random_image("麻辣烫")
    assert resolved is not None
    assert Path(resolved).resolve() == uploaded_file.resolve()
    assert mgr.get_random_image("outside_secret") is None
    # Direct traversal path MUST return "" rather than falling back to plugin_dir/../../...
    assert mgr._get_full_path("../../etc/passwd") == ""
    assert mgr._get_full_path("../../../outside_secret.jpg") == ""
    assert "outside_secret" not in mgr.config_images


def test_p1_8_what_to_eat_taste_memory_feedback_enforces_time_window_and_group(
    tmp_path: Path,
):
    store = TasteMemoryStore(
        enabled=True,
        data_dir=tmp_path,
        learn_from_message=True,
        feedback_window_seconds=1800.0,
    )
    store.record_recommendation("u1", "黄焖鸡米饭", group_id="g1")
    rec_ts = float(store.get_profile("u1").recent[-1]["ts"])

    # 1) Feedback 3 hours later (outside 30-min window) must NOT blacklist the old recommendation
    stale_res = store.learn_from_message("u1", "太难吃了", group_id="g1", now=rec_ts + 10800.0)
    assert stale_res["feedback"] == []
    assert not store.get_profile("u1").is_forbidden("黄焖鸡米饭")

    # 2) Feedback from a DIFFERENT group within window must NOT cross-apply
    cross_res = store.learn_from_message("u1", "太难吃了", group_id="g2", now=rec_ts + 60.0)
    assert cross_res["feedback"] == []
    assert not store.get_profile("u1").is_forbidden("黄焖鸡米饭")

    # 3) Feedback in same group within window DOES apply properly
    fresh_res = store.learn_from_message("u1", "很好吃", group_id="g1", now=rec_ts + 120.0)
    assert fresh_res["feedback"] == ["+黄焖鸡米饭"]


@pytest.mark.asyncio
async def test_p1_8_what_to_eat_lazy_series_inheritance_after_init(tmp_path: Path):
    # At init time, chat_dynamics is NOT loaded yet
    stars_holder = {"stars": {}}

    persona_mgr = SimpleNamespace(
        get_persona=AsyncMock(
            side_effect=lambda pid: SimpleNamespace(system_prompt=f"PROMPT:{pid}")
        ),
        get_default_persona_v3=AsyncMock(return_value={"prompt": "PROMPT:default"}),
    )
    ctx = SimpleNamespace(
        get_all_stars=lambda: stars_holder["stars"],
        get_provider_by_id=lambda pid: SimpleNamespace(id=pid) if pid == "late-reply-prov" else None,
        persona_manager=persona_mgr,
    )
    wte = WhatToEatPlugin(
        ctx,
        {"llm_enabled": True, "llm_provider_id": "", "llm_persona_id": "", "taste_memory_enabled": False},
    )
    # Raw explicit fields remain empty (not frozen at init!)
    assert wte.persona_llm.provider_id == ""
    assert wte.persona_llm.persona_id == ""

    # Later, chat_dynamics loads and provides shared series defaults
    class LateChatOwner:
        def get_series_shared_config(self):
            return {"reply_provider": "late-reply-prov", "persona_id": "late-series-persona"}

    stars_holder["stars"] = {
        "chat": SimpleNamespace(
            name="astrbot_plugin_chat_dynamics",
            activated=True,
            star_cls=LateChatOwner(),
        )
    }

    resolved_pid = await wte.persona_llm._resolve_provider_id("qq:GroupMessage:1")
    assert resolved_pid == "late-reply-prov"
    sys_prompt = await wte.persona_llm.resolve_system_prompt("qq:GroupMessage:1")
    assert "PROMPT:late-series-persona" in sys_prompt


# ---------------------------------------------------------------------------
# WebConsole toggle_module offloads save_config to worker thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_8_web_console_toggle_module_offloads_to_thread():
    loop_tid = threading.get_ident()
    save_threads: list[int] = []

    def fake_set_module_enabled(key: str, enabled: bool):
        save_threads.append(threading.get_ident())

    fake_plugin = SimpleNamespace(
        set_module_enabled=fake_set_module_enabled,
        module_enabled=lambda k: True,
        module_states=lambda: {"rollpig": True},
        engines={},
        context=None,
    )
    console = GamesWebConsole(fake_plugin)
    console._body = AsyncMock(return_value={"key": "rollpig", "enabled": True})
    console._engine_runtime_snapshot = lambda: []

    res = await console.toggle_module()
    assert res["key"] == "rollpig"
    assert len(save_threads) == 1 and save_threads[0] != loop_tid
