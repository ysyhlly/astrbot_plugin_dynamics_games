"""Pure functional state machine and natural-day playtime aggregation for Steam Monitor.

Zero I/O: all functions are deterministic given explicit timestamps and timezones.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal

SessionState = Literal["playing", "confirming_exit"]
EventKind = Literal["started", "closed", "fluctuation"]

_EMPTY_GAME_IDS = frozenset({"", "0"})


@dataclass(frozen=True)
class ActiveSession:
    """Global single-source-of-truth active session for a Steam player."""

    steam_id: str
    game_id: str
    game_name: str
    started_at: float
    state: SessionState = "playing"
    exited_at: float | None = None
    exit_deadline: float | None = None
    last_seen_at: float = 0.0
    consecutive_failures: int = 0
    player_name: str = ""
    avatar_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "steam_id": self.steam_id,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "started_at": float(self.started_at),
            "state": self.state,
            "exited_at": float(self.exited_at) if self.exited_at is not None else None,
            "exit_deadline": (
                float(self.exit_deadline) if self.exit_deadline is not None else None
            ),
            "last_seen_at": float(self.last_seen_at),
            "consecutive_failures": int(self.consecutive_failures),
            "player_name": self.player_name,
            "avatar_url": self.avatar_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActiveSession:
        state_raw = str(data.get("state") or "playing")
        state: SessionState = (
            "confirming_exit" if state_raw == "confirming_exit" else "playing"
        )
        exited_raw = data.get("exited_at")
        deadline_raw = data.get("exit_deadline")
        return cls(
            steam_id=str(data.get("steam_id") or data.get("sid") or ""),
            game_id=str(data.get("game_id") or data.get("gameid") or ""),
            game_name=str(data.get("game_name") or ""),
            started_at=float(data.get("started_at") or 0.0),
            state=state,
            exited_at=float(exited_raw) if exited_raw is not None else None,
            exit_deadline=float(deadline_raw) if deadline_raw is not None else None,
            last_seen_at=float(data.get("last_seen_at") or 0.0),
            consecutive_failures=int(data.get("consecutive_failures") or 0),
            player_name=str(data.get("player_name") or ""),
            avatar_url=str(data.get("avatar_url") or ""),
        )


@dataclass(frozen=True)
class SessionEvent:
    """State transition event emitted by the pure session state machine."""

    kind: EventKind
    steam_id: str
    game_id: str
    game_name: str
    started_at: float
    ended_at: float | None = None
    duration_sec: float = 0.0
    player_name: str = ""
    avatar_url: str = ""


def _normalize_game_id(game_id: Any) -> str | None:
    if game_id is None:
        return None
    text = str(game_id).strip()
    if text in _EMPTY_GAME_IDS:
        return None
    return text


def _build_closed_event(
    session: ActiveSession,
    *,
    ended_at: float,
    player_name: str = "",
    avatar_url: str = "",
) -> SessionEvent:
    eff_end = float(ended_at)
    duration_sec = max(0.0, eff_end - float(session.started_at))
    return SessionEvent(
        kind="closed",
        steam_id=session.steam_id,
        game_id=session.game_id,
        game_name=session.game_name,
        started_at=float(session.started_at),
        ended_at=eff_end,
        duration_sec=duration_sec,
        player_name=player_name or session.player_name,
        avatar_url=avatar_url or session.avatar_url,
    )


def _open_session(
    *,
    steam_id: str,
    game_id: str,
    game_name: str,
    now: float,
    player_name: str,
    avatar_url: str,
) -> tuple[ActiveSession, SessionEvent]:
    now_f = float(now)
    resolved_game_name = str(game_name or "").strip() or game_id
    opened = ActiveSession(
        steam_id=steam_id,
        game_id=game_id,
        game_name=resolved_game_name,
        started_at=now_f,
        state="playing",
        exited_at=None,
        exit_deadline=None,
        last_seen_at=now_f,
        consecutive_failures=0,
        player_name=player_name,
        avatar_url=avatar_url,
    )
    event = SessionEvent(
        kind="started",
        steam_id=steam_id,
        game_id=game_id,
        game_name=resolved_game_name,
        started_at=now_f,
        ended_at=None,
        duration_sec=0.0,
        player_name=player_name,
        avatar_url=avatar_url,
    )
    return opened, event


def apply_poll_observation(
    session: ActiveSession | None,
    observed_game_id: str | int | None,
    *,
    now: float,
    steam_id: str,
    game_name: str = "",
    player_name: str = "",
    avatar_url: str = "",
    api_ok: bool = True,
    debounce_seconds: int = 180,
) -> tuple[ActiveSession | None, list[SessionEvent]]:
    """Advance a player's session state machine from a single poll observation.

    CRITICAL:
    - When `api_ok=False`, freeze state completely: increment `consecutive_failures`,
      leave `state`, `exited_at`, and `exit_deadline` untouched, and emit NO events.
    - When `api_ok=True`, reset `consecutive_failures=0` and process start, exit
      confirmation, immediate game switch, fluctuation resume, or true exit.
    """
    if not api_ok:
        if session is None:
            return None, []
        frozen = replace(
            session,
            consecutive_failures=int(session.consecutive_failures) + 1,
        )
        return frozen, []

    now_f = float(now)
    debounce = max(0, int(debounce_seconds))
    sid = str(steam_id or (session.steam_id if session is not None else "")).strip()
    current_gid = _normalize_game_id(observed_game_id)
    p_name = str(player_name or "") or (session.player_name if session is not None else "")
    a_url = str(avatar_url or "") or (session.avatar_url if session is not None else "")
    g_name = str(game_name or "").strip()

    # Idle -> no active session
    if session is None:
        if current_gid is None:
            return None, []
        opened, started_event = _open_session(
            steam_id=sid,
            game_id=current_gid,
            game_name=g_name,
            now=now_f,
            player_name=p_name,
            avatar_url=a_url,
        )
        return opened, [started_event]

    # Playing state
    if session.state == "playing":
        if current_gid == session.game_id:
            updated = replace(
                session,
                game_name=g_name or session.game_name,
                last_seen_at=now_f,
                consecutive_failures=0,
                player_name=p_name,
                avatar_url=a_url,
            )
            return updated, []

        if current_gid is None:
            if debounce <= 0:
                closed_event = _build_closed_event(
                    session,
                    ended_at=now_f,
                    player_name=p_name,
                    avatar_url=a_url,
                )
                return None, [closed_event]
            confirming = replace(
                session,
                state="confirming_exit",
                exited_at=now_f,
                exit_deadline=now_f + float(debounce),
                last_seen_at=now_f,
                consecutive_failures=0,
                player_name=p_name,
                avatar_url=a_url,
            )
            return confirming, []

        # Direct game switch: immediately close old game and start new game
        closed_event = _build_closed_event(
            session,
            ended_at=now_f,
            player_name=p_name,
            avatar_url=a_url,
        )
        opened, started_event = _open_session(
            steam_id=sid,
            game_id=current_gid,
            game_name=g_name,
            now=now_f,
            player_name=p_name,
            avatar_url=a_url,
        )
        return opened, [closed_event, started_event]

    # Confirming exit state
    if session.state == "confirming_exit":
        deadline = (
            float(session.exit_deadline)
            if session.exit_deadline is not None
            else now_f
        )
        effective_ended = (
            float(session.exited_at) if session.exited_at is not None else now_f
        )

        if current_gid == session.game_id:
            if now_f <= deadline:
                resumed = replace(
                    session,
                    state="playing",
                    game_name=g_name or session.game_name,
                    exited_at=None,
                    exit_deadline=None,
                    last_seen_at=now_f,
                    consecutive_failures=0,
                    player_name=p_name,
                    avatar_url=a_url,
                )
                fluct_event = SessionEvent(
                    kind="fluctuation",
                    steam_id=resumed.steam_id,
                    game_id=resumed.game_id,
                    game_name=resumed.game_name,
                    started_at=float(resumed.started_at),
                    ended_at=None,
                    duration_sec=max(0.0, now_f - float(resumed.started_at)),
                    player_name=p_name,
                    avatar_url=a_url,
                )
                return resumed, [fluct_event]

            # Returned after exit deadline already elapsed: close old, start new
            closed_event = _build_closed_event(
                session,
                ended_at=effective_ended,
                player_name=p_name,
                avatar_url=a_url,
            )
            opened, started_event = _open_session(
                steam_id=sid,
                game_id=current_gid,
                game_name=g_name,
                now=now_f,
                player_name=p_name,
                avatar_url=a_url,
            )
            return opened, [closed_event, started_event]

        if current_gid is None:
            if now_f >= deadline:
                closed_event = _build_closed_event(
                    session,
                    ended_at=effective_ended,
                    player_name=p_name,
                    avatar_url=a_url,
                )
                return None, [closed_event]
            still_confirming = replace(
                session,
                last_seen_at=now_f,
                consecutive_failures=0,
                player_name=p_name,
                avatar_url=a_url,
            )
            return still_confirming, []

        # Switched to a different game while confirming exit
        closed_event = _build_closed_event(
            session,
            ended_at=effective_ended,
            player_name=p_name,
            avatar_url=a_url,
        )
        opened, started_event = _open_session(
            steam_id=sid,
            game_id=current_gid,
            game_name=g_name,
            now=now_f,
            player_name=p_name,
            avatar_url=a_url,
        )
        return opened, [closed_event, started_event]

    return session, []


def tick_session_deadline(
    session: ActiveSession,
    now: float,
) -> tuple[ActiveSession | None, list[SessionEvent]]:
    """Close a `confirming_exit` session if its deadline has passed and API is healthy.

    CRITICAL: If `consecutive_failures > 0`, freeze the session and do NOT close it.
    """
    if session.state != "confirming_exit":
        return session, []
    if int(session.consecutive_failures) > 0:
        return session, []
    if session.exit_deadline is None:
        return session, []

    now_f = float(now)
    if now_f >= float(session.exit_deadline):
        ended_at = (
            float(session.exited_at) if session.exited_at is not None else now_f
        )
        closed_event = _build_closed_event(session, ended_at=ended_at)
        return None, [closed_event]
    return session, []


def _resolve_tz(tz: tzinfo | None) -> tzinfo:
    if tz is not None:
        return tz
    local_tz = datetime.now().astimezone().tzinfo
    assert local_tz is not None
    return local_tz


def _normalize_day_str(day_str: str) -> str:
    return str(day_str).strip().replace("/", "-")


def natural_day_bounds(
    day_str: str,
    tz: tzinfo | None = None,
) -> tuple[float, float]:
    """Return `(start_ts, end_ts)` for natural local day `[00:00:00, next 00:00:00)`."""
    resolved_tz = _resolve_tz(tz)
    clean = _normalize_day_str(day_str)
    parsed = datetime.strptime(clean, "%Y-%m-%d").date()
    next_day = parsed + timedelta(days=1)
    start_dt = datetime(parsed.year, parsed.month, parsed.day, 0, 0, 0, tzinfo=resolved_tz)
    end_dt = datetime(
        next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=resolved_tz
    )
    return start_dt.timestamp(), end_dt.timestamp()


def day_key_for_ts(ts: float, tz: tzinfo | None = None) -> str:
    """Return `YYYY-MM-DD` for `ts` using natural 00:00:00 boundary in `tz`."""
    resolved_tz = _resolve_tz(tz)
    return datetime.fromtimestamp(float(ts), tz=resolved_tz).strftime("%Y-%m-%d")


def split_interval_by_natural_day(
    start_ts: float,
    end_ts: float,
    tz: tzinfo | None = None,
) -> list[tuple[str, float]]:
    """Split `[start_ts, end_ts)` at natural 00:00 boundaries with exact float seconds."""
    cur = float(start_ts)
    end = float(end_ts)
    if end <= cur:
        return []

    resolved_tz = _resolve_tz(tz)
    slices: list[tuple[str, float]] = []
    while cur < end:
        day_key = day_key_for_ts(cur, tz=resolved_tz)
        _, day_end = natural_day_bounds(day_key, tz=resolved_tz)
        if day_end <= cur:
            # Guard against anomalous timezone definitions
            break
        seg_end = min(end, day_end)
        seg_sec = seg_end - cur
        if seg_sec > 0.0:
            slices.append((day_key, seg_sec))
        cur = seg_end
    return slices


def _extract_game_name(raw: Any, fallback: str) -> str:
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    text = str(raw or "").strip()
    return text or fallback


def aggregate_playtime_window(
    closed_sessions: Iterable[Mapping[str, Any]],
    active_sessions: Iterable[ActiveSession | Mapping[str, Any]],
    legacy_daily_rows: Iterable[Mapping[str, Any]],
    *,
    start_day: str,
    end_day: str,
    now_ts: float,
    target_sids: set[str] | Iterable[str] | None,
    tz: tzinfo | None = None,
) -> list[dict[str, Any]]:
    """Aggregate exact float playtime seconds across `[start_day, end_day]` inclusive.

    - Intersects `closed_sessions` with `[start_day 00:00, (end_day + 1) 00:00)`.
    - Clips `active_sessions` to `min(now_ts, window_end)` (or `exited_at` if
      `confirming_exit`), never losing ongoing sessions across midnight.
    - Merges `legacy_daily_rows` whose `day_key` lies in `[start_day, end_day]`.
    - Performs zero `int()` truncation; returns players sorted by `total_seconds` desc.
    """
    start_norm = _normalize_day_str(start_day)
    end_norm = _normalize_day_str(end_day)
    if end_norm < start_norm:
        return []

    resolved_tz = _resolve_tz(tz)
    window_start, _ = natural_day_bounds(start_norm, tz=resolved_tz)
    _, window_end = natural_day_bounds(end_norm, tz=resolved_tz)
    if window_end <= window_start:
        return []

    target_set: set[str] | None
    if target_sids is None:
        target_set = None
    else:
        target_set = {str(sid).strip() for sid in target_sids if str(sid).strip()}
        if not target_set:
            return []

    # per_player[sid][game_id] = {"seconds": float, "name": str, "latest_ts": float}
    per_player: dict[str, dict[str, dict[str, Any]]] = {}

    def _add(
        sid: str,
        game_id: str,
        game_name: str,
        seconds: float,
        order_ts: float,
    ) -> None:
        if seconds <= 0.0 or not sid or not game_id:
            return
        if target_set is not None and sid not in target_set:
            return
        games = per_player.setdefault(sid, {})
        entry = games.get(game_id)
        clean_name = _extract_game_name(game_name, game_id)
        if entry is None:
            games[game_id] = {
                "seconds": float(seconds),
                "name": clean_name,
                "latest_ts": float(order_ts),
            }
        else:
            entry["seconds"] += float(seconds)
            if clean_name != game_id and (
                entry["name"] == game_id or float(order_ts) >= entry["latest_ts"]
            ):
                entry["name"] = clean_name
                entry["latest_ts"] = float(order_ts)

    # 1. Closed sessions
    for row in closed_sessions or ():
        sid = str(row.get("steam_id") or row.get("sid") or "").strip()
        gid = _normalize_game_id(row.get("game_id") or row.get("gameid"))
        if not sid or gid is None:
            continue
        try:
            s_ts = float(row.get("started_at") or 0.0)
            e_ts = float(row.get("ended_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if e_ts <= s_ts:
            continue
        ov_start = max(s_ts, window_start)
        ov_end = min(e_ts, window_end)
        if ov_end > ov_start:
            _add(
                sid,
                gid,
                row.get("game_name") or row.get("name") or gid,
                ov_end - ov_start,
                e_ts,
            )

    # 2. Active sessions (clipped to min(now_ts, window_end))
    now_f = float(now_ts)
    for item in active_sessions or ():
        if isinstance(item, ActiveSession):
            sid = item.steam_id
            gid = _normalize_game_id(item.game_id)
            gname = item.game_name
            s_ts = float(item.started_at)
            state = item.state
            exited_at = item.exited_at
        else:
            sid = str(item.get("steam_id") or item.get("sid") or "").strip()
            gid = _normalize_game_id(item.get("game_id") or item.get("gameid"))
            gname = str(item.get("game_name") or item.get("name") or "")
            try:
                s_ts = float(item.get("started_at") or 0.0)
            except (TypeError, ValueError):
                continue
            state = str(item.get("state") or "playing")
            exited_at = item.get("exited_at")

        if not sid or gid is None:
            continue
        upper = min(now_f, window_end)
        if state == "confirming_exit" and exited_at is not None:
            upper = min(float(exited_at), upper)
        ov_start = max(s_ts, window_start)
        if upper > ov_start:
            _add(sid, gid, gname or gid, upper - ov_start, upper)

    # 3. Legacy daily rows within [start_day, end_day]
    for row in legacy_daily_rows or ():
        day_raw = row.get("day_key") or row.get("date") or ""
        day_norm = _normalize_day_str(str(day_raw))
        if not day_norm or not (start_norm <= day_norm <= end_norm):
            continue
        sid = str(row.get("steam_id") or row.get("sid") or "").strip()
        gid = _normalize_game_id(row.get("game_id") or row.get("gameid"))
        if not sid or gid is None:
            continue
        sec_val = row.get("seconds")
        if sec_val is None:
            min_val = row.get("minutes")
            sec_val = float(min_val) * 60.0 if min_val is not None else 0.0
        try:
            sec_f = float(sec_val)
        except (TypeError, ValueError):
            continue
        if sec_f > 0.0:
            _add(
                sid,
                gid,
                row.get("game_name") or row.get("name") or gid,
                sec_f,
                0.0,
            )

    # Build sorted output list
    results: list[dict[str, Any]] = []
    for sid, games_map in per_player.items():
        game_items = [
            {
                "gameid": gid,
                "name": info["name"],
                "seconds": float(info["seconds"]),
                "minutes": float(info["seconds"]) / 60.0,
            }
            for gid, info in games_map.items()
            if float(info["seconds"]) > 0.0
        ]
        if not game_items:
            continue
        game_items.sort(key=lambda item: (-item["seconds"], item["gameid"]))
        total_seconds = sum(item["seconds"] for item in game_items)
        results.append(
            {
                "sid": sid,
                "total_seconds": total_seconds,
                "total_minutes": total_seconds / 60.0,
                "games": game_items,
                "top_game_id": game_items[0]["gameid"],
            }
        )

    results.sort(key=lambda item: (-item["total_seconds"], item["sid"]))
    return results


def should_skip_game(
    filter_mode: str,
    filter_ids_raw: str,
    game_id: str | None,
) -> bool:
    """Return True if `game_id` should be skipped under the configured filter policy."""
    gid = _normalize_game_id(game_id)
    if gid is None:
        return False

    mode = str(filter_mode or "全部游戏").strip().lower()
    if mode in ("", "全部游戏", "all", "none", "off"):
        return False

    if isinstance(filter_ids_raw, (list, tuple, set, frozenset)):
        tokens = [str(x).strip() for x in filter_ids_raw if str(x).strip()]
    else:
        normalized = (
            str(filter_ids_raw or "")
            .replace("，", ",")
            .replace(";", ",")
            .replace("；", ",")
            .replace("\n", ",")
        )
        tokens = [part.strip() for part in normalized.split(",") if part.strip()]

    if not tokens:
        return False

    allowed_or_blocked = set(tokens)
    if mode in ("白名单", "whitelist", "allowlist"):
        return gid not in allowed_or_blocked
    if mode in ("黑名单", "blacklist", "blocklist", "denylist"):
        return gid in allowed_or_blocked
    return False


def format_duration(seconds: float) -> str:
    """Format duration in seconds into concise readable Chinese text."""
    sec_f = max(0.0, float(seconds or 0.0))
    total_sec = int(round(sec_f))
    if total_sec < 60:
        return f"{total_sec}秒"
    total_min = total_sec // 60
    hours, mins = divmod(total_min, 60)
    if hours == 0:
        return f"{mins}分钟"
    if mins == 0:
        return f"{hours}小时"
    return f"{hours}小时{mins}分钟"


def end_game_tip(duration_min: float) -> str:
    """Return a playful Chinese tip tailored to session duration in minutes."""
    mins = max(0.0, float(duration_min or 0.0))
    if mins < 5.0:
        return "浅尝辄止，热身刚刚结束！"
    if mins < 30.0:
        return "小玩一局，轻松惬意~"
    if mins < 90.0:
        return "恰到好处的游戏时光，记得喝口水休息一下！"
    if mins < 180.0:
        return "火力全开！玩了这么久，起来活动一下肩颈吧~"
    if mins < 360.0:
        return "资深肝帝认证！快去歇歇眼睛，补充点能量吧！"
    return "传世级爆肝现场！地球不爆炸你都不下线，赶紧好好休息！"
