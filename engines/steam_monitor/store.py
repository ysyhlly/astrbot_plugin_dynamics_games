"""Thread-safe SQLite WAL persistence store for the Steam Monitor engine."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .state_machine import ActiveSession

_GROUP_SWITCH_FIELDS = frozenset(
    {
        "monitor_enabled",
        "achievement_enabled",
        "rank_push_enabled",
        "rank_push_global",
    }
)


class SteamMonitorStore:
    """ACID SQLite WAL store for Steam monitoring state, sessions, bindings, and caches."""

    _locks: dict[str, threading.RLock] = {}

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self._locks.setdefault(
            str(self.path.resolve()), threading.RLock()
        )
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(
                    f"Unsupported steam_monitor DB schema version: {version}"
                )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS group_settings (
                    group_id TEXT PRIMARY KEY,
                    notify_session TEXT NOT NULL DEFAULT '',
                    monitor_enabled INTEGER NOT NULL DEFAULT 1,
                    achievement_enabled INTEGER NOT NULL DEFAULT 1,
                    rank_push_enabled INTEGER NOT NULL DEFAULT 0,
                    rank_push_global INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS group_players (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    added_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, steam_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_players_sid
                    ON group_players(steam_id);

                CREATE TABLE IF NOT EXISTS push_routes (
                    steam_id TEXT NOT NULL,
                    target_group_id TEXT NOT NULL,
                    created_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (steam_id, target_group_id)
                );
                CREATE INDEX IF NOT EXISTS idx_push_routes_group
                    ON push_routes(target_group_id);

                CREATE TABLE IF NOT EXISTS player_bindings (
                    steam_id TEXT PRIMARY KEY,
                    qq_id TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    last_persona_name TEXT NOT NULL DEFAULT '',
                    last_avatar_url TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_player_bindings_qq
                    ON player_bindings(qq_id);

                CREATE TABLE IF NOT EXISTS active_sessions (
                    steam_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    game_name TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'playing',
                    exited_at REAL,
                    exit_deadline REAL,
                    last_seen_at REAL NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    player_name TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS play_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    steam_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    game_name TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL,
                    duration_sec REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'live',
                    UNIQUE(steam_id, game_id, started_at, ended_at)
                );
                CREATE INDEX IF NOT EXISTS idx_play_sessions_window
                    ON play_sessions(started_at, ended_at);
                CREATE INDEX IF NOT EXISTS idx_play_sessions_sid_window
                    ON play_sessions(steam_id, started_at, ended_at);

                CREATE TABLE IF NOT EXISTS legacy_daily_playtime (
                    day_key TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    game_name TEXT NOT NULL DEFAULT '',
                    seconds REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (day_key, steam_id, game_id)
                );
                CREATE INDEX IF NOT EXISTS idx_legacy_daily_range
                    ON legacy_daily_playtime(day_key, steam_id);

                CREATE TABLE IF NOT EXISTS game_cache (
                    game_id TEXT PRIMARY KEY,
                    name_zh TEXT NOT NULL DEFAULT '',
                    name_en TEXT NOT NULL DEFAULT '',
                    header_url TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0
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

    # ------------------------------------------------------------------
    # kv_meta
    # ------------------------------------------------------------------

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT value FROM kv_meta WHERE key=?", (str(key),)
            ).fetchone()
            return str(row["value"]) if row is not None else default

    def set_kv(self, key: str, value: str) -> None:
        now = time.time()
        with self.connection(write=True) as db:
            db.execute(
                """
                INSERT INTO kv_meta(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (str(key), str(value), now),
            )

    def delete_kv(self, key: str) -> bool:
        with self.connection(write=True) as db:
            cur = db.execute("DELETE FROM kv_meta WHERE key=?", (str(key),))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # group_settings & switches & notify_session (UMO)
    # ------------------------------------------------------------------

    @staticmethod
    def _hydrate_group_settings(
        row: sqlite3.Row | None, group_id: str
    ) -> dict[str, Any]:
        if row is None:
            return {
                "group_id": str(group_id),
                "notify_session": "",
                "monitor_enabled": True,
                "achievement_enabled": True,
                "rank_push_enabled": False,
                "rank_push_global": False,
                "updated_at": 0.0,
            }
        return {
            "group_id": str(row["group_id"]),
            "notify_session": str(row["notify_session"] or ""),
            "monitor_enabled": bool(row["monitor_enabled"]),
            "achievement_enabled": bool(row["achievement_enabled"]),
            "rank_push_enabled": bool(row["rank_push_enabled"]),
            "rank_push_global": bool(row["rank_push_global"]),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def get_group_settings(self, group_id: str) -> dict[str, Any]:
        gid = str(group_id).strip()
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM group_settings WHERE group_id=?", (gid,)
            ).fetchone()
            return self._hydrate_group_settings(row, gid)

    def list_group_settings(self) -> list[dict[str, Any]]:
        with self.connection(write=False) as db:
            rows = db.execute(
                "SELECT * FROM group_settings ORDER BY group_id ASC"
            ).fetchall()
            return [self._hydrate_group_settings(r, str(r["group_id"])) for r in rows]

    def upsert_group_settings(
        self,
        group_id: str,
        *,
        notify_session: str | None = None,
        monitor_enabled: bool | None = None,
        achievement_enabled: bool | None = None,
        rank_push_enabled: bool | None = None,
        rank_push_global: bool | None = None,
    ) -> dict[str, Any]:
        gid = str(group_id).strip()
        if not gid:
            raise ValueError("group_id cannot be empty")
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM group_settings WHERE group_id=?", (gid,)
            ).fetchone()
            current = self._hydrate_group_settings(row, gid)
            new_notify = (
                str(notify_session).strip()
                if notify_session is not None
                else current["notify_session"]
            )
            new_monitor = (
                bool(monitor_enabled)
                if monitor_enabled is not None
                else current["monitor_enabled"]
            )
            new_ach = (
                bool(achievement_enabled)
                if achievement_enabled is not None
                else current["achievement_enabled"]
            )
            new_rank_push = (
                bool(rank_push_enabled)
                if rank_push_enabled is not None
                else current["rank_push_enabled"]
            )
            new_rank_global = (
                bool(rank_push_global)
                if rank_push_global is not None
                else current["rank_push_global"]
            )
            db.execute(
                """
                INSERT INTO group_settings(
                    group_id, notify_session, monitor_enabled,
                    achievement_enabled, rank_push_enabled, rank_push_global, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    notify_session=excluded.notify_session,
                    monitor_enabled=excluded.monitor_enabled,
                    achievement_enabled=excluded.achievement_enabled,
                    rank_push_enabled=excluded.rank_push_enabled,
                    rank_push_global=excluded.rank_push_global,
                    updated_at=excluded.updated_at
                """,
                (
                    gid,
                    new_notify,
                    1 if new_monitor else 0,
                    1 if new_ach else 0,
                    1 if new_rank_push else 0,
                    1 if new_rank_global else 0,
                    now,
                ),
            )
            updated = db.execute(
                "SELECT * FROM group_settings WHERE group_id=?", (gid,)
            ).fetchone()
            return self._hydrate_group_settings(updated, gid)

    def set_notify_session(self, group_id: str, notify_session: str) -> dict[str, Any]:
        return self.upsert_group_settings(
            group_id, notify_session=str(notify_session or "").strip()
        )

    def get_notify_session(self, group_id: str) -> str:
        return str(self.get_group_settings(group_id)["notify_session"])

    def set_group_switch(
        self, group_id: str, field: str, enabled: bool
    ) -> dict[str, Any]:
        if field not in _GROUP_SWITCH_FIELDS:
            raise ValueError(
                f"Invalid switch field '{field}'; expected one of {sorted(_GROUP_SWITCH_FIELDS)}"
            )
        return self.upsert_group_settings(group_id, **{field: bool(enabled)})

    def list_rank_push_groups(self) -> list[dict[str, Any]]:
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT * FROM group_settings
                WHERE rank_push_enabled = 1
                ORDER BY group_id ASC
                """
            ).fetchall()
            return [self._hydrate_group_settings(r, str(r["group_id"])) for r in rows]

    def list_enabled_monitor_groups(self) -> list[str]:
        """Return groups that have players or push routes and have monitor_enabled=1."""
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT DISTINCT g.group_id
                FROM (
                    SELECT group_id FROM group_players
                    UNION
                    SELECT target_group_id AS group_id FROM push_routes
                ) g
                LEFT JOIN group_settings gs ON gs.group_id = g.group_id
                WHERE COALESCE(gs.monitor_enabled, 1) = 1
                ORDER BY g.group_id ASC
                """
            ).fetchall()
            return [str(r["group_id"]) for r in rows]

    # ------------------------------------------------------------------
    # group_players
    # ------------------------------------------------------------------

    def add_group_player(self, group_id: str, steam_id: str) -> bool:
        gid = str(group_id).strip()
        sid = str(steam_id).strip()
        if not gid or not sid:
            return False
        now = time.time()
        with self.connection(write=True) as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO group_players(group_id, steam_id, added_at)
                VALUES (?, ?, ?)
                """,
                (gid, sid, now),
            )
            # Ensure a group_settings row exists so defaults are explicit
            db.execute(
                """
                INSERT OR IGNORE INTO group_settings(
                    group_id, notify_session, monitor_enabled,
                    achievement_enabled, rank_push_enabled, rank_push_global, updated_at
                ) VALUES (?, '', 1, 1, 0, 0, ?)
                """,
                (gid, now),
            )
            return cur.rowcount > 0

    def remove_group_player(self, group_id: str, steam_id: str) -> bool:
        gid = str(group_id).strip()
        sid = str(steam_id).strip()
        with self.connection(write=True) as db:
            cur = db.execute(
                "DELETE FROM group_players WHERE group_id=? AND steam_id=?",
                (gid, sid),
            )
            return cur.rowcount > 0

    def list_group_players(self, group_id: str) -> list[str]:
        gid = str(group_id).strip()
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT steam_id FROM group_players
                WHERE group_id=?
                ORDER BY added_at ASC, steam_id ASC
                """,
                (gid,),
            ).fetchall()
            return [str(r["steam_id"]) for r in rows]

    def list_all_group_players(self) -> dict[str, list[str]]:
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT group_id, steam_id FROM group_players
                ORDER BY group_id ASC, added_at ASC, steam_id ASC
                """
            ).fetchall()
            result: dict[str, list[str]] = {}
            for r in rows:
                result.setdefault(str(r["group_id"]), []).append(str(r["steam_id"]))
            return result

    def list_all_monitored_steam_ids(
        self, *, only_enabled_groups: bool = False
    ) -> list[str]:
        with self.connection(write=False) as db:
            if only_enabled_groups:
                rows = db.execute(
                    """
                    SELECT DISTINCT gp.steam_id
                    FROM group_players gp
                    LEFT JOIN group_settings gs ON gs.group_id = gp.group_id
                    WHERE COALESCE(gs.monitor_enabled, 1) = 1
                    UNION
                    SELECT DISTINCT pr.steam_id
                    FROM push_routes pr
                    LEFT JOIN group_settings gs ON gs.group_id = pr.target_group_id
                    WHERE COALESCE(gs.monitor_enabled, 1) = 1
                    ORDER BY steam_id ASC
                    """
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT DISTINCT steam_id FROM group_players
                    UNION
                    SELECT DISTINCT steam_id FROM push_routes
                    ORDER BY steam_id ASC
                    """
                ).fetchall()
            return [str(r["steam_id"]) for r in rows]

    def get_groups_for_steam_id(self, steam_id: str) -> list[str]:
        sid = str(steam_id).strip()
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT group_id FROM group_players
                WHERE steam_id=?
                ORDER BY group_id ASC
                """,
                (sid,),
            ).fetchall()
            return [str(r["group_id"]) for r in rows]

    def clear_group_players(self, group_id: str) -> int:
        gid = str(group_id).strip()
        with self.connection(write=True) as db:
            cur1 = db.execute("DELETE FROM group_players WHERE group_id=?", (gid,))
            cur2 = db.execute(
                "DELETE FROM push_routes WHERE target_group_id=?", (gid,)
            )
            return int(cur1.rowcount or 0) + int(cur2.rowcount or 0)

    def clear_all_players(self) -> dict[str, int]:
        with self.connection(write=True) as db:
            c1 = db.execute("DELETE FROM group_players").rowcount or 0
            c2 = db.execute("DELETE FROM push_routes").rowcount or 0
            c3 = db.execute("DELETE FROM active_sessions").rowcount or 0
            return {
                "group_players": int(c1),
                "push_routes": int(c2),
                "active_sessions": int(c3),
            }

    # ------------------------------------------------------------------
    # push_routes
    # ------------------------------------------------------------------

    def add_push_route(self, steam_id: str, target_group_id: str) -> bool:
        sid = str(steam_id).strip()
        gid = str(target_group_id).strip()
        if not sid or not gid:
            return False
        now = time.time()
        with self.connection(write=True) as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO push_routes(steam_id, target_group_id, created_at)
                VALUES (?, ?, ?)
                """,
                (sid, gid, now),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO group_settings(
                    group_id, notify_session, monitor_enabled,
                    achievement_enabled, rank_push_enabled, rank_push_global, updated_at
                ) VALUES (?, '', 1, 1, 0, 0, ?)
                """,
                (gid, now),
            )
            return cur.rowcount > 0

    def remove_push_route(self, steam_id: str, target_group_id: str) -> bool:
        sid = str(steam_id).strip()
        gid = str(target_group_id).strip()
        with self.connection(write=True) as db:
            cur = db.execute(
                "DELETE FROM push_routes WHERE steam_id=? AND target_group_id=?",
                (sid, gid),
            )
            return cur.rowcount > 0

    def list_push_routes(
        self,
        *,
        steam_id: str | None = None,
        target_group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if steam_id is not None:
            clauses.append("steam_id = ?")
            params.append(str(steam_id).strip())
        if target_group_id is not None:
            clauses.append("target_group_id = ?")
            params.append(str(target_group_id).strip())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.connection(write=False) as db:
            rows = db.execute(
                f"""
                SELECT steam_id, target_group_id, created_at
                FROM push_routes
                {where}
                ORDER BY steam_id ASC, target_group_id ASC
                """,
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_notify_target_groups(
        self,
        steam_id: str,
        *,
        only_monitor_enabled: bool = True,
        only_achievement_enabled: bool = False,
    ) -> list[str]:
        """Return deduplicated target group IDs (direct + push routes) for a player."""
        sid = str(steam_id).strip()
        conditions: list[str] = []
        if only_monitor_enabled:
            conditions.append("COALESCE(gs.monitor_enabled, 1) = 1")
        if only_achievement_enabled:
            conditions.append("COALESCE(gs.achievement_enabled, 1) = 1")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self.connection(write=False) as db:
            rows = db.execute(
                f"""
                SELECT DISTINCT g.group_id
                FROM (
                    SELECT group_id FROM group_players WHERE steam_id = ?
                    UNION
                    SELECT target_group_id AS group_id FROM push_routes WHERE steam_id = ?
                ) g
                LEFT JOIN group_settings gs ON gs.group_id = g.group_id
                {where}
                ORDER BY g.group_id ASC
                """,
                (sid, sid),
            ).fetchall()
            return [str(r["group_id"]) for r in rows]

    def get_group_effective_steam_ids(self, group_id: str) -> list[str]:
        """Return direct players plus players routed to this group via push_routes."""
        gid = str(group_id).strip()
        with self.connection(write=False) as db:
            rows = db.execute(
                """
                SELECT DISTINCT steam_id FROM (
                    SELECT steam_id FROM group_players WHERE group_id = ?
                    UNION
                    SELECT steam_id FROM push_routes WHERE target_group_id = ?
                )
                ORDER BY steam_id ASC
                """,
                (gid, gid),
            ).fetchall()
            return [str(r["steam_id"]) for r in rows]

    # ------------------------------------------------------------------
    # player_bindings
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_nickname(nickname: str | None) -> str:
        text = str(nickname or "").strip()
        return "" if text == "*" else text

    def upsert_binding(
        self,
        steam_id: str,
        *,
        qq_id: str | None = None,
        nickname: str | None = None,
        last_persona_name: str | None = None,
        last_avatar_url: str | None = None,
    ) -> dict[str, Any]:
        sid = str(steam_id).strip()
        if not sid:
            raise ValueError("steam_id cannot be empty")
        now = time.time()
        with self.connection(write=True) as db:
            existing = db.execute(
                "SELECT * FROM player_bindings WHERE steam_id=?", (sid,)
            ).fetchone()
            new_qq = (
                str(qq_id).strip()
                if qq_id is not None
                else (str(existing["qq_id"]) if existing else "")
            )
            new_nick = (
                self._clean_nickname(nickname)
                if nickname is not None
                else (str(existing["nickname"]) if existing else "")
            )
            new_persona = (
                str(last_persona_name).strip()
                if last_persona_name is not None
                else (str(existing["last_persona_name"]) if existing else "")
            )
            new_avatar = (
                str(last_avatar_url).strip()
                if last_avatar_url is not None
                else (str(existing["last_avatar_url"]) if existing else "")
            )
            # Keep qq_id uniquely mapped to a single SteamID when rebinding
            if new_qq:
                db.execute(
                    "UPDATE player_bindings SET qq_id='', updated_at=? WHERE qq_id=? AND steam_id!=?",
                    (now, new_qq, sid),
                )
            db.execute(
                """
                INSERT INTO player_bindings(
                    steam_id, qq_id, nickname, last_persona_name, last_avatar_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(steam_id) DO UPDATE SET
                    qq_id=excluded.qq_id,
                    nickname=excluded.nickname,
                    last_persona_name=excluded.last_persona_name,
                    last_avatar_url=excluded.last_avatar_url,
                    updated_at=excluded.updated_at
                """,
                (sid, new_qq, new_nick, new_persona, new_avatar, now),
            )
            row = db.execute(
                "SELECT * FROM player_bindings WHERE steam_id=?", (sid,)
            ).fetchone()
            return dict(row)  # type: ignore[arg-type]

    def update_player_profile(
        self,
        steam_id: str,
        *,
        persona_name: str = "",
        avatar_url: str = "",
    ) -> None:
        sid = str(steam_id).strip()
        if not sid:
            return
        now = time.time()
        clean_persona = str(persona_name or "").strip()
        clean_avatar = str(avatar_url or "").strip()
        with self.connection(write=True) as db:
            db.execute(
                """
                INSERT INTO player_bindings(
                    steam_id, qq_id, nickname, last_persona_name, last_avatar_url, updated_at
                ) VALUES (?, '', '', ?, ?, ?)
                ON CONFLICT(steam_id) DO UPDATE SET
                    last_persona_name=CASE WHEN excluded.last_persona_name != '' THEN excluded.last_persona_name ELSE player_bindings.last_persona_name END,
                    last_avatar_url=CASE WHEN excluded.last_avatar_url != '' THEN excluded.last_avatar_url ELSE player_bindings.last_avatar_url END,
                    updated_at=excluded.updated_at
                """,
                (sid, clean_persona, clean_avatar, now),
            )

    def get_binding_by_steam_id(self, steam_id: str) -> dict[str, Any] | None:
        sid = str(steam_id).strip()
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM player_bindings WHERE steam_id=?", (sid,)
            ).fetchone()
            return dict(row) if row is not None else None

    def get_binding_by_qq(self, qq_id: str) -> dict[str, Any] | None:
        qq = str(qq_id).strip()
        if not qq:
            return None
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM player_bindings WHERE qq_id=? ORDER BY updated_at DESC LIMIT 1",
                (qq,),
            ).fetchone()
            return dict(row) if row is not None else None

    def delete_binding_by_qq(self, qq_id: str) -> bool:
        qq = str(qq_id).strip()
        if not qq:
            return False
        now = time.time()
        with self.connection(write=True) as db:
            rows = db.execute(
                "SELECT steam_id, nickname, last_persona_name, last_avatar_url FROM player_bindings WHERE qq_id=?",
                (qq,),
            ).fetchall()
            if not rows:
                return False
            for r in rows:
                if r["last_persona_name"] or r["last_avatar_url"]:
                    db.execute(
                        "UPDATE player_bindings SET qq_id='', nickname='', updated_at=? WHERE steam_id=?",
                        (now, r["steam_id"]),
                    )
                else:
                    db.execute(
                        "DELETE FROM player_bindings WHERE steam_id=?",
                        (r["steam_id"],),
                    )
            return True

    def delete_binding_by_steam_id(self, steam_id: str) -> bool:
        sid = str(steam_id).strip()
        with self.connection(write=True) as db:
            cur = db.execute(
                "DELETE FROM player_bindings WHERE steam_id=?", (sid,)
            )
            return cur.rowcount > 0

    def list_bindings(self) -> list[dict[str, Any]]:
        with self.connection(write=False) as db:
            rows = db.execute(
                "SELECT * FROM player_bindings ORDER BY steam_id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_bindings_map(
        self, steam_ids: Iterable[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Batch-fetch player_bindings keyed by steam_id in one DB read."""
        with self.connection(write=False) as db:
            if steam_ids is None:
                rows = db.execute("SELECT * FROM player_bindings").fetchall()
            else:
                sid_list = sorted({str(x).strip() for x in steam_ids if str(x).strip()})
                if not sid_list:
                    return {}
                placeholders = ",".join("?" for _ in sid_list)
                rows = db.execute(
                    f"SELECT * FROM player_bindings WHERE steam_id IN ({placeholders})",
                    tuple(sid_list),
                ).fetchall()
            return {str(r["steam_id"]): dict(r) for r in rows}

    @classmethod
    def resolve_display_name_from_binding(
        cls,
        binding: dict[str, Any] | None,
        steam_id: str,
        fallback_persona: str = "",
    ) -> str:
        """Resolve display name from a pre-fetched binding row without hitting SQLite."""
        sid = str(steam_id).strip()
        if binding:
            nick = cls._clean_nickname(binding.get("nickname"))
            if nick:
                return nick
        clean_fallback = str(fallback_persona or "").strip()
        if clean_fallback:
            return clean_fallback
        if binding:
            last_persona = str(binding.get("last_persona_name") or "").strip()
            if last_persona:
                return last_persona
        return sid

    def resolve_display_name(
        self, steam_id: str, fallback_persona: str = ""
    ) -> str:
        """Resolve display name priority: nickname > fallback_persona > last_persona_name > steam_id."""
        sid = str(steam_id).strip()
        binding = self.get_binding_by_steam_id(sid)
        return self.resolve_display_name_from_binding(binding, sid, fallback_persona)

    def update_player_profiles_batch(
        self, updates: Iterable[tuple[str, str, str]]
    ) -> int:
        """Batch-update `(steam_id, persona_name, avatar_url)` profiles in a single transaction."""
        rows: list[tuple[str, str, str, float]] = []
        now = time.time()
        for raw_sid, raw_persona, raw_avatar in updates:
            sid = str(raw_sid or "").strip()
            persona = str(raw_persona or "").strip()
            avatar = str(raw_avatar or "").strip()
            if sid and (persona or avatar):
                rows.append((sid, persona, avatar, now))
        if not rows:
            return 0
        with self.connection(write=True) as db:
            db.executemany(
                """
                INSERT INTO player_bindings(
                    steam_id, qq_id, nickname, last_persona_name, last_avatar_url, updated_at
                ) VALUES (?, '', '', ?, ?, ?)
                ON CONFLICT(steam_id) DO UPDATE SET
                    last_persona_name=CASE WHEN excluded.last_persona_name != '' THEN excluded.last_persona_name ELSE player_bindings.last_persona_name END,
                    last_avatar_url=CASE WHEN excluded.last_avatar_url != '' THEN excluded.last_avatar_url ELSE player_bindings.last_avatar_url END,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def commit_poll_batch(
        self,
        *,
        profile_updates: Iterable[tuple[str, str, str]] = (),
        upsert_sessions: Iterable[ActiveSession] = (),
        delete_session_sids: Iterable[str] = (),
        play_sessions: Iterable[tuple[str, str, str, float, float, float, str]] = (),
        last_poll_at: float | None = None,
    ) -> None:
        """Commit an entire poll cycle's DB mutations inside a single atomic SQLite transaction."""
        now = time.time()
        prof_rows: list[tuple[str, str, str, float]] = []
        for raw_sid, raw_persona, raw_avatar in profile_updates:
            sid = str(raw_sid or "").strip()
            persona = str(raw_persona or "").strip()
            avatar = str(raw_avatar or "").strip()
            if sid and (persona or avatar):
                prof_rows.append((sid, persona, avatar, now))

        session_rows: list[tuple[Any, ...]] = []
        for session in upsert_sessions:
            session_rows.append(
                (
                    session.steam_id,
                    session.game_id,
                    session.game_name,
                    float(session.started_at),
                    session.state,
                    float(session.exited_at) if session.exited_at is not None else None,
                    (
                        float(session.exit_deadline)
                        if session.exit_deadline is not None
                        else None
                    ),
                    float(session.last_seen_at),
                    int(session.consecutive_failures),
                    session.player_name,
                    session.avatar_url,
                )
            )

        del_rows = [
            (str(sid).strip(),)
            for sid in delete_session_sids
            if str(sid or "").strip()
        ]

        play_rows: list[tuple[Any, ...]] = []
        for raw_sid, raw_gid, raw_name, started_at, ended_at, dur_sec, source in play_sessions:
            sid = str(raw_sid or "").strip()
            gid = str(raw_gid or "").strip()
            s_ts = float(started_at)
            e_ts = float(ended_at)
            dur = float(dur_sec)
            if sid and gid and e_ts > s_ts and dur > 0.0:
                clean_name = str(raw_name or "").strip() or gid
                play_rows.append(
                    (sid, gid, clean_name, s_ts, e_ts, dur, str(source or "live"))
                )

        if (
            not prof_rows
            and not session_rows
            and not del_rows
            and not play_rows
            and last_poll_at is None
        ):
            return

        with self.connection(write=True) as db:
            if prof_rows:
                db.executemany(
                    """
                    INSERT INTO player_bindings(
                        steam_id, qq_id, nickname, last_persona_name, last_avatar_url, updated_at
                    ) VALUES (?, '', '', ?, ?, ?)
                    ON CONFLICT(steam_id) DO UPDATE SET
                        last_persona_name=CASE WHEN excluded.last_persona_name != '' THEN excluded.last_persona_name ELSE player_bindings.last_persona_name END,
                        last_avatar_url=CASE WHEN excluded.last_avatar_url != '' THEN excluded.last_avatar_url ELSE player_bindings.last_avatar_url END,
                        updated_at=excluded.updated_at
                    """,
                    prof_rows,
                )
            if session_rows:
                db.executemany(
                    """
                    INSERT INTO active_sessions(
                        steam_id, game_id, game_name, started_at, state,
                        exited_at, exit_deadline, last_seen_at,
                        consecutive_failures, player_name, avatar_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(steam_id) DO UPDATE SET
                        game_id=excluded.game_id,
                        game_name=excluded.game_name,
                        started_at=excluded.started_at,
                        state=excluded.state,
                        exited_at=excluded.exited_at,
                        exit_deadline=excluded.exit_deadline,
                        last_seen_at=excluded.last_seen_at,
                        consecutive_failures=excluded.consecutive_failures,
                        player_name=excluded.player_name,
                        avatar_url=excluded.avatar_url
                    """,
                    session_rows,
                )
            if del_rows:
                db.executemany(
                    "DELETE FROM active_sessions WHERE steam_id=?",
                    del_rows,
                )
            if play_rows:
                db.executemany(
                    """
                    INSERT OR IGNORE INTO play_sessions(
                        steam_id, game_id, game_name, started_at, ended_at, duration_sec, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    play_rows,
                )
            if last_poll_at is not None:
                db.execute(
                    """
                    INSERT INTO kv_meta(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    ("last_poll_at", f"{float(last_poll_at):.3f}", now),
                )

    # ------------------------------------------------------------------
    # active_sessions
    # ------------------------------------------------------------------

    def save_active_session(self, session: ActiveSession) -> None:
        with self.connection(write=True) as db:
            db.execute(
                """
                INSERT INTO active_sessions(
                    steam_id, game_id, game_name, started_at, state,
                    exited_at, exit_deadline, last_seen_at,
                    consecutive_failures, player_name, avatar_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(steam_id) DO UPDATE SET
                    game_id=excluded.game_id,
                    game_name=excluded.game_name,
                    started_at=excluded.started_at,
                    state=excluded.state,
                    exited_at=excluded.exited_at,
                    exit_deadline=excluded.exit_deadline,
                    last_seen_at=excluded.last_seen_at,
                    consecutive_failures=excluded.consecutive_failures,
                    player_name=excluded.player_name,
                    avatar_url=excluded.avatar_url
                """,
                (
                    session.steam_id,
                    session.game_id,
                    session.game_name,
                    float(session.started_at),
                    session.state,
                    float(session.exited_at) if session.exited_at is not None else None,
                    (
                        float(session.exit_deadline)
                        if session.exit_deadline is not None
                        else None
                    ),
                    float(session.last_seen_at),
                    int(session.consecutive_failures),
                    session.player_name,
                    session.avatar_url,
                ),
            )

    def get_active_session(self, steam_id: str) -> ActiveSession | None:
        sid = str(steam_id).strip()
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM active_sessions WHERE steam_id=?", (sid,)
            ).fetchone()
            return ActiveSession.from_dict(dict(row)) if row is not None else None

    def load_active_sessions(self) -> dict[str, ActiveSession]:
        with self.connection(write=False) as db:
            rows = db.execute(
                "SELECT * FROM active_sessions ORDER BY steam_id ASC"
            ).fetchall()
            return {
                str(r["steam_id"]): ActiveSession.from_dict(dict(r)) for r in rows
            }

    def delete_active_session(self, steam_id: str) -> bool:
        sid = str(steam_id).strip()
        with self.connection(write=True) as db:
            cur = db.execute(
                "DELETE FROM active_sessions WHERE steam_id=?", (sid,)
            )
            return cur.rowcount > 0

    def clear_active_sessions(self) -> int:
        with self.connection(write=True) as db:
            cur = db.execute("DELETE FROM active_sessions")
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # play_sessions
    # ------------------------------------------------------------------

    def insert_play_session(
        self,
        steam_id: str,
        game_id: str,
        game_name: str,
        started_at: float,
        ended_at: float,
        duration_sec: float | None = None,
        source: str = "live",
    ) -> int:
        sid = str(steam_id).strip()
        gid = str(game_id).strip()
        s_ts = float(started_at)
        e_ts = float(ended_at)
        if not sid or not gid or e_ts <= s_ts:
            return 0
        dur = (
            float(duration_sec)
            if duration_sec is not None
            else max(0.0, e_ts - s_ts)
        )
        if dur <= 0.0:
            return 0
        clean_name = str(game_name or "").strip() or gid
        with self.connection(write=True) as db:
            cur = db.execute(
                """
                INSERT OR IGNORE INTO play_sessions(
                    steam_id, game_id, game_name, started_at, ended_at, duration_sec, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, gid, clean_name, s_ts, e_ts, dur, str(source or "live")),
            )
            return int(cur.lastrowid or 0) if cur.rowcount > 0 else 0

    def query_play_sessions_overlapping(
        self,
        start_ts: float,
        end_ts: float,
        steam_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Query all closed sessions overlapping `[start_ts, end_ts)`."""
        s_ts = float(start_ts)
        e_ts = float(end_ts)
        if e_ts <= s_ts:
            return []
        params: list[Any] = [s_ts, e_ts]
        sid_clause = ""
        if steam_ids is not None:
            sid_list = sorted({str(x).strip() for x in steam_ids if str(x).strip()})
            if not sid_list:
                return []
            placeholders = ",".join("?" for _ in sid_list)
            sid_clause = f" AND steam_id IN ({placeholders})"
            params.extend(sid_list)
        with self.connection(write=False) as db:
            rows = db.execute(
                f"""
                SELECT id, steam_id, game_id, game_name, started_at, ended_at, duration_sec, source
                FROM play_sessions
                WHERE ended_at > ? AND started_at < ?{sid_clause}
                ORDER BY started_at ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # legacy_daily_playtime
    # ------------------------------------------------------------------

    def upsert_legacy_daily_playtime(
        self,
        day_key: str,
        steam_id: str,
        game_id: str,
        game_name: str,
        seconds: float,
        *,
        mode: str = "replace",
    ) -> None:
        d_key = str(day_key).strip().replace("/", "-")
        sid = str(steam_id).strip()
        gid = str(game_id).strip()
        sec_f = float(seconds)
        if not d_key or not sid or not gid or sec_f <= 0.0:
            return
        clean_name = str(game_name or "").strip() or gid
        with self.connection(write=True) as db:
            if mode == "add":
                db.execute(
                    """
                    INSERT INTO legacy_daily_playtime(
                        day_key, steam_id, game_id, game_name, seconds
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day_key, steam_id, game_id) DO UPDATE SET
                        game_name=CASE WHEN excluded.game_name != excluded.game_id THEN excluded.game_name ELSE legacy_daily_playtime.game_name END,
                        seconds=legacy_daily_playtime.seconds + excluded.seconds
                    """,
                    (d_key, sid, gid, clean_name, sec_f),
                )
            else:
                db.execute(
                    """
                    INSERT INTO legacy_daily_playtime(
                        day_key, steam_id, game_id, game_name, seconds
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day_key, steam_id, game_id) DO UPDATE SET
                        game_name=CASE WHEN excluded.game_name != excluded.game_id THEN excluded.game_name ELSE legacy_daily_playtime.game_name END,
                        seconds=excluded.seconds
                    """,
                    (d_key, sid, gid, clean_name, sec_f),
                )

    def query_legacy_daily_playtime(
        self,
        start_day: str,
        end_day: str,
        steam_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Query legacy daily playtime rows with `start_day <= day_key <= end_day`."""
        s_day = str(start_day).strip().replace("/", "-")
        e_day = str(end_day).strip().replace("/", "-")
        if not s_day or not e_day or e_day < s_day:
            return []
        params: list[Any] = [s_day, e_day]
        sid_clause = ""
        if steam_ids is not None:
            sid_list = sorted({str(x).strip() for x in steam_ids if str(x).strip()})
            if not sid_list:
                return []
            placeholders = ",".join("?" for _ in sid_list)
            sid_clause = f" AND steam_id IN ({placeholders})"
            params.extend(sid_list)
        with self.connection(write=False) as db:
            rows = db.execute(
                f"""
                SELECT day_key, steam_id, game_id, game_name, seconds
                FROM legacy_daily_playtime
                WHERE day_key >= ? AND day_key <= ?{sid_clause}
                ORDER BY day_key ASC, steam_id ASC, game_id ASC
                """,
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # game_cache
    # ------------------------------------------------------------------

    def get_game_cache(self, game_id: str) -> dict[str, Any] | None:
        gid = str(game_id).strip()
        if not gid:
            return None
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM game_cache WHERE game_id=?", (gid,)
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_game_cache(
        self,
        game_id: str,
        *,
        name_zh: str = "",
        name_en: str = "",
        header_url: str = "",
    ) -> dict[str, Any]:
        gid = str(game_id).strip()
        if not gid:
            raise ValueError("game_id cannot be empty")
        now = time.time()
        zh = str(name_zh or "").strip()
        en = str(name_en or "").strip()
        hdr = str(header_url or "").strip()
        with self.connection(write=True) as db:
            db.execute(
                """
                INSERT INTO game_cache(game_id, name_zh, name_en, header_url, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    name_zh=CASE WHEN excluded.name_zh != '' THEN excluded.name_zh ELSE game_cache.name_zh END,
                    name_en=CASE WHEN excluded.name_en != '' THEN excluded.name_en ELSE game_cache.name_en END,
                    header_url=CASE WHEN excluded.header_url != '' THEN excluded.header_url ELSE game_cache.header_url END,
                    updated_at=excluded.updated_at
                """,
                (gid, zh, en, hdr, now),
            )
            row = db.execute(
                "SELECT * FROM game_cache WHERE game_id=?", (gid,)
            ).fetchone()
            return dict(row)  # type: ignore[arg-type]

    def clear_game_cache(self) -> int:
        with self.connection(write=True) as db:
            cur = db.execute("DELETE FROM game_cache")
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # runtime reset & summary
    # ------------------------------------------------------------------

    def reset_runtime_state(self) -> dict[str, int]:
        """Clear transient runtime monitoring state (active sessions) without losing bindings/history."""
        cleared_sessions = self.clear_active_sessions()
        return {"active_sessions_cleared": cleared_sessions}

    def stats_summary(self) -> dict[str, Any]:
        with self.connection(write=False) as db:
            monitored_groups = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT group_id FROM group_players
                        UNION
                        SELECT target_group_id AS group_id FROM push_routes
                    )
                    """
                ).fetchone()[0]
            )
            enabled_groups = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT group_id FROM group_players
                        UNION
                        SELECT target_group_id AS group_id FROM push_routes
                    ) g
                    LEFT JOIN group_settings gs ON gs.group_id = g.group_id
                    WHERE COALESCE(gs.monitor_enabled, 1) = 1
                    """
                ).fetchone()[0]
            )
            monitored_players = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT steam_id FROM group_players
                        UNION
                        SELECT steam_id FROM push_routes
                    )
                    """
                ).fetchone()[0]
            )
            active_playing = int(
                db.execute(
                    "SELECT COUNT(*) FROM active_sessions WHERE state='playing'"
                ).fetchone()[0]
            )
            active_confirming = int(
                db.execute(
                    "SELECT COUNT(*) FROM active_sessions WHERE state='confirming_exit'"
                ).fetchone()[0]
            )
            total_bindings = int(
                db.execute(
                    "SELECT COUNT(*) FROM player_bindings WHERE qq_id != '' OR nickname != ''"
                ).fetchone()[0]
            )
            total_play_sessions = int(
                db.execute("SELECT COUNT(*) FROM play_sessions").fetchone()[0]
            )
            total_legacy_daily = int(
                db.execute("SELECT COUNT(*) FROM legacy_daily_playtime").fetchone()[0]
            )
            cached_games = int(
                db.execute("SELECT COUNT(*) FROM game_cache").fetchone()[0]
            )
            kv_rows = db.execute(
                "SELECT key, value FROM kv_meta WHERE key IN ('last_poll_at', 'last_rank_push_date', 'legacy_migrated_v1')"
            ).fetchall()
            kv = {str(r["key"]): str(r["value"]) for r in kv_rows}
            last_poll_raw = kv.get("last_poll_at", "")
            try:
                last_poll_at = float(last_poll_raw) if last_poll_raw else 0.0
            except ValueError:
                last_poll_at = 0.0
            return {
                "monitored_groups": monitored_groups,
                "enabled_groups": enabled_groups,
                "monitored_players": monitored_players,
                "active_playing": active_playing,
                "active_confirming_exit": active_confirming,
                "total_bindings": total_bindings,
                "total_play_sessions": total_play_sessions,
                "total_legacy_daily_rows": total_legacy_daily,
                "cached_games": cached_games,
                "last_poll_at": last_poll_at,
                "last_rank_push_date": kv.get("last_rank_push_date", ""),
                "legacy_migrated_v1": kv.get("legacy_migrated_v1", "") == "1",
            }
