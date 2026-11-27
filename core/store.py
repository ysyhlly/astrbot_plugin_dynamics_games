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
    CATEGORIES,
    STATUS_LABELS,
    build_artifact_markdown,
    evaluate_outcome_worthiness,
    parse_stages,
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
        self._conn: sqlite3.Connection | None = None
        self._tx_depth = 0
        with self.connection(write=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
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
                    sync_attempts INTEGER NOT NULL DEFAULT 0,
                    next_sync_at REAL NOT NULL DEFAULT 0,
                    last_sync_error TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS minigame_dedup (
                    umo TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    activity_id TEXT NOT NULL UNIQUE REFERENCES outcomes(activity_id),
                    created_at REAL NOT NULL,
                    PRIMARY KEY(umo, dedup_key)
                );
                PRAGMA user_version = 1;
                """
            )
            act_cols = {
                str(r["name"])
                for r in db.execute("PRAGMA table_info(activities)").fetchall()
            }
            if "stale_reminded_at" not in act_cols:
                db.execute(
                    "ALTER TABLE activities ADD COLUMN stale_reminded_at REAL NOT NULL DEFAULT 0"
                )
            cols = {
                str(r["name"])
                for r in db.execute("PRAGMA table_info(outcomes)").fetchall()
            }
            if "sync_attempts" not in cols:
                db.execute(
                    "ALTER TABLE outcomes ADD COLUMN sync_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "next_sync_at" not in cols:
                db.execute(
                    "ALTER TABLE outcomes ADD COLUMN next_sync_at REAL NOT NULL DEFAULT 0"
                )
            if "last_sync_error" not in cols:
                db.execute(
                    "ALTER TABLE outcomes ADD COLUMN last_sync_error TEXT NOT NULL DEFAULT ''"
                )
            if "poster_path" not in cols:
                db.execute(
                    "ALTER TABLE outcomes ADD COLUMN poster_path TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcomes_sync ON outcomes(next_sync_at, memory_status, chronicle_status)"
            )

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                str(self.path), timeout=30.0, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._ensure_conn()
            started_tx = False
            if write and self._tx_depth == 0:
                conn.execute("BEGIN IMMEDIATE")
                started_tx = True
            if write:
                self._tx_depth += 1
            try:
                yield conn
                if started_tx and conn.in_transaction:
                    conn.execute("COMMIT")
            except BaseException:
                if started_tx and conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
            finally:
                if write:
                    self._tx_depth = max(0, self._tx_depth - 1)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
                    self._tx_depth = 0

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

    def create_activity_checked(
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
        max_active_per_group: int = 5,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Atomically enforce per-group open limit and insert a new activity."""
        limit = max(1, int(max_active_per_group))
        with self.connection(write=True) as db:
            open_cnt = int(
                db.execute(
                    "SELECT COUNT(*) FROM activities WHERE umo=? AND category!='mini_game' AND status IN ('recruiting', 'active')",
                    (umo,),
                ).fetchone()[0]
            )
            if open_cnt >= limit:
                return (
                    False,
                    f"本群已有 {open_cnt} 个进行中的活动（上限 {limit} 个），请先完结或取消旧活动。",
                    None,
                )
            act = self.create_activity(
                umo=umo,
                group_id=group_id,
                category=category,
                title=title,
                description=description,
                creator_uid=creator_uid,
                creator_name=creator_name,
                stages=stages,
                target_count=target_count,
                min_participants=min_participants,
                max_participants=max_participants,
                _connection=db,
            )
            return True, "创建成功", act

    def expire_stale_activities(
        self,
        *,
        max_age_seconds: float,
        umo: str = "",
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Auto-cancel abandoned recruiting/active activities past inactivity TTL."""
        age = float(max_age_seconds)
        if age <= 0:
            return []
        ts = time.time() if now is None else float(now)
        cutoff = ts - age
        clause = "status IN ('recruiting', 'active') AND category!='mini_game' AND updated_at < ?"
        params: tuple[Any, ...] = (cutoff, umo) if umo else (cutoff,)
        if umo:
            clause += " AND umo=?"
        with self.connection(write=False) as db:
            pending = db.execute(
                f"SELECT id FROM activities WHERE {clause}", params
            ).fetchall()
        if not pending:
            return []
        note = "超时未推进，自动过期清理（流局不入群史记）"
        with self.connection(write=True) as db:
            rows = db.execute(
                f"SELECT * FROM activities WHERE {clause}", params
            ).fetchall()
            if not rows:
                return []
            ids = [str(r["id"]) for r in rows]
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE activities SET status='cancelled', closing_note=?, updated_at=?, revision=revision+1 WHERE id IN ({placeholders})",
                (note, ts, *ids),
            )
            expired_rows = db.execute(
                f"SELECT * FROM activities WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            return [
                act
                for act in (self._hydrate_activity(r) for r in expired_rows)
                if act is not None
            ]

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

    def list_activities_needing_reminder(
        self,
        *,
        stale_expire_seconds: float,
        reminder_lead_seconds: float = 86400.0,
        umo: str = "",
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Find open activities within reminder_lead_seconds of stale expiry that haven't been reminded."""
        expire_age = float(stale_expire_seconds or 0)
        lead = float(reminder_lead_seconds or 0)
        if expire_age <= 0 or lead <= 0:
            return []
        ts = time.time() if now is None else float(now)
        warn_cutoff = ts - max(0.0, expire_age - lead)
        expire_cutoff = ts - expire_age
        clause = (
            "status IN ('recruiting', 'active') AND category!='mini_game' "
            "AND stale_reminded_at = 0 AND updated_at <= ? AND updated_at > ?"
        )
        params: list[Any] = [warn_cutoff, expire_cutoff]
        if umo:
            clause += " AND umo=?"
            params.append(umo)
        with self.connection(write=False) as db:
            rows = db.execute(
                f"SELECT * FROM activities WHERE {clause} ORDER BY updated_at ASC LIMIT 30",
                tuple(params),
            ).fetchall()
            return [
                act
                for act in (self._hydrate_activity(r) for r in rows)
                if act is not None
            ]

    def mark_stale_reminded(
        self, activity_id: str, *, now: float | None = None
    ) -> None:
        ts = time.time() if now is None else float(now)
        with self.connection(write=True) as db:
            db.execute(
                "UPDATE activities SET stale_reminded_at=? WHERE id=?",
                (ts, activity_id),
            )

    def update_outcome_poster(self, activity_id: str, poster_path: str) -> None:
        clean = str(poster_path or "").strip()
        with self.connection(write=True) as db:
            db.execute(
                "UPDATE outcomes SET poster_path=?, updated_at=? WHERE activity_id=?",
                (clean, time.time(), activity_id),
            )

    def resolve_active_activity(
        self,
        umo: str,
        activity_id: str = "",
        category: str = "",
        *,
        preferred_uid: str = "",
    ) -> dict[str, Any] | None:
        if activity_id.strip():
            return self.get_activity(activity_id, umo=umo)
        pref = str(preferred_uid or "").strip()
        with self.connection(write=False) as db:
            if pref:
                if category:
                    row = db.execute(
                        """
                        SELECT a.* FROM activities a
                        WHERE a.umo=? AND a.category=? AND a.status IN ('recruiting', 'active')
                          AND EXISTS (
                              SELECT 1 FROM participants p
                              WHERE p.activity_id=a.id AND p.uid=? AND p.status='joined'
                          )
                        ORDER BY a.updated_at DESC LIMIT 1
                        """,
                        (umo, category, pref),
                    ).fetchone()
                else:
                    row = db.execute(
                        """
                        SELECT a.* FROM activities a
                        WHERE a.umo=? AND a.category!='mini_game' AND a.status IN ('recruiting', 'active')
                          AND EXISTS (
                              SELECT 1 FROM participants p
                              WHERE p.activity_id=a.id AND p.uid=? AND p.status='joined'
                          )
                        ORDER BY a.updated_at DESC LIMIT 1
                        """,
                        (umo, pref),
                    ).fetchone()
                if row is not None:
                    return self._hydrate_activity(row)
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

    def withdraw_last_contribution(
        self,
        umo: str,
        uid: str,
        *,
        activity_id: str = "",
        contribution_id: int | None = None,
        is_admin: bool = False,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Withdraw the user's most recent contribution from an open activity."""
        now = time.time()
        clean_aid = str(activity_id or "").strip().lstrip("#").upper()
        if clean_aid.isdigit():
            clean_aid = f"G{clean_aid}"
        with self.connection(write=True) as db:
            if contribution_id is not None:
                row = db.execute(
                    """
                    SELECT c.*, a.title AS act_title, a.status AS act_status
                    FROM contributions c
                    JOIN activities a ON a.id = c.activity_id
                    WHERE c.id=? AND c.umo=?
                    """,
                    (int(contribution_id), umo),
                ).fetchone()
            elif clean_aid:
                row = db.execute(
                    """
                    SELECT c.*, a.title AS act_title, a.status AS act_status
                    FROM contributions c
                    JOIN activities a ON a.id = c.activity_id
                    WHERE c.umo=? AND UPPER(c.activity_id)=? AND c.uid=?
                      AND a.status IN ('recruiting', 'active')
                    ORDER BY c.id DESC LIMIT 1
                    """,
                    (umo, clean_aid, str(uid)),
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT c.*, a.title AS act_title, a.status AS act_status
                    FROM contributions c
                    JOIN activities a ON a.id = c.activity_id
                    WHERE c.umo=? AND c.uid=? AND a.status IN ('recruiting', 'active')
                    ORDER BY c.id DESC LIMIT 1
                    """,
                    (umo, str(uid)),
                ).fetchone()
            if row is None:
                return False, "未找到可撤回的进行中活动打卡/提交记录。", None
            if row["act_status"] not in ("recruiting", "active"):
                return False, "该活动已结束，无法撤回历史记录。", None
            if not is_admin and str(row["uid"]) != str(uid):
                return False, "仅本人或群管理员可撤回该条记录。", None

            rec = dict(row)
            db.execute("DELETE FROM contributions WHERE id=?", (rec["id"],))
            db.execute(
                """
                UPDATE participants
                SET contribution_count = CASE WHEN contribution_count > 0 THEN contribution_count - 1 ELSE 0 END,
                    last_active_at = ?
                WHERE activity_id=? AND uid=?
                """,
                (now, rec["activity_id"], rec["uid"]),
            )
            db.execute(
                "UPDATE activities SET updated_at=?, revision=revision+1 WHERE id=?",
                (now, rec["activity_id"]),
            )
            return (
                True,
                f"↩️ 已撤回你在《{rec['act_title']}》[{rec['activity_id']}] 的最近一条记录。",
                rec,
            )

    def rebind_contribution(
        self,
        umo: str,
        uid: str,
        target_activity_id: str,
        *,
        contribution_id: int | None = None,
        is_admin: bool = False,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Move the user's latest contribution to another open activity in the same group."""
        now = time.time()
        clean_target = str(target_activity_id or "").strip().lstrip("#").upper()
        if clean_target.isdigit():
            clean_target = f"G{clean_target}"
        if not clean_target:
            return False, "请指定要改绑的目标活动编号，例如：`/活动 改绑 G102`。", None

        with self.connection(write=True) as db:
            target_row = db.execute(
                "SELECT * FROM activities WHERE UPPER(id)=? AND umo=?",
                (clean_target, umo),
            ).fetchone()
            target_act = self._hydrate_activity(target_row)
            if target_act is None:
                return False, f"未在本群找到目标活动 `{clean_target}`。", None
            if target_act["status"] not in ("recruiting", "active"):
                return (
                    False,
                    f"目标活动《{target_act['title']}》已处于「{target_act['status_label']}」，无法改绑。",
                    None,
                )

            if contribution_id is not None:
                c_row = db.execute(
                    """
                    SELECT c.*, a.title AS act_title, a.status AS act_status
                    FROM contributions c
                    JOIN activities a ON a.id = c.activity_id
                    WHERE c.id=? AND c.umo=?
                    """,
                    (int(contribution_id), umo),
                ).fetchone()
            else:
                c_row = db.execute(
                    """
                    SELECT c.*, a.title AS act_title, a.status AS act_status
                    FROM contributions c
                    JOIN activities a ON a.id = c.activity_id
                    WHERE c.umo=? AND c.uid=? AND a.status IN ('recruiting', 'active')
                    ORDER BY c.id DESC LIMIT 1
                    """,
                    (umo, str(uid)),
                ).fetchone()
            if c_row is None:
                return False, "未找到你最近可改绑的打卡/提交记录。", None
            if c_row["act_status"] not in ("recruiting", "active"):
                return False, "原活动已结束，无法改绑。", None
            if not is_admin and str(c_row["uid"]) != str(uid):
                return False, "仅本人或群管理员可改绑该条记录。", None
            if str(c_row["activity_id"]).upper() == target_act["id"].upper():
                return (
                    False,
                    f"该条记录已经在目标活动《{target_act['title']}》[{target_act['id']}] 中啦。",
                    None,
                )

            src_aid = str(c_row["activity_id"])
            c_uid = str(c_row["uid"])
            c_name = str(c_row["name"] or c_uid)[:80]

            # Ensure participant exists in target
            p_target = db.execute(
                "SELECT * FROM participants WHERE activity_id=? AND uid=?",
                (target_act["id"], c_uid),
            ).fetchone()
            if not p_target:
                db.execute(
                    """
                    INSERT INTO participants(
                        activity_id, uid, name, role_note, status,
                        contribution_count, joined_at, last_active_at
                    ) VALUES (?, ?, ?, '', 'joined', 1, ?, ?)
                    """,
                    (target_act["id"], c_uid, c_name, now, now),
                )
            else:
                db.execute(
                    """
                    UPDATE participants
                    SET status='joined', contribution_count=contribution_count+1, last_active_at=?
                    WHERE activity_id=? AND uid=?
                    """,
                    (now, target_act["id"], c_uid),
                )
            db.execute(
                """
                UPDATE participants
                SET contribution_count = CASE WHEN contribution_count > 0 THEN contribution_count - 1 ELSE 0 END,
                    last_active_at = ?
                WHERE activity_id=? AND uid=?
                """,
                (now, src_aid, c_uid),
            )
            db.execute(
                """
                UPDATE contributions
                SET activity_id=?, stage_index=?, stage_name=?
                WHERE id=?
                """,
                (
                    target_act["id"],
                    int(target_act["stage_index"]),
                    str(target_act["current_stage"]),
                    int(c_row["id"]),
                ),
            )
            db.execute(
                "UPDATE activities SET updated_at=?, stale_reminded_at=0, revision=revision+1 WHERE id IN (?, ?)",
                (now, src_aid, target_act["id"]),
            )
            return (
                True,
                f"🔀 已将最近一条记录从《{c_row['act_title']}》[{src_aid}] 改绑至《{target_act['title']}》[{target_act['id']}]！",
                {
                    "contribution_id": int(c_row["id"]),
                    "from_activity_id": src_aid,
                    "to_activity_id": target_act["id"],
                    "target_activity": target_act,
                },
            )

    def transfer_creator(
        self,
        activity_id: str,
        *,
        new_creator_uid: str,
        new_creator_name: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Transfer activity creator/owner role to another member."""
        now = time.time()
        clean_uid = str(new_creator_uid or "").strip()
        if not clean_uid:
            return False, "请指定新的发起人（支持 @成员 或填写昵称/UID）。", None
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            act = self._hydrate_activity(row)
            if act is None:
                return False, "未找到该活动。", None
            if act["status"] not in ("recruiting", "active"):
                return False, f"活动已处于「{act['status_label']}」，无法移交发起人。", act

            p_row = db.execute(
                "SELECT * FROM participants WHERE activity_id=? AND uid=?",
                (activity_id, clean_uid),
            ).fetchone()
            if p_row is None:
                # Allow matching by participant nickname
                p_row = db.execute(
                    "SELECT * FROM participants WHERE activity_id=? AND status='joined' AND LOWER(name)=LOWER(?)",
                    (activity_id, clean_uid),
                ).fetchone()
                if p_row is not None:
                    clean_uid = str(p_row["uid"])

            resolved_name = (
                str(new_creator_name or "").strip()
                or (str(p_row["name"]) if p_row is not None else "")
                or clean_uid
            )[:80]
            if str(act["creator_uid"]) == clean_uid:
                return False, f"{resolved_name} 已经是该活动的发起人啦。", act

            if p_row is None:
                db.execute(
                    """
                    INSERT INTO participants(
                        activity_id, uid, name, role_note, status,
                        contribution_count, joined_at, last_active_at
                    ) VALUES (?, ?, ?, '发起人', 'joined', 0, ?, ?)
                    """,
                    (activity_id, clean_uid, resolved_name, now, now),
                )
            else:
                db.execute(
                    """
                    UPDATE participants
                    SET status='joined', name=?, role_note=COALESCE(NULLIF(role_note, ''), '发起人'), last_active_at=?
                    WHERE activity_id=? AND uid=?
                    """,
                    (resolved_name, now, activity_id, clean_uid),
                )

            db.execute(
                "UPDATE activities SET creator_uid=?, creator_name=?, updated_at=?, stale_reminded_at=0, revision=revision+1 WHERE id=?",
                (clean_uid, resolved_name, now, activity_id),
            )
            updated = self._hydrate_activity(
                db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            )
            return (
                True,
                f"🤝 已将《{act['title']}》[{act['id']}] 的发起人移交给 {resolved_name}！",
                updated,
            )

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
                SET status=?, stage_index=?, updated_at=?, stale_reminded_at=0, revision=revision+1
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

    @staticmethod
    def _hydrate_outcome_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["valuable"] = bool(data.get("valuable"))
        participants = json.loads(data.pop("participants_json", "[]") or "[]")
        stats = json.loads(data.pop("stats_json", "{}") or "{}")
        data["participants"] = participants
        data["stats"] = stats
        data["participant_count"] = int(
            stats.get("participant_count")
            if isinstance(stats, dict) and stats.get("participant_count") is not None
            else (len(participants) if isinstance(participants, list) else 0)
        )
        data["contribution_count"] = int(
            stats.get("contribution_count") or 0
            if isinstance(stats, dict)
            else 0
        )
        cat = CATEGORIES.get(data["category"], CATEGORIES["creation"])
        data["category_label"] = cat["label"]
        data["category_icon"] = cat["icon"]
        return data

    def record_minigame_outcome_atomic(
        self,
        *,
        umo: str,
        group_id: str,
        game_key: str,
        title: str,
        rounds: int,
        participants: list[dict[str, Any]],
        summary: str,
        category: str = "mini_game",
        extra_contributions: list[dict[str, Any]] | None = None,
        dedup_key: str = "",
        min_chronicle_participants: int = 2,
        min_chronicle_contributions: int = 1,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically archive a mini-game outcome in a single SQLite write transaction.

        Executes dedup check, activity creation, participant joins, contribution
        inserts, completion/outcome persistence, and dedup key registration within
        one ``BEGIN IMMEDIATE`` transaction so mid-flight errors roll back completely
        with zero orphan ``active`` activities or partial contributions.

        Returns:
            ``(outcome_dict, created)`` where ``created`` is ``False`` on dedup hit.
        """
        clean_dedup = str(dedup_key or "").strip()[:240]
        clean_summary = str(summary or "")
        expected_contribs = max(
            1,
            len(
                [
                    c
                    for c in (extra_contributions or [])[:50]
                    if str(c.get("content") or "").strip()
                ]
            )
            or 1,
        )
        now = time.time()
        with self.connection(write=True) as db:
            if clean_dedup:
                prior = db.execute(
                    "SELECT activity_id FROM minigame_dedup WHERE umo=? AND dedup_key=?",
                    (umo, clean_dedup),
                ).fetchone()
                if prior is not None:
                    row = db.execute(
                        "SELECT * FROM outcomes WHERE activity_id=?",
                        (prior["activity_id"],),
                    ).fetchone()
                    hydrated = self._hydrate_outcome_row(row)
                    if hydrated is not None:
                        return hydrated, False
                # Backward-compatible dedup check for pre-migration outcomes
                legacy_rows = db.execute(
                    "SELECT * FROM outcomes WHERE umo=? AND title=? ORDER BY created_at DESC, activity_id DESC LIMIT 50",
                    (umo, str(title or "")[:120]),
                ).fetchall()
                for row in legacy_rows:
                    hydrated = self._hydrate_outcome_row(row)
                    if hydrated is not None and int(
                        (hydrated.get("stats") or {}).get("contribution_count") or 0
                    ) == expected_contribs:
                        return hydrated, False

            creator = (
                participants[0]
                if participants
                else {"uid": "system", "name": "群成员"}
            )
            creator_uid = str(creator.get("uid") or "system")
            creator_name = str(creator.get("name") or "群成员")
            max_people = max(30, len(participants) + 1)
            act = self.create_activity(
                umo=umo,
                group_id=group_id or umo.rsplit(":", 1)[-1],
                category=category,
                title=title,
                description=f"内置引擎 {game_key} 完赛/导出成果记录",
                creator_uid=creator_uid,
                creator_name=creator_name,
                target_count=max(1, int(rounds)),
                min_participants=1,
                max_participants=max_people,
                created_at=now,
                initial_status="active",
                _connection=db,
            )
            aid = act["id"]

            seen_uids = {creator_uid}
            for p in participants[1:]:
                p_uid = str(p.get("uid") or "").strip()
                if not p_uid or p_uid in seen_uids:
                    continue
                seen_uids.add(p_uid)
                p_name = str(p.get("name") or p_uid)
                ok, msg, _ = self.join_activity(aid, p_uid, p_name, _connection=db)
                if not ok:
                    raise ValueError(msg)

            added_any = False
            if extra_contributions:
                for item in extra_contributions[:50]:
                    item_content = str(item.get("content") or "")[:1200]
                    if not item_content.strip():
                        continue
                    ok, msg, updated_act, _ = self.add_contribution(
                        aid,
                        uid=str(item.get("uid") or creator_uid),
                        name=str(item.get("name") or creator_name),
                        content=item_content,
                        kind=str(item.get("kind") or "work"),
                        _connection=db,
                    )
                    if not ok or updated_act is None:
                        raise ValueError(msg)
                    act = updated_act
                    added_any = True

            if not added_any:
                fallback_content = clean_summary[:1200].strip() or f"{title} 完赛记录"
                ok, msg, updated_act, _ = self.add_contribution(
                    aid,
                    uid=creator_uid,
                    name=creator_name,
                    content=fallback_content,
                    kind="game_round",
                    _connection=db,
                )
                if not ok or updated_act is None:
                    raise ValueError(msg)
                act = updated_act

            participant_rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM participants WHERE activity_id=? ORDER BY rowid",
                    (aid,),
                )
            ]
            contribution_rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM contributions WHERE activity_id=? ORDER BY id",
                    (aid,),
                )
            ]
            joined = [
                p for p in participant_rows if p.get("status", "joined") == "joined"
            ]
            valuable, worthiness_reason, importance = evaluate_outcome_worthiness(
                {**act, "status": "completed"},
                participant_rows,
                contribution_rows,
                min_participants=min_chronicle_participants,
                min_contributions=min_chronicle_contributions,
            )
            closing_note = clean_summary[:400]
            artifact_md = build_artifact_markdown(
                act, participant_rows, contribution_rows, closing_note=closing_note
            )
            cat = CATEGORIES.get(act["category"], CATEGORIES["creation"])
            member_names = "、".join(p["name"] for p in joined[:12])
            excerpts = "；".join(
                f"{c['name']}：{str(c['content']).strip()[:80]}"
                for c in contribution_rows[:6]
            )
            summary_parts = [
                f"群友共同完成{cat['label']}《{act['title']}》（编号 {act['id']}），共 {len(joined)} 人参与（{member_names}），历经 {len(act['stages'])} 个阶段、累计 {len(contribution_rows)} 条推进记录。"
            ]
            if closing_note.strip():
                summary_parts.append(f"结项总结：{closing_note.strip()[:300]}")
            elif excerpts:
                summary_parts.append(f"内容节选：{excerpts[:450]}")
            outcome_summary = " ".join(summary_parts)
            stats = {
                "participant_count": len(joined),
                "contribution_count": len(contribution_rows),
                "stages_total": len(act["stages"]),
            }
            if not self.save_completion(
                aid,
                expected_revision=act["revision"],
                closing_note=closing_note,
                summary=outcome_summary,
                artifact_markdown=artifact_md,
                valuable=valuable,
                worthiness_reason=worthiness_reason,
                importance=importance,
                participants_snapshot=joined,
                stats=stats,
                completed_at=now,
                _connection=db,
            ):
                raise RuntimeError("小游戏成果原子归档失败")

            if clean_dedup:
                db.execute(
                    "INSERT INTO minigame_dedup(umo, dedup_key, activity_id, created_at) VALUES (?, ?, ?, ?)",
                    (umo, clean_dedup, aid, now),
                )

            outcome_row = db.execute(
                "SELECT * FROM outcomes WHERE activity_id=?", (aid,)
            ).fetchone()
            hydrated = self._hydrate_outcome_row(outcome_row)
            if hydrated is None:
                raise RuntimeError("小游戏成果归档后回读失败")
            return hydrated, True

    def pending_outcome_syncs(
        self,
        *,
        memory: bool,
        chronicle: bool,
        now: float | None = None,
        max_attempts: int = 8,
    ) -> list[str]:
        """List all completed outcomes (activities + minigames) eligible for sync retry."""
        ts = time.time() if now is None else float(now)
        with self.connection() as db:
            return [
                str(row["activity_id"])
                for row in db.execute(
                    """
                    SELECT o.activity_id
                    FROM outcomes o
                    WHERE o.sync_attempts < ?
                      AND o.next_sync_at <= ?
                      AND (
                        (? AND o.memory_status IN (
                            'pending', 'unavailable', 'error', 'timeout', 'not_initialized',
                            'engine_unavailable', 'source_store_unavailable', 'memory_index_unavailable',
                            'memory_write_failed', 'source_snapshot_changed'
                        )) OR
                        (? AND o.valuable AND o.chronicle_event_id=0
                            AND o.chronicle_status IN ('pending', 'unavailable', 'error', 'timeout'))
                      )
                    ORDER BY o.next_sync_at ASC, o.updated_at ASC
                    LIMIT 50
                    """,
                    (max(1, int(max_attempts)), ts, memory, chronicle),
                )
            ]

    def pending_truth_outcomes(self, *, memory: bool, chronicle: bool) -> list[str]:
        return self.pending_outcome_syncs(memory=memory, chronicle=chronicle)

    def record_sync_attempt(
        self,
        activity_id: str,
        *,
        succeeded: bool,
        error: str = "",
        now: float | None = None,
        backoff_base: float = 30.0,
        backoff_cap: float = 3600.0,
    ) -> None:
        ts = time.time() if now is None else float(now)
        with self.connection(write=True) as db:
            if succeeded:
                db.execute(
                    "UPDATE outcomes SET sync_attempts=0, next_sync_at=0, last_sync_error='', updated_at=? WHERE activity_id=?",
                    (ts, activity_id),
                )
                return
            row = db.execute(
                "SELECT sync_attempts FROM outcomes WHERE activity_id=?",
                (activity_id,),
            ).fetchone()
            attempts = int(row["sync_attempts"] if row else 0)
            delay = (
                0.0
                if attempts == 0
                else min(backoff_cap, backoff_base * (2 ** min(attempts - 1, 10)))
            )
            db.execute(
                "UPDATE outcomes SET sync_attempts=sync_attempts+1, next_sync_at=?, last_sync_error=?, updated_at=? WHERE activity_id=?",
                (ts + delay, str(error or "")[:300], ts, activity_id),
            )

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
            return self._hydrate_outcome_row(row)

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
                d = self._hydrate_outcome_row(r)
                if d is None:
                    continue
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

    def activity_count_since(self, since_ts: float, umo: str = "") -> int:
        """Count activities updated and contributions logged since ``since_ts``."""
        with self.connection(write=False) as db:
            if umo:
                acts = db.execute(
                    "SELECT COUNT(*) FROM activities WHERE umo=? AND updated_at >= ?",
                    (umo, float(since_ts)),
                ).fetchone()[0]
                contribs = db.execute(
                    "SELECT COUNT(*) FROM contributions WHERE umo=? AND created_at >= ?",
                    (umo, float(since_ts)),
                ).fetchone()[0]
            else:
                acts = db.execute(
                    "SELECT COUNT(*) FROM activities WHERE updated_at >= ?",
                    (float(since_ts),),
                ).fetchone()[0]
                contribs = db.execute(
                    "SELECT COUNT(*) FROM contributions WHERE created_at >= ?",
                    (float(since_ts),),
                ).fetchone()[0]
            return int(acts) + int(contribs)

    def search_items(
        self, query: str, umo: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        """Read-only search across activities and completed outcomes."""
        clean = str(query or "").strip()
        cap = max(1, min(50, int(limit or 8)))
        if not clean:
            return []
        pattern = f"%{clean.lower()}%"
        results: list[dict[str, Any]] = []
        with self.connection(write=False) as db:
            act_where = "WHERE (LOWER(a.id) LIKE ? OR LOWER(a.title) LIKE ? OR LOWER(a.description) LIKE ?)"
            act_params: list[Any] = [pattern, pattern, pattern]
            if umo:
                act_where += " AND a.umo = ?"
                act_params.append(umo)
            act_params.append(cap)
            act_rows = db.execute(
                f"""
                SELECT a.*,
                    (SELECT COUNT(*) FROM participants p WHERE p.activity_id=a.id AND p.status='joined') AS participant_count,
                    (SELECT COUNT(*) FROM contributions c WHERE c.activity_id=a.id) AS contribution_count
                FROM activities a
                {act_where}
                ORDER BY
                    CASE WHEN a.status IN ('recruiting', 'active') THEN 0 ELSE 1 END ASC,
                    a.updated_at DESC
                LIMIT ?
                """,
                tuple(act_params),
            ).fetchall()
            seen_ids: set[str] = set()
            for r in act_rows:
                act = self._hydrate_activity(r)
                if act is None:
                    continue
                seen_ids.add(str(act["id"]))
                results.append({"record_type": "activity", "data": act})

            remaining = cap - len(results)
            if remaining > 0:
                out_where = "WHERE (LOWER(activity_id) LIKE ? OR LOWER(title) LIKE ? OR LOWER(summary) LIKE ?)"
                out_params: list[Any] = [pattern, pattern, pattern]
                if umo:
                    out_where += " AND umo = ?"
                    out_params.append(umo)
                out_params.append(remaining + len(seen_ids))
                out_rows = db.execute(
                    f"""
                    SELECT activity_id, umo, category, title, summary, valuable, importance, created_at
                    FROM outcomes
                    {out_where}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    tuple(out_params),
                ).fetchall()
                for r in out_rows:
                    if len(results) >= cap:
                        break
                    aid = str(r["activity_id"])
                    if aid in seen_ids:
                        continue
                    d = dict(r)
                    cat = CATEGORIES.get(d["category"], CATEGORIES["creation"])
                    d["category_label"] = cat["label"]
                    d["category_icon"] = cat["icon"]
                    results.append({"record_type": "outcome", "data": d})
        return results[:cap]

    def recent_journal_rows(
        self,
        *,
        since_ts: float,
        until_ts: float,
        umo: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read-only activities updated or completed within [since_ts, until_ts)."""
        cap = max(1, min(50, int(limit or 20)))
        clauses = [
            "a.status IN ('recruiting', 'active', 'completed')",
            "a.updated_at >= ?",
            "a.updated_at < ?",
        ]
        params: list[Any] = [float(since_ts), float(until_ts)]
        if umo:
            clauses.append("a.umo = ?")
            params.append(umo)
        params.append(cap)
        where = "WHERE " + " AND ".join(clauses)
        with self.connection(write=False) as db:
            rows = db.execute(
                f"""
                SELECT a.*,
                    (SELECT COUNT(*) FROM participants p WHERE p.activity_id=a.id AND p.status='joined') AS participant_count,
                    (SELECT COUNT(*) FROM contributions c WHERE c.activity_id=a.id) AS contribution_count
                FROM activities a
                {where}
                ORDER BY a.updated_at DESC, a.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                act = self._hydrate_activity(r)
                if act is not None:
                    out.append(act)
            return out
