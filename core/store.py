"""ACID SQLite WAL store for group collaborative activities, progress, and outcomes."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from .models import (
    CATEGORIES, STATUS_LABELS, build_artifact_markdown,
    evaluate_outcome_worthiness, parse_stages,
)


class ActivityConflict(ValueError):
    """The activity changed after the Dashboard loaded its details."""


def check_activity_revision(
    activity: dict[str, Any], expected_revision: int | None
) -> None:
    if expected_revision is not None and activity["revision"] != expected_revision:
        raise ActivityConflict("活动已有更新，请刷新后重试。")


class ActivityStore:
    """Thread-safe SQLite store isolated per group UMO."""

    _locks: dict[str, threading.RLock] = {}

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self._locks.setdefault(str(self.path.resolve()), threading.RLock())
        with self.connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(
                    f"Unsupported dynamics_games DB schema version: {version}"
                )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS seq (
                    name TEXT PRIMARY KEY,
                    val INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS activities (
                    id TEXT PRIMARY KEY,
                    umo TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    creator_uid TEXT NOT NULL,
                    creator_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'recruiting',
                    stages_json TEXT NOT NULL,
                    stage_index INTEGER NOT NULL DEFAULT 0,
                    target_count INTEGER NOT NULL DEFAULT 5,
                    min_participants INTEGER NOT NULL DEFAULT 2,
                    max_participants INTEGER NOT NULL DEFAULT 30,
                    closing_note TEXT NOT NULL DEFAULT '',
                    memory_doc_id INTEGER NOT NULL DEFAULT 0,
                    chronicle_event_id INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_activities_umo_status
                    ON activities(umo, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS participants (
                    activity_id TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role_note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'joined',
                    contribution_count INTEGER NOT NULL DEFAULT 0,
                    joined_at REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    PRIMARY KEY(activity_id, uid)
                );

                CREATE TABLE IF NOT EXISTS contributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    name TEXT NOT NULL,
                    stage_index INTEGER NOT NULL DEFAULT 0,
                    stage_name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'checkin',
                    content TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_contributions_activity
                    ON contributions(activity_id, id ASC);

                CREATE TABLE IF NOT EXISTS outcomes (
                    activity_id TEXT PRIMARY KEY,
                    umo TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    artifact_markdown TEXT NOT NULL,
                    valuable INTEGER NOT NULL DEFAULT 0,
                    worthiness_reason TEXT NOT NULL DEFAULT '',
                    importance INTEGER NOT NULL DEFAULT 1,
                    participants_json TEXT NOT NULL DEFAULT '[]',
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    memory_status TEXT NOT NULL DEFAULT 'pending',
                    memory_doc_id INTEGER NOT NULL DEFAULT 0,
                    chronicle_status TEXT NOT NULL DEFAULT 'skipped',
                    chronicle_event_id INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outcomes_umo
                    ON outcomes(umo, created_at DESC);
                CREATE TABLE IF NOT EXISTS truth_room_outcomes (
                    room_id TEXT PRIMARY KEY,
                    activity_id TEXT NOT NULL UNIQUE REFERENCES outcomes(activity_id)
                );
                CREATE TABLE IF NOT EXISTS truth_room_handoffs (
                    room_id TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );
                PRAGMA user_version = 1;
                """
            )

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if write:
                    conn.commit()
            except BaseException:
                if write and conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def quick_check(self) -> str:
        with self.connection(write=False) as db:
            row = db.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "unknown"

    def _next_id(self, db: sqlite3.Connection, prefix: str = "G") -> str:
        db.execute(
            "INSERT INTO seq(name, val) VALUES('activity', 100) ON CONFLICT(name) DO UPDATE SET val = val + 1"
        )
        val = db.execute("SELECT val FROM seq WHERE name='activity'").fetchone()[0]
        return f"{prefix}{val}"

    @staticmethod
    def _hydrate_activity(
        row: sqlite3.Row | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        stages = json.loads(data.pop("stages_json", "[]") or "[]")
        cat = CATEGORIES.get(data["category"], CATEGORIES["creation"])
        idx = max(0, min(int(data.get("stage_index") or 0), max(0, len(stages) - 1)))
        data["stages"] = stages
        data["stage_index"] = idx
        data["current_stage"] = stages[idx] if stages else "进行中"
        data["category_label"] = cat["label"]
        data["category_icon"] = cat["icon"]
        data["status_label"] = STATUS_LABELS.get(data["status"], data["status"])
        return data

    def create_activity(
        self,
        *,
        umo: str,
        group_id: str,
        category: str,
        title: str,
        description: str = "",
        creator_uid: str,
        creator_name: str,
        stages: list[str] | str | None = None,
        target_count: int | None = None,
        min_participants: int | None = None,
        max_participants: int = 30,
        initial_status: str = "recruiting",
        created_at: float | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        spec = CATEGORIES.get(category)
        if spec is None:
            raise ValueError(f"未知活动类型: {category}")
        clean_title = str(title or "").strip()[:120]
        if not clean_title:
            raise ValueError("活动标题不能为空")
        stage_list = parse_stages(stages, category)
        target = max(
            1, int(target_count if target_count is not None else spec["default_target"])
        )
        min_p = max(
            1,
            int(
                min_participants
                if min_participants is not None
                else spec["min_participants"]
            ),
        )
        max_p = max(min_p, min(200, int(max_participants)))
        now = created_at if created_at is not None else time.time()

        with (nullcontext(_connection) if _connection is not None else self.connection(write=True)) as db:
            act_id = self._next_id(db, "M" if category == "mini_game" else "G")
            db.execute(
                """
                INSERT INTO activities(
                    id, umo, group_id, category, title, description,
                    creator_uid, creator_name, status, stages_json, stage_index,
                    target_count, min_participants, max_participants,
                    created_at, updated_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 1)
                """,
                (
                    act_id,
                    umo,
                    group_id,
                    category,
                    clean_title,
                    str(description or "").strip()[:800],
                    str(creator_uid),
                    str(creator_name or creator_uid)[:80],
                    initial_status,
                    json.dumps(stage_list, ensure_ascii=False),
                    target,
                    min_p,
                    max_p,
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO participants(
                    activity_id, uid, name, role_note, status,
                    contribution_count, joined_at, last_active_at
                ) VALUES (?, ?, ?, '发起人', 'joined', 0, ?, ?)
                """,
                (
                    act_id,
                    str(creator_uid),
                    str(creator_name or creator_uid)[:80],
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (act_id,)
            ).fetchone()
            return self._hydrate_activity(row)  # type: ignore[return-value]

    def get_activity(
        self, activity_id: str, umo: str | None = None
    ) -> dict[str, Any] | None:
        clean_id = str(activity_id or "").strip().lstrip("#").upper()
        if clean_id.isdigit():
            clean_id = f"G{clean_id}"
        with self.connection(write=False) as db:
            if umo:
                row = db.execute(
                    "SELECT * FROM activities WHERE UPPER(id)=? AND umo=?",
                    (clean_id, umo),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM activities WHERE UPPER(id)=?", (clean_id,)
                ).fetchone()
            return self._hydrate_activity(row)

    def resolve_active_activity(
        self, umo: str, activity_id: str = "", category: str = ""
    ) -> dict[str, Any] | None:
        if activity_id.strip():
            return self.get_activity(activity_id, umo=umo)
        with self.connection(write=False) as db:
            if category:
                row = db.execute(
                    """
                    SELECT * FROM activities
                    WHERE umo=? AND category=? AND status IN ('recruiting', 'active')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (umo, category),
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT * FROM activities
                    WHERE umo=? AND category!='mini_game' AND status IN ('recruiting', 'active')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (umo,),
                ).fetchone()
            return self._hydrate_activity(row)

    def list_activities(
        self,
        *,
        umo: str = "",
        category: str = "",
        status: str = "",
        include_minigames: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if umo:
            clauses.append("a.umo = ?")
            params.append(umo)
        if category:
            clauses.append("a.category = ?")
            params.append(category)
        elif not include_minigames:
            clauses.append("a.category != 'mini_game'")
        if status == "open":
            clauses.append("a.status IN ('recruiting', 'active')")
        elif status:
            clauses.append("a.status = ?")
            params.append(status)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(200, int(limit))))
        params.append(max(0, int(offset)))
        with self.connection(write=False) as db:
            rows = db.execute(
                f"""
                SELECT a.*,
                    (SELECT COUNT(*) FROM participants p WHERE p.activity_id=a.id AND p.status='joined') AS participant_count,
                    (SELECT COUNT(*) FROM contributions c WHERE c.activity_id=a.id) AS contribution_count
                FROM activities a
                {where}
                ORDER BY
                    CASE WHEN a.status IN ('recruiting', 'active') THEN 0 ELSE 1 END ASC,
                    a.updated_at DESC, a.id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for r in rows:
                item = self._hydrate_activity(r)
                if item is not None:
                    target = max(1, int(item["target_count"]))
                    cnt = int(item.get("contribution_count") or 0)
                    item["progress_pct"] = min(100, round(cnt * 100 / target))
                    result.append(item)
            return result

    def get_participants(self, activity_id: str) -> list[dict[str, Any]]:
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT * FROM participants
                WHERE activity_id=?
                ORDER BY joined_at ASC
                """,
                (activity_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_contributions(
        self, activity_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT * FROM contributions
                WHERE activity_id=?
                ORDER BY id ASC
                LIMIT ?
                """,
                (activity_id, max(1, min(1000, int(limit)))),
            ).fetchall()
            return [dict(r) for r in rows]

    def join_activity(
        self, activity_id: str, uid: str, name: str, role_note: str = "",
        *, _connection: sqlite3.Connection | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        now = time.time()
        with (nullcontext(_connection) if _connection is not None else self.connection(write=True)) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False, "未找到该活动。", None
            if act["status"] not in ("recruiting", "active"):
                return (
                    False,
                    f"活动当前状态为「{act['status_label']}」，无法报名。",
                    act,
                )

            existing = db.execute(
                "SELECT * FROM participants WHERE activity_id=? AND uid=?",
                (activity_id, str(uid)),
            ).fetchone()
            joined_count = db.execute(
                "SELECT COUNT(*) FROM participants WHERE activity_id=? AND status='joined'",
                (activity_id,),
            ).fetchone()[0]

            if existing and existing["status"] == "joined":
                if role_note.strip():
                    db.execute(
                        "UPDATE participants SET name=?, role_note=?, last_active_at=? WHERE activity_id=? AND uid=?",
                        (
                            str(name or uid)[:80],
                            role_note.strip()[:80],
                            now,
                            activity_id,
                            str(uid),
                        ),
                    )
                    return (
                        True,
                        f"已更新你在《{act['title']}》的分工备注：{role_note.strip()[:80]}",
                        act,
                    )
                return False, f"你已经报名在《{act['title']}》名单中啦。", act

            if joined_count >= int(act["max_participants"]):
                return (
                    False,
                    f"《{act['title']}》报名人数已满（上限 {act['max_participants']} 人）。",
                    act,
                )

            if existing:
                db.execute(
                    """
                    UPDATE participants
                    SET name=?, role_note=COALESCE(NULLIF(?, ''), role_note), status='joined', last_active_at=?
                    WHERE activity_id=? AND uid=?
                    """,
                    (
                        str(name or uid)[:80],
                        role_note.strip()[:80],
                        now,
                        activity_id,
                        str(uid),
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO participants(
                        activity_id, uid, name, role_note, status,
                        contribution_count, joined_at, last_active_at
                    ) VALUES (?, ?, ?, ?, 'joined', 0, ?, ?)
                    """,
                    (
                        activity_id,
                        str(uid),
                        str(name or uid)[:80],
                        role_note.strip()[:80],
                        now,
                        now,
                    ),
                )
            db.execute(
                "UPDATE activities SET updated_at=?, revision=revision+1 WHERE id=?",
                (now, activity_id),
            )
            updated = self._hydrate_activity(
                db.execute(
                    "SELECT * FROM activities WHERE id=?", (activity_id,)
                ).fetchone()
            )
            return True, "报名成功", updated

    def leave_activity(self, activity_id: str, uid: str) -> tuple[bool, str]:
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False, "未找到该活动。"
            if act["status"] not in ("recruiting", "active"):
                return False, "活动已结束，无法退出。"
            existing = db.execute(
                "SELECT * FROM participants WHERE activity_id=? AND uid=? AND status='joined'",
                (activity_id, str(uid)),
            ).fetchone()
            if not existing:
                return False, "你当前不在该活动的参与名单中。"
            db.execute(
                "UPDATE participants SET status='left', last_active_at=? WHERE activity_id=? AND uid=?",
                (now, activity_id, str(uid)),
            )
            db.execute(
                "UPDATE activities SET updated_at=?, revision=revision+1 WHERE id=?",
                (now, activity_id),
            )
            return True, f"已退出活动《{act['title']}》。"

    def update_stage(
        self,
        activity_id: str,
        *,
        start_only: bool = False,
        custom_stage_name: str = "",
        expected_revision: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False, "未找到该活动。", None
            check_activity_revision(act, expected_revision)
            if act["status"] in ("completed", "cancelled"):
                return False, f"活动已处于「{act['status_label']}」状态。", act

            joined_count = db.execute(
                "SELECT COUNT(*) FROM participants WHERE activity_id=? AND status='joined'",
                (activity_id,),
            ).fetchone()[0]

            stages: list[str] = list(act["stages"])
            idx = int(act["stage_index"])

            if act["status"] == "recruiting":
                if joined_count < int(act["min_participants"]):
                    return (
                        False,
                        f"当前仅 {joined_count} 人报名，至少需要 {act['min_participants']} 人才能开启阶段。",
                        act,
                    )
                next_idx = (
                    1
                    if (len(stages) > 1 and stages[0].startswith("招募"))
                    or (len(stages) > 1 and stages[0].startswith("报名"))
                    or (len(stages) > 1 and stages[0].startswith("筹备"))
                    else 0
                )
                if custom_stage_name.strip():
                    if next_idx < len(stages):
                        stages[next_idx] = custom_stage_name.strip()[:40]
                    else:
                        stages.append(custom_stage_name.strip()[:40])
                        next_idx = len(stages) - 1
                db.execute(
                    "UPDATE activities SET status='active', stages_json=?, stage_index=?, updated_at=?, revision=revision+1 WHERE id=?",
                    (
                        json.dumps(stages, ensure_ascii=False),
                        next_idx,
                        now,
                        activity_id,
                    ),
                )
            elif start_only:
                return (
                    False,
                    f"《{act['title']}》已经处于进行中（当前阶段：{act['current_stage']}）。",
                    act,
                )
            else:
                if custom_stage_name.strip():
                    clean_custom = custom_stage_name.strip()[:40]
                    if idx + 1 < len(stages):
                        idx += 1
                        stages[idx] = clean_custom
                    else:
                        stages.append(clean_custom)
                        idx = len(stages) - 1
                else:
                    if idx + 1 >= len(stages):
                        return (
                            False,
                            f"已在最后一个阶段「{act['current_stage']}」，可使用 `/活动 完结` 定稿结项，或 `/活动 阶段 <新阶段名>` 追加阶段。",
                            act,
                        )
                    idx += 1
                db.execute(
                    "UPDATE activities SET stages_json=?, stage_index=?, updated_at=?, revision=revision+1 WHERE id=?",
                    (json.dumps(stages, ensure_ascii=False), idx, now, activity_id),
                )

            updated = self._hydrate_activity(
                db.execute(
                    "SELECT * FROM activities WHERE id=?", (activity_id,)
                ).fetchone()
            )
            return True, f"已进入阶段：{updated['current_stage']}", updated  # type: ignore[index]

    def add_contribution(
        self,
        activity_id: str,
        *,
        uid: str,
        name: str,
        content: str,
        kind: str = "checkin",
        message_id: str = "",
        auto_join: bool = True,
        _connection: sqlite3.Connection | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None, int]:
        clean = str(content or "").strip()[:4000]
        if not clean:
            return False, "提交或打卡内容不能为空。", None, 0

        now = time.time()
        with (nullcontext(_connection) if _connection is not None else self.connection(write=True)) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False, "未找到该活动。", None, 0
            if act["status"] not in ("recruiting", "active"):
                return False, f"活动已「{act['status_label']}」，不再接受提交。", act, 0

            participant = db.execute(
                "SELECT * FROM participants WHERE activity_id=? AND uid=?",
                (activity_id, str(uid)),
            ).fetchone()
            if not participant or participant["status"] != "joined":
                if not auto_join:
                    return False, "请先发送 `/活动 报名` 加入后再提交进度。", act, 0
                joined_count = db.execute(
                    "SELECT COUNT(*) FROM participants WHERE activity_id=? AND status='joined'",
                    (activity_id,),
                ).fetchone()[0]
                if joined_count >= int(act["max_participants"]):
                    return False, "活动人数已满，无法自动加入。", act, 0
                if participant:
                    db.execute(
                        "UPDATE participants SET name=?, status='joined', last_active_at=? WHERE activity_id=? AND uid=?",
                        (str(name or uid)[:80], now, activity_id, str(uid)),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO participants(
                            activity_id, uid, name, role_note, status,
                            contribution_count, joined_at, last_active_at
                        ) VALUES (?, ?, ?, '', 'joined', 0, ?, ?)
                        """,
                        (activity_id, str(uid), str(name or uid)[:80], now, now),
                    )

            # Auto-transition recruiting -> active once min_participants is satisfied
            stages: list[str] = list(act["stages"])
            stage_idx = int(act["stage_index"])
            status = act["status"]
            joined_now = db.execute(
                "SELECT COUNT(*) FROM participants WHERE activity_id=? AND status='joined'",
                (activity_id,),
            ).fetchone()[0]
            if status == "recruiting" and joined_now >= int(act["min_participants"]):
                status = "active"
                if len(stages) > 1 and stage_idx == 0:
                    stage_idx = 1

            stage_name = stages[stage_idx] if stages else "进行中"
            db.execute(
                """
                INSERT INTO contributions(
                    activity_id, umo, uid, name, stage_index, stage_name,
                    kind, content, message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activity_id,
                    act["umo"],
                    str(uid),
                    str(name or uid)[:80],
                    stage_idx,
                    stage_name,
                    kind,
                    clean,
                    str(message_id or ""),
                    now,
                ),
            )
            db.execute(
                """
                UPDATE participants
                SET name=?, contribution_count=contribution_count+1, last_active_at=?
                WHERE activity_id=? AND uid=?
                """,
                (str(name or uid)[:80], now, activity_id, str(uid)),
            )
            db.execute(
                """
                UPDATE activities
                SET status=?, stage_index=?, updated_at=?, revision=revision+1
                WHERE id=?
                """,
                (status, stage_idx, now, activity_id),
            )
            total_contribs = db.execute(
                "SELECT COUNT(*) FROM contributions WHERE activity_id=?",
                (activity_id,),
            ).fetchone()[0]
            updated = self._hydrate_activity(
                db.execute(
                    "SELECT * FROM activities WHERE id=?", (activity_id,)
                ).fetchone()
            )
            return True, "记录成功", updated, int(total_contribs)

    def save_completion(
        self,
        activity_id: str,
        *,
        expected_revision: int,
        closing_note: str,
        summary: str,
        artifact_markdown: str,
        valuable: bool,
        worthiness_reason: str,
        importance: int,
        participants_snapshot: list[dict[str, Any]],
        stats: dict[str, Any],
        completed_at: float | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> bool:
        now = completed_at if completed_at is not None else time.time()
        with (nullcontext(_connection) if _connection is not None else self.connection(write=True)) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False
            stages = act["stages"]
            last_idx = max(0, len(stages) - 1)
            updated = db.execute(
                """
                UPDATE activities
                SET status='completed', stage_index=?, closing_note=?,
                    updated_at=?, completed_at=?, revision=revision+1
                WHERE id=? AND revision=? AND status IN ('recruiting', 'active')
                """,
                (
                    last_idx,
                    str(closing_note or "")[:600],
                    now,
                    now,
                    activity_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                return False
            db.execute(
                """
                INSERT INTO outcomes(
                    activity_id, umo, category, title, summary, artifact_markdown,
                    valuable, worthiness_reason, importance, participants_json,
                    stats_json, memory_status, chronicle_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    activity_id,
                    act["umo"],
                    act["category"],
                    act["title"],
                    summary[:2400],
                    artifact_markdown,
                    1 if valuable else 0,
                    worthiness_reason[:400],
                    int(importance),
                    json.dumps(participants_snapshot, ensure_ascii=False),
                    json.dumps({**stats, "completed_at": now}, ensure_ascii=False),
                    "pending" if valuable else "not_eligible",
                    now,
                    now,
                ),
            )
            return True

    def enqueue_truth_room(self, snapshot: dict[str, Any]) -> None:
        with self.connection(write=True) as db:
            db.execute(
                """
                INSERT OR IGNORE INTO truth_room_handoffs (room_id, snapshot_json, occurred_at)
                SELECT ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM truth_room_outcomes WHERE room_id=?)
                """,
                (snapshot["room_id"], json.dumps(snapshot, ensure_ascii=False),
                 snapshot["occurred_at"], snapshot["room_id"]),
            )

    def pending_truth_rooms(self) -> list[str]:
        with self.connection() as db:
            return [str(row["room_id"]) for row in db.execute(
                "SELECT room_id FROM truth_room_handoffs ORDER BY occurred_at, room_id LIMIT 50"
            )]

    def archive_truth_room(self, room_id: str) -> str | None:
        """Commit the linked activity, complete outcome, and sync handoff together."""
        with self.connection(write=True) as db:
            prior = db.execute(
                "SELECT activity_id FROM truth_room_outcomes WHERE room_id=?", (room_id,)
            ).fetchone()
            if prior:
                return str(prior["activity_id"])
            handoff = db.execute(
                "SELECT snapshot_json FROM truth_room_handoffs WHERE room_id=?", (room_id,)
            ).fetchone()
            if handoff is None:
                return None
            snapshot = json.loads(handoff["snapshot_json"])

            people = snapshot["participants"]
            first = people[0]
            rounds = snapshot["rounds"]
            summary = snapshot["summary_text"]
            act = self.create_activity(
                umo=snapshot["umo"], group_id=snapshot["group_id"],
                category="mini_game", title="真心话局",
                description="内置引擎 truth_game 完赛成果记录",
                creator_uid=first["uid"], creator_name=first["name"],
                target_count=rounds, min_participants=1, max_participants=len(people),
                created_at=snapshot["created_at"],
                initial_status="active", _connection=db,
            )
            aid = act["id"]
            for player in people[1:]:
                ok, msg, _ = self.join_activity(
                    aid, player["uid"], player["name"], _connection=db
                )
                if not ok:
                    raise ValueError(msg)
            ok, msg, act, _ = self.add_contribution(
                aid, uid=first["uid"], name=first["name"], content=summary,
                kind="game_round", _connection=db,
            )
            if not ok or act is None:
                raise ValueError(msg)
            participants = [dict(row) for row in db.execute(
                "SELECT * FROM participants WHERE activity_id=? ORDER BY rowid", (aid,)
            )]
            contributions = [dict(row) for row in db.execute(
                "SELECT * FROM contributions WHERE activity_id=? ORDER BY id", (aid,)
            )]
            valuable, reason, importance = evaluate_outcome_worthiness(
                {**act, "status": "completed"}, participants, contributions,
            )
            if not self.save_completion(
                aid, expected_revision=act["revision"], closing_note=summary,
                summary=summary, artifact_markdown=build_artifact_markdown(
                    act, participants, contributions, closing_note=summary
                ),
                valuable=valuable, worthiness_reason=reason, importance=importance,
                participants_snapshot=participants, stats={
                    "participant_count": len(participants),
                    "contribution_count": len(contributions),
                    "stages_total": len(act["stages"]),
                    "rounds": rounds, "ask_counts": snapshot["ask_counts"],
                    "end_reason": snapshot["end_reason"],
                },
                completed_at=snapshot["occurred_at"], _connection=db,
            ):
                raise RuntimeError("真心话成果归档失败")
            db.execute(
                "INSERT INTO truth_room_outcomes (room_id, activity_id) VALUES (?, ?)",
                (room_id, aid),
            )
            db.execute("DELETE FROM truth_room_handoffs WHERE room_id=?", (room_id,))
            return aid

    def pending_truth_outcomes(self, *, memory: bool, chronicle: bool) -> list[str]:
        with self.connection() as db:
            return [str(row["activity_id"]) for row in db.execute(
                """
                SELECT o.activity_id
                FROM truth_room_outcomes t JOIN outcomes o USING(activity_id)
                WHERE
                    (? AND o.memory_status IN (
                        'pending', 'unavailable', 'error', 'not_initialized',
                        'engine_unavailable', 'source_store_unavailable', 'memory_index_unavailable',
                        'memory_write_failed', 'source_snapshot_changed'
                    )) OR
                    (? AND o.valuable AND o.chronicle_event_id=0
                        AND o.chronicle_status IN ('pending', 'unavailable', 'error'))
                ORDER BY o.updated_at ASC
                LIMIT 50
                """,
                (memory, chronicle),
            )]

    def update_sync_status(
        self,
        activity_id: str,
        *,
        memory_status: str | None = None,
        memory_doc_id: int | None = None,
        chronicle_status: str | None = None,
        chronicle_event_id: int | None = None,
    ) -> None:
        now = time.time()
        with self.connection(write=True) as db:
            if memory_status is not None:
                db.execute(
                    "UPDATE outcomes SET memory_status=?, updated_at=? WHERE activity_id=?",
                    (memory_status, now, activity_id),
                )
            if memory_doc_id is not None:
                db.execute(
                    "UPDATE outcomes SET memory_doc_id=?, updated_at=? WHERE activity_id=?",
                    (int(memory_doc_id), now, activity_id),
                )
                db.execute(
                    "UPDATE activities SET memory_doc_id=?, updated_at=? WHERE id=?",
                    (int(memory_doc_id), now, activity_id),
                )
            if chronicle_status is not None:
                db.execute(
                    "UPDATE outcomes SET chronicle_status=?, updated_at=? WHERE activity_id=?",
                    (chronicle_status, now, activity_id),
                )
            if chronicle_event_id is not None:
                db.execute(
                    "UPDATE outcomes SET chronicle_event_id=?, updated_at=? WHERE activity_id=?",
                    (int(chronicle_event_id), now, activity_id),
                )
                db.execute(
                    "UPDATE activities SET chronicle_event_id=?, updated_at=? WHERE id=?",
                    (int(chronicle_event_id), now, activity_id),
                )

    def cancel_activity(
        self,
        activity_id: str,
        reason: str = "",
        *,
        expected_revision: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False, "未找到该活动。", None
            check_activity_revision(act, expected_revision)
            if act["status"] == "completed":
                return False, "活动已正式完结，不能取消。", act
            if act["status"] == "cancelled":
                return False, "活动已经处于取消状态。", act
            db.execute(
                """
                UPDATE activities
                SET status='cancelled', closing_note=?, updated_at=?, revision=revision+1
                WHERE id=?
                """,
                (str(reason or "流局/手动取消")[:400], now, activity_id),
            )
            updated = self._hydrate_activity(
                db.execute(
                    "SELECT * FROM activities WHERE id=?", (activity_id,)
                ).fetchone()
            )
            return True, f"活动《{act['title']}》已取消（流局不入群史记）。", updated

    def get_outcome(self, activity_id: str) -> dict[str, Any] | None:
        clean_id = str(activity_id or "").strip().lstrip("#").upper()
        if clean_id.isdigit():
            clean_id = f"G{clean_id}"
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM outcomes WHERE UPPER(activity_id)=?", (clean_id,)
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["valuable"] = bool(data.get("valuable"))
            data["participants"] = json.loads(
                data.pop("participants_json", "[]") or "[]"
            )
            data["stats"] = json.loads(data.pop("stats_json", "{}") or "{}")
            cat = CATEGORIES.get(data["category"], CATEGORIES["creation"])
            data["category_label"] = cat["label"]
            data["category_icon"] = cat["icon"]
            return data

    def list_outcomes(
        self, *, umo: str = "", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        window = (max(1, min(200, int(limit))), max(0, int(offset)))
        with self.connection(write=False) as db:
            if umo:
                rows = db.execute(
                    "SELECT * FROM outcomes WHERE umo=? ORDER BY created_at DESC, activity_id DESC LIMIT ? OFFSET ?",
                    (umo, *window),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM outcomes ORDER BY created_at DESC, activity_id DESC LIMIT ? OFFSET ?",
                    window,
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["valuable"] = bool(d.get("valuable"))
                d["participants"] = json.loads(d.pop("participants_json", "[]") or "[]")
                d["stats"] = json.loads(d.pop("stats_json", "{}") or "{}")
                cat = CATEGORIES.get(d["category"], CATEGORIES["creation"])
                d["category_label"] = cat["label"]
                d["category_icon"] = cat["icon"]
                # Omit full markdown in list view to keep payloads snappy
                d["artifact_preview"] = str(d.get("artifact_markdown") or "")[:360]
                d.pop("artifact_markdown", None)
                out.append(d)
            return out

    def stats_summary(self, umo: str = "") -> dict[str, Any]:
        with self.connection(write=False) as db:
            where = "WHERE umo=?" if umo else ""
            args = (umo,) if umo else ()
            open_cnt = db.execute(
                f"SELECT COUNT(*) FROM activities {where + (' AND ' if where else 'WHERE ')} status IN ('recruiting', 'active') AND category!='mini_game'",
                args,
            ).fetchone()[0]
            completed_cnt = db.execute(
                f"SELECT COUNT(*) FROM activities {where + (' AND ' if where else 'WHERE ')} status='completed'",
                args,
            ).fetchone()[0]
            memory_cnt = db.execute(
                f"SELECT COUNT(*) FROM outcomes {where + (' AND ' if where else 'WHERE ')} memory_doc_id > 0",
                args,
            ).fetchone()[0]
            chronicle_cnt = db.execute(
                f"SELECT COUNT(*) FROM outcomes {where + (' AND ' if where else 'WHERE ')} chronicle_event_id > 0",
                args,
            ).fetchone()[0]
            groups = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT umo FROM activities ORDER BY umo ASC"
                ).fetchall()
                if r[0]
            ]
            return {
                "open_activities": int(open_cnt),
                "completed_activities": int(completed_cnt),
                "memory_recorded": int(memory_cnt),
                "chronicle_admitted": int(chronicle_cnt),
                "known_umos": groups,
            }
