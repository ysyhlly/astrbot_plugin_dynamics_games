"""Batch 2 storage & transaction atomicity regressions for Dynamics Games.

Covers:
- P1-7: RollPig BoardStore concurrency locking (thread + cross-process lock),
  atomic os.replace retry, no lost updates on concurrent draws, and NEVER
  wiping the daily leaderboard on transient/persistent PermissionError/OSError.
- P1-8: record_minigame_outcome executes dedup check + create + joins +
  contributions + completion in a single SQLite write transaction so mid-flight
  failures roll back completely with zero orphan activities, and concurrent
  duplicate dedup_key calls stay strictly idempotent.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator
from astrbot_plugin_dynamics_games.core.store import ActivityStore
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.models import DailyBoard, Pig
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.repository import PigRepository
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.service import RollPigService
from astrbot_plugin_dynamics_games.engines.rollpig.rollpig.storage import BoardStore


# ---------------------------------------------------------------------------
# P1-7: RollPig BoardStore locking, retry & never wiping on OSError
# ---------------------------------------------------------------------------


def _make_pig(i: int) -> Pig:
    return Pig(id=f"p{i}", name=f"小猪{i}", description=f"desc{i}", analysis=f"an{i}")


def test_p1_7_concurrent_draws_preserve_all_users_without_wipe(tmp_path: Path):
    catalog = tmp_path / "pig.json"
    pigs_raw = [_make_pig(i).to_mapping() for i in range(1, 11)]
    catalog.write_text(json.dumps(pigs_raw, ensure_ascii=False), encoding="utf-8")
    board_path = tmp_path / "daily_board.json"
    logger = logging.getLogger("test.rollpig.concurrent")

    repo = PigRepository(catalog, tmp_path / "img", logger)

    def draw_for_user(i: int) -> tuple[str, str]:
        # Intentionally construct independent BoardStore instances pointing at the same file
        store = BoardStore(board_path, logger)
        svc = RollPigService(
            store,
            repo,
            chooser=lambda seq: seq[i % len(seq)],
            clock=lambda: date(2026, 11, 25),
        )
        pig = svc.draw(f"user_{i}")
        assert pig is not None
        return f"user_{i}", pig.id

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(draw_for_user, range(30)))

    final_store = BoardStore(board_path, logger)
    board = final_store.load("2026-11-25")
    assert len(board.records) == 30
    for uid, pid in results:
        assert board.get(uid) is not None
        assert board.get(uid).id == pid


def test_p1_7_permission_error_never_wipes_existing_board_and_retries_transient(
    tmp_path: Path, monkeypatch
):
    board_path = tmp_path / "daily_board.json"
    logger = logging.getLogger("test.rollpig.perm")
    store = BoardStore(board_path, logger)

    # Seed an existing non-empty daily board
    seed = DailyBoard(day="2026-11-25", records={"u1": _make_pig(1), "u2": _make_pig(2)})
    store.save(seed)
    original_bytes = board_path.read_bytes()

    # 1) Transient PermissionError on read -> retries and returns intact board
    orig_read_text = Path.read_text
    read_attempts = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        if self == board_path:
            read_attempts["n"] += 1
            if read_attempts["n"] <= 2:
                raise PermissionError(13, "Sharing violation", str(self))
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    loaded = store.load("2026-11-25")
    assert read_attempts["n"] == 3
    assert set(loaded.records.keys()) == {"u1", "u2"}

    # 2) Persistent PermissionError on read -> MUST raise, NEVER wipe on-disk JSON!
    def always_perm_error(self, *args, **kwargs):
        if self == board_path:
            raise PermissionError(13, "Locked by AV", str(self))
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", always_perm_error)
    with pytest.raises(PermissionError):
        store.load("2026-11-25")

    # Restore read_text and verify the original file is completely untouched!
    monkeypatch.setattr(Path, "read_text", orig_read_text)
    assert board_path.read_bytes() == original_bytes
    reloaded = store.load("2026-11-25")
    assert set(reloaded.records.keys()) == {"u1", "u2"}

    # 3) Transient PermissionError on os.replace -> retries cleanly and cleans no orphan tmp
    orig_replace = os.replace
    replace_attempts = {"n": 0}

    def flaky_replace(src, dst):
        if Path(dst) == board_path:
            replace_attempts["n"] += 1
            if replace_attempts["n"] <= 2:
                raise PermissionError(13, "Access denied during replace")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    reloaded.put("u3", _make_pig(3))
    store.save(reloaded)
    assert replace_attempts["n"] == 3
    assert set(store.load("2026-11-25").records.keys()) == {"u1", "u2", "u3"}
    assert list(tmp_path.glob("*.tmp")) == []


def test_p1_7_corrupt_json_backs_up_and_recovers(tmp_path: Path):
    board_path = tmp_path / "daily_board.json"
    board_path.write_text("{not valid json", encoding="utf-8")
    logger = logging.getLogger("test.rollpig.corrupt")
    store = BoardStore(board_path, logger)

    board = store.load("2026-11-25")
    assert board.records == {}
    backup = tmp_path / "daily_board.json.corrupt"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "{not valid json"


# ---------------------------------------------------------------------------
# P1-8: Atomic single-transaction record_minigame_outcome
# ---------------------------------------------------------------------------


def _make_coordinator(tmp_path: Path) -> ActivityCoordinator:
    store = ActivityStore(tmp_path / "games.sqlite3")
    coord = ActivityCoordinator(context=None, config={}, store=store)
    coord.sync_outcome_records = AsyncMock(return_value="synced")
    return coord


def test_p1_8_record_minigame_outcome_single_write_txn_and_dedup(tmp_path: Path, monkeypatch):
    coord = _make_coordinator(tmp_path)
    store = coord.store

    write_txns = 0
    orig_conn = store.connection

    def counting_conn(*, write: bool = False):
        nonlocal write_txns
        if write:
            write_txns += 1
        return orig_conn(write=write)

    monkeypatch.setattr(store, "connection", counting_conn)

    umo = "aiocqhttp:GroupMessage:10086"
    participants = [
        {"uid": "u1", "name": "Alice"},
        {"uid": "u2", "name": "Bob"},
        {"uid": "u3", "name": "Carol"},
    ]
    contribs = [
        {"uid": "u1", "name": "Alice", "content": "第一回合出题", "kind": "round"},
        {"uid": "u2", "name": "Bob", "content": "第二回合猜中", "kind": "round"},
    ]

    try:
        outcome = asyncio.run(
            coord.record_minigame_outcome(
                umo=umo,
                game_key="guess_word",
                title="你画我猜第1局",
                rounds=3,
                participants=participants,
                summary="三人合作完成三回合猜词",
                extra_contributions=contribs,
                dedup_key="guess_word:room_42",
            )
        )
        assert outcome is not None
        # Entire archival (dedup + create + joins + contributions + completion + dedup insert) = 1 write txn
        assert write_txns == 1
        assert outcome["stats"]["participant_count"] == 3
        assert outcome["stats"]["contribution_count"] == 2
        assert coord.sync_outcome_records.await_count == 1

        # Re-submitting with the same dedup_key (even concurrently) returns the same outcome with 0 duplicates
        async def dup_burst():
            return await asyncio.gather(
                *[
                    coord.record_minigame_outcome(
                        umo=umo,
                        game_key="guess_word",
                        title="你画我猜第1局",
                        rounds=3,
                        participants=participants,
                        summary="三人合作完成三回合猜词",
                        extra_contributions=contribs,
                        dedup_key="guess_word:room_42",
                    )
                    for _ in range(5)
                ]
            )

        dup_results = asyncio.run(dup_burst())
        assert all(r is not None and r["activity_id"] == outcome["activity_id"] for r in dup_results)
        assert coord.sync_outcome_records.await_count == 1
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM minigame_dedup").fetchone()[0] == 1
    finally:
        store.close()


def test_p1_8_record_minigame_outcome_rolls_back_completely_on_mid_flight_error(
    tmp_path: Path, monkeypatch
):
    coord = _make_coordinator(tmp_path)
    store = coord.store

    def boom_save_completion(*args, **kwargs):
        raise RuntimeError("simulated disk full during completion")

    monkeypatch.setattr(store, "save_completion", boom_save_completion)

    try:
        with pytest.raises(RuntimeError, match="simulated disk full"):
            asyncio.run(
                coord.record_minigame_outcome(
                    umo="aiocqhttp:GroupMessage:20099",
                    game_key="guess_word",
                    title="故障回滚局",
                    rounds=2,
                    participants=[
                        {"uid": "u1", "name": "Alice"},
                        {"uid": "u2", "name": "Bob"},
                    ],
                    summary="测试原子回滚",
                    dedup_key="rollback_key_1",
                )
            )

        # ZERO orphan active activities, participants, contributions, outcomes, or dedup keys!
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM minigame_dedup").fetchone()[0] == 0
    finally:
        store.close()
