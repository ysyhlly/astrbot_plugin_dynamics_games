"""Global per-player Steam achievement polling and post-exit delayed check service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .steam_client import SteamClient

DEFAULT_POLL_INTERVAL_SECONDS = 180.0
DEFAULT_FINAL_CHECK_DELAY_SECONDS = 90.0
DEFAULT_MAX_NOTIFY_PER_BATCH = 10


@dataclass
class _TrackedSession:
    steam_id: str
    appid: str
    game_name: str
    player_name: str
    avatar_url: str
    baseline: set[str] | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass
class _DelayedCheck:
    steam_id: str
    appid: str
    game_name: str
    player_name: str
    avatar_url: str
    baseline: set[str] | None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class AchievementService:
    """Tracks unlocked achievements globally per `steam_id` across all subscribed groups."""

    def __init__(
        self,
        steam_client: SteamClient,
        config_getter: Callable[[], dict[str, Any]],
        on_unlocked: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._client = steam_client
        self._config_getter = config_getter
        self._on_unlocked = on_unlocked
        self._active: dict[str, _TrackedSession] = {}
        self._delayed: dict[tuple[str, str], _DelayedCheck] = {}
        self._closed = False

    def _cfg(self) -> dict[str, Any]:
        try:
            cfg = self._config_getter()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _is_enabled(self) -> bool:
        return bool(self._cfg().get("enable_achievement_poll", True))

    def _poll_interval(self) -> float:
        try:
            val = float(self._cfg().get("achievement_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            val = DEFAULT_POLL_INTERVAL_SECONDS
        return max(0.01, val)

    def _final_check_delay(self) -> float:
        try:
            val = float(
                self._cfg().get("achievement_final_check_delay_seconds", DEFAULT_FINAL_CHECK_DELAY_SECONDS)
            )
        except (TypeError, ValueError):
            val = DEFAULT_FINAL_CHECK_DELAY_SECONDS
        return max(0.01, val)

    def _max_notify_count(self) -> int:
        try:
            val = int(self._cfg().get("max_achievement_notifications", DEFAULT_MAX_NOTIFY_PER_BATCH))
        except (TypeError, ValueError):
            val = DEFAULT_MAX_NOTIFY_PER_BATCH
        return max(1, min(val, 50))

    def is_tracking(self, steam_id: str) -> bool:
        return str(steam_id or "").strip() in self._active

    def active_count(self) -> int:
        return len(self._active)

    def start_tracking(
        self,
        steam_id: str,
        appid: str,
        game_name: str,
        player_name: str,
        avatar_url: str,
    ) -> None:
        """Start (or refresh metadata for) global achievement tracking for `steam_id`."""
        if self._closed:
            return
        sid = str(steam_id or "").strip()
        gid = str(appid or "").strip()
        if not sid or not gid or not gid.isdigit():
            return

        if not self._is_enabled():
            if sid in self._active:
                self.stop_tracking(sid, delayed_final_check=False)
            return

        existing = self._active.get(sid)
        if existing is not None:
            if existing.appid == gid and existing.task and not existing.task.done():
                existing.game_name = game_name or existing.game_name
                existing.player_name = player_name or existing.player_name
                existing.avatar_url = avatar_url or existing.avatar_url
                return
            # Switched games: finalize previous game with delayed check before starting new one
            self.stop_tracking(sid, delayed_final_check=(existing.appid != gid))

        # If a delayed final check for this exact (sid, gid) is pending, inherit its baseline and cancel it
        inherited_baseline: set[str] | None = None
        pending = self._delayed.pop((sid, gid), None)
        if pending is not None:
            inherited_baseline = set(pending.baseline) if pending.baseline is not None else None
            if pending.task and not pending.task.done():
                pending.task.cancel()

        session = _TrackedSession(
            steam_id=sid,
            appid=gid,
            game_name=game_name or f"App {gid}",
            player_name=player_name or sid,
            avatar_url=avatar_url or "",
            baseline=inherited_baseline,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[AchievementService] 无运行中事件循环，跳过启动成就轮询 (sid=%s, appid=%s)", sid, gid)
            return

        session.task = loop.create_task(
            self._periodic_loop(session),
            name=f"steam-ach-poll-{sid}-{gid}",
        )
        self._active[sid] = session

    def stop_tracking(self, steam_id: str, *, delayed_final_check: bool = True) -> None:
        """Stop periodic polling for `steam_id` and optionally schedule one delayed post-exit check."""
        sid = str(steam_id or "").strip()
        session = self._active.pop(sid, None)
        if session is None:
            return

        if session.task and not session.task.done():
            session.task.cancel()

        if (
            self._closed
            or not delayed_final_check
            or not self._is_enabled()
            or self._client.is_no_achievement_app(session.appid)
        ):
            return

        key = (sid, session.appid)
        old_delayed = self._delayed.pop(key, None)
        if old_delayed is not None and old_delayed.task and not old_delayed.task.done():
            old_delayed.task.cancel()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        delayed = _DelayedCheck(
            steam_id=sid,
            appid=session.appid,
            game_name=session.game_name,
            player_name=session.player_name,
            avatar_url=session.avatar_url,
            baseline=set(session.baseline) if session.baseline is not None else None,
        )
        delayed.task = loop.create_task(
            self._delayed_final_check_task(delayed),
            name=f"steam-ach-final-{sid}-{session.appid}",
        )
        self._delayed[key] = delayed

    def close(self) -> None:
        """Cancel all active periodic tasks and pending delayed checks cleanly."""
        self._closed = True
        for session in list(self._active.values()):
            if session.task and not session.task.done():
                session.task.cancel()
        self._active.clear()

        for delayed in list(self._delayed.values()):
            if delayed.task and not delayed.task.done():
                delayed.task.cancel()
        self._delayed.clear()

    async def aclose(self) -> None:
        """Async variant of close() that also awaits task cancellation."""
        tasks: list[asyncio.Task[None]] = []
        for session in list(self._active.values()):
            if session.task and not session.task.done():
                tasks.append(session.task)
        for delayed in list(self._delayed.values()):
            if delayed.task and not delayed.task.done():
                tasks.append(delayed.task)
        self.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal loops & emission
    # ------------------------------------------------------------------

    async def _periodic_loop(self, session: _TrackedSession) -> None:
        sid = session.steam_id
        gid = session.appid
        try:
            # Establish initial baseline if not inherited from a resumed session
            if session.baseline is None:
                initial = await self._client.fetch_player_achievements(sid, gid)
                if initial is not None:
                    session.baseline = set(initial)
                if self._client.is_no_achievement_app(gid):
                    return

            while not self._closed:
                await asyncio.sleep(self._poll_interval())
                if self._closed or self._active.get(sid) is not session:
                    return
                if not self._is_enabled():
                    continue
                if self._client.is_no_achievement_app(gid):
                    return

                current = await self._client.fetch_player_achievements(sid, gid)
                if current is None:
                    if self._client.is_no_achievement_app(gid):
                        return
                    continue

                if session.baseline is None:
                    # First successful fetch establishes silent baseline
                    session.baseline = set(current)
                    continue

                newly_unlocked = current - session.baseline
                session.baseline = set(current)
                if newly_unlocked:
                    await self._emit_unlocked(
                        steam_id=sid,
                        player_name=session.player_name,
                        avatar_url=session.avatar_url,
                        appid=gid,
                        game_name=session.game_name,
                        newly_unlocked=newly_unlocked,
                        all_unlocked=current,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[AchievementService] 成就轮询循环异常 (sid=%s, appid=%s): %s",
                sid,
                gid,
                exc,
            )
        finally:
            if self._active.get(sid) is session:
                self._active.pop(sid, None)

    async def _delayed_final_check_task(self, delayed: _DelayedCheck) -> None:
        key = (delayed.steam_id, delayed.appid)
        try:
            await asyncio.sleep(self._final_check_delay())
            if self._closed or not self._is_enabled():
                return
            if self._client.is_no_achievement_app(delayed.appid):
                return
            # If the player already resumed the same game, the active loop owns it
            active = self._active.get(delayed.steam_id)
            if active is not None and active.appid == delayed.appid:
                return
            if delayed.baseline is None:
                return

            current = await self._client.fetch_player_achievements(delayed.steam_id, delayed.appid)
            if current is None:
                return

            newly_unlocked = current - delayed.baseline
            if newly_unlocked:
                await self._emit_unlocked(
                    steam_id=delayed.steam_id,
                    player_name=delayed.player_name,
                    avatar_url=delayed.avatar_url,
                    appid=delayed.appid,
                    game_name=delayed.game_name,
                    newly_unlocked=newly_unlocked,
                    all_unlocked=current,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[AchievementService] 结束延迟成就补查异常 (sid=%s, appid=%s): %s",
                delayed.steam_id,
                delayed.appid,
                exc,
            )
        finally:
            if self._delayed.get(key) is delayed:
                self._delayed.pop(key, None)

    async def _emit_unlocked(
        self,
        *,
        steam_id: str,
        player_name: str,
        avatar_url: str,
        appid: str,
        game_name: str,
        newly_unlocked: set[str],
        all_unlocked: set[str],
    ) -> None:
        schema = await self._client.fetch_achievement_schema(appid, lang="schinese")
        items: list[dict[str, Any]] = []
        for apiname in sorted(newly_unlocked):
            info = schema.get(apiname) or {}
            pct = info.get("global_percent")
            items.append(
                {
                    "apiname": apiname,
                    "display_name": str(info.get("display_name") or apiname),
                    "description": str(info.get("description") or ""),
                    "icon": str(info.get("icon") or ""),
                    "percent": pct,
                }
            )

        # Sort rarest achievements first (None at the end)
        items.sort(key=lambda x: (x["percent"] is None, x["percent"] if x["percent"] is not None else 999.0))
        max_n = self._max_notify_count()
        items = items[:max_n]

        payload: dict[str, Any] = {
            "steam_id": steam_id,
            "player_name": player_name,
            "avatar_url": avatar_url,
            "appid": appid,
            "game_name": game_name,
            "unlocked_count": len(all_unlocked),
            "total_count": len(schema) if schema else 0,
            "achievements": items,
        }
        try:
            await self._on_unlocked(payload)
        except Exception as exc:
            logger.warning(
                "[AchievementService] on_unlocked 回调执行失败 (sid=%s, appid=%s): %s",
                steam_id,
                appid,
                exc,
            )
