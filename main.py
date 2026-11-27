"""群间游戏 (Dynamics Games) — 一起完成一件事，统一群聊协作活动与娱乐引擎。"""
# ruff: noqa: E402

from __future__ import annotations

from .reload_bootstrap import ensure_current

ensure_current(
    __package__,
    __name__,
    __file__,
    checks=[
        ("core.coordinator", "ActivityCoordinator", "sync_outcome_records"),
        ("core.store", "ActivityStore", "save_completion"),
    ],
    version="0.1.2",
)

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from importlib import reload
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.star import star_registry

from . import series_panels
from .bridges.social_bridge import emit_entertainment_signal
from .core.commands import (
    build_arcade_text,
    build_help_text,
    handle_cancel_command,
    handle_category_shortcut_with_media,
    handle_cleanup_command,
    handle_complete_with_media,
    handle_create_command,
    handle_detail_command,
    handle_join_command,
    handle_leave_command,
    handle_list_command,
    handle_outcome_command,
    handle_poster_command,
    handle_rebind_command,
    handle_stage_command,
    handle_submit_with_media,
    handle_transfer_command,
    handle_withdraw_command,
)
from .core.coordinator import ActivityCoordinator
from .core.models import ENGINE_CATALOG
from .core.store import ActivityStore
from .core.web_console import GamesWebConsole
from .engines.blackjack21.main import Blackjack21Plugin as Blackjack21Engine
from .engines.rollpig.main import (
    COMMAND_ALIASES as ROLLPIG_ALIASES,
)
from .engines.rollpig.main import (
    COMMAND_NAME as ROLLPIG_CMD,
)
from .engines.rollpig.main import (
    WAKE_FREE_PATTERN as ROLLPIG_WAKE_FREE_PATTERN,
)
from .engines.rollpig.main import (
    RollPigPlugin as RollPigEngine,
)
from .engines.steam_monitor.main import SteamMonitorEngine
from .engines.truth_game.main import TruthGamePlugin as TruthGameEngine
from .engines.what_to_eat.main import WhatToEatPlugin as WhatToEatEngine
from .runtime import LEGACY_PLUGIN_IDS, PLUGIN_ID, ConfigSection, EngineContext

MODULE_FLAG_MAP: dict[str, str] = {
    item["key"]: item["config_flag"] for item in ENGINE_CATALOG
}
MODULE_LABEL_MAP: dict[str, str] = {
    item["key"]: item["label"] for item in ENGINE_CATALOG
}
LEGACY_ENGINE_PLUGIN_MAP: dict[str, str] = {
    "truth_game": "astrbot_plugin_truth_game",
    "blackjack21": "astrbot_plugin_blackjack21",
    "rollpig": "astrbot_plugin_rollpig",
    "what_to_eat": "astrbot_plugin_what_to_eat",
    "steam_monitor": "steam_status_monitor_V3",
}


class DynamicsGamesPlugin(Star):
    """群间游戏：组织共读、游戏约局、接龙创作、小型挑战、共同制作作品与群聊娱乐引擎总成。"""

    VERSION = "0.1.2"
    name = PLUGIN_ID
    author = "ysyhlly"
    plugin_id = f"ysyhlly/{PLUGIN_ID}"

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.raw_config = config if config is not None else {}
        self._config_lock = threading.RLock()
        self.engine_context = EngineContext(self)
        self.store: ActivityStore | None = None
        self.coordinator: ActivityCoordinator | None = None
        self.web_console: GamesWebConsole | None = None
        self.engines: dict[str, Any] = {}
        self._truth_outcome_task: asyncio.Task | None = None
        self._sections: dict[str, ConfigSection] = {}
        self._legacy_conflicts: list[str] = []
        self._scope_version: int = 0
        self._ready = False
        self._closed = False

    def _section(self, key: str, defaults: dict | None = None) -> ConfigSection:
        sec = self._sections.get(key)
        if sec is None:
            sec = ConfigSection(
                self.raw_config, key, defaults=defaults, lock=self._config_lock
            )
            self._sections[key] = sec
        return sec

    def activities_enabled(self) -> bool:
        act = self.raw_config.get("activities")
        if isinstance(act, dict):
            return bool(act.get("enabled", True))
        return bool(self.raw_config.get("enabled", True))

    def _get_config_lock(self) -> threading.RLock:
        lock = getattr(self, "_config_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._config_lock = lock
        return lock

    def _persist_raw_config(self) -> None:
        save_fn = getattr(self.raw_config, "save_config", None)
        if callable(save_fn):
            save_fn()

    def _disabled_groups(self) -> set[str]:
        raw_cfg = self.raw_config if isinstance(self.raw_config, dict) else {}
        act = raw_cfg.get("activities")
        raw_list = (
            act.get("disabled_groups")
            if isinstance(act, dict) and isinstance(act.get("disabled_groups"), list)
            else raw_cfg.get("disabled_groups")
        )
        if not isinstance(raw_list, list):
            return set()
        return {str(item).strip() for item in raw_list if str(item or "").strip()}

    @staticmethod
    def _extract_group_id_from_umo(umo: str) -> str:
        clean = str(umo or "").strip()
        if not clean:
            return ""
        if ":GroupMessage:" in clean:
            return clean.rsplit(":", 1)[-1].strip()
        if ":" in clean:
            return clean.rsplit(":", 1)[-1].strip()
        return clean

    def _is_scope_disabled(self, umo: str, group_id: str = "") -> bool:
        disabled = self._disabled_groups()
        if not disabled:
            return False
        clean_umo = str(umo or "").strip()
        if clean_umo and clean_umo in disabled:
            return True
        gid = str(group_id or "").strip() or self._extract_group_id_from_umo(clean_umo)
        return bool(gid and gid in disabled)

    def _is_scope_disabled_event(self, event: AstrMessageEvent | None) -> bool:
        if event is None:
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        gid = ""
        get_gid = getattr(event, "get_group_id", None)
        if callable(get_gid):
            try:
                gid = str(get_gid() or "").strip()
            except Exception:
                gid = ""
        if not gid:
            gid = str(getattr(event, "group_id", "") or "").strip()
        return self._is_scope_disabled(umo, gid)

    def per_group_modules(self) -> dict[str, dict[str, bool]]:
        raw_cfg = self.raw_config if isinstance(self.raw_config, dict) else {}
        mods = raw_cfg.get("modules")
        raw_map = (
            mods.get("per_group_modules")
            if isinstance(mods, dict) and isinstance(mods.get("per_group_modules"), dict)
            else raw_cfg.get("per_group_modules")
        )
        if not isinstance(raw_map, dict):
            return {}
        out: dict[str, dict[str, bool]] = {}
        for scope_key, val in raw_map.items():
            sk = str(scope_key or "").strip()
            if sk and isinstance(val, dict):
                out[sk] = {
                    str(k): bool(v)
                    for k, v in val.items()
                    if str(k) in MODULE_FLAG_MAP
                }
        return out

    def module_enabled(
        self, key: str, *, umo: str = "", group_id: str = ""
    ) -> bool:
        flag = MODULE_FLAG_MAP.get(key, f"enable_{key}")
        mods = self.raw_config.get("modules")
        if isinstance(mods, dict) and flag in mods:
            global_on = bool(mods.get(flag, True))
        else:
            global_on = bool(self.raw_config.get(flag, True))
        if not global_on:
            return False
        clean_umo = str(umo or "").strip()
        gid = str(group_id or "").strip() or self._extract_group_id_from_umo(clean_umo)
        if clean_umo or gid:
            pg = self.per_group_modules()
            if clean_umo and clean_umo in pg and key in pg[clean_umo]:
                return bool(pg[clean_umo][key])
            if gid and gid in pg and key in pg[gid]:
                return bool(pg[gid][key])
        return True

    def set_module_enabled(self, key: str, enabled: bool) -> None:
        if key not in MODULE_FLAG_MAP:
            raise ValueError(f"未知模块: {key}")
        flag = MODULE_FLAG_MAP[key]
        with self._get_config_lock():
            mods = self.raw_config.get("modules")
            if not isinstance(mods, dict):
                mods = {}
                self.raw_config["modules"] = mods
            mods[flag] = bool(enabled)
            self._persist_raw_config()

    def set_group_module_enabled(
        self, umo: str, key: str, enabled: bool | None
    ) -> None:
        """Persist a per-group override for an entertainment engine module."""
        if key not in MODULE_FLAG_MAP:
            raise ValueError(f"未知模块: {key}")
        target = str(umo or "").strip()
        if not target:
            raise ValueError("缺少目标群 UMO")
        with self._get_config_lock():
            mods = self.raw_config.get("modules")
            if not isinstance(mods, dict):
                mods = {}
                self.raw_config["modules"] = mods
            pg = mods.get("per_group_modules")
            if not isinstance(pg, dict):
                pg = {}
                mods["per_group_modules"] = pg
            group_map = pg.get(target)
            if not isinstance(group_map, dict):
                group_map = {}
                pg[target] = group_map
            if enabled is None:
                group_map.pop(key, None)
                if not group_map:
                    pg.pop(target, None)
            else:
                group_map[key] = bool(enabled)
            self._persist_raw_config()

    def module_states(
        self, *, umo: str = "", group_id: str = ""
    ) -> dict[str, bool]:
        return {
            k: self.module_enabled(k, umo=umo, group_id=group_id)
            for k in MODULE_FLAG_MAP
        }

    def _detect_legacy_conflicts(self) -> list[str]:
        found: list[str] = []
        getter = getattr(self.context, "get_registered_star", None)
        for pid in sorted(LEGACY_PLUGIN_IDS):
            if callable(getter):
                existing = getter(pid)
                if existing is not None and getattr(existing, "activated", False):
                    found.append(pid)
                    continue
            for existing in star_registry:
                if (
                    getattr(existing, "name", None) == pid
                    and getattr(existing, "activated", False)
                    and getattr(existing, "star_cls", None) is not None
                ):
                    found.append(pid)
                    break
        return found

    async def initialize(self) -> None:
        conflicts = self._detect_legacy_conflicts()
        if conflicts:
            self._legacy_conflicts = conflicts
            self._ready = False
            logger.warning(
                "[DynamicsGames] 检测到已启用的旧插件 (%s)，已进入降级待机模式；停用旧插件后重载即可恢复。",
                ", ".join(conflicts),
            )
            return
        try:
            reload(series_panels).register_series_panels(self, PLUGIN_ID)
            db_path = StarTools.get_data_dir(PLUGIN_ID) / "dynamics_games.db"
            self.store = await asyncio.to_thread(ActivityStore, db_path)
            self.coordinator = ActivityCoordinator(
                self.store, self.context, self.raw_config, plugin=self
            )
            self.web_console = GamesWebConsole(self)
            self.web_console.register()

            engine_builders: tuple[tuple[str, Any], ...] = (
                ("truth_game", TruthGameEngine),
                ("blackjack21", Blackjack21Engine),
                ("rollpig", RollPigEngine),
                ("what_to_eat", WhatToEatEngine),
                ("steam_monitor", SteamMonitorEngine),
            )
            for key, cls in engine_builders:
                sec = self._section(key)
                inst = cls(self.engine_context, sec)
                if key == "truth_game":
                    inst.room_manager.outcome_callback = self.coordinator.enqueue_truth_outcome
                elif key == "blackjack21":
                    inst.outcome_callback = self.coordinator.record_minigame_outcome
                elif key == "what_to_eat":
                    inst.signal_callback = self._emit_engine_signal
                init_fn = getattr(inst, "initialize", None)
                if callable(init_fn):
                    res = init_fn()
                    if asyncio.iscoroutine(res):
                        await res
                self.engines[key] = inst

            self._truth_outcome_task = asyncio.create_task(
                self.coordinator.run_truth_outcomes(self.engines["truth_game"].room_manager)
            )
            self._ready = True
            logger.info(
                "[DynamicsGames] 群间游戏 v%s 初始化完成（协作活动库 + 5 大娱乐引擎已就绪）",
                self.VERSION,
            )
        except BaseException:
            await self.terminate()
            raise

    async def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        try:
            if self._truth_outcome_task is not None:
                self._truth_outcome_task.cancel()
                await asyncio.gather(self._truth_outcome_task, return_exceptions=True)
            if getattr(self, "_series_panels", None) is not None:
                self._series_panels.close()
            if self.web_console is not None:
                self.web_console.close()
            for key, inst in list(self.engines.items()):
                term_fn = getattr(inst, "terminate", None)
                if callable(term_fn):
                    try:
                        res = term_fn()
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        logger.exception("[DynamicsGames] 引擎 %s 关闭异常", key)
            self.engines.clear()
            if self.store is not None:
                await asyncio.to_thread(self.store.close)
        finally:
            self.engine_context.release()

    # ------------------------------------------------------------------
    # Public Series Contract
    # ------------------------------------------------------------------
    def get_dynamics_capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider_id": PLUGIN_ID,
            "display_name": "群间游戏",
            "enabled": self.activities_enabled(),
            "ready": self._ready and not self._closed and self.store is not None,
            "capabilities": {"games": 1, "activities": 1},
            "api_version": 1,
        }

    async def get_group_activity_context(self, umo: str) -> dict[str, Any]:
        if not self._ready or self.coordinator is None:
            return {"schema_version": 1, "umo": umo, "active_count": 0, "active_activities": []}
        return await self.coordinator.get_group_activity_context(umo)

    @staticmethod
    def _tz_shanghai() -> timezone:
        return timezone(timedelta(hours=8))

    def _series_tz(self):
        return series_panels.resolve_series_timezone(getattr(self, "context", None))

    def _is_event_excluded_by_core(self, event: AstrMessageEvent | None) -> bool:
        if event is None:
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        get_gid = getattr(event, "get_group_id", None)
        gid = ""
        if callable(get_gid):
            try:
                gid = str(get_gid() or "").strip()
            except Exception:
                gid = ""
        if not gid:
            gid = str(getattr(event, "group_id", "") or "").strip()
        return series_panels.is_series_group_excluded(
            getattr(self, "context", None), umo=umo, group_id=gid
        )

    def _is_in_quiet_hours(self) -> bool:
        return series_panels.is_series_in_quiet_hours(getattr(self, "context", None))

    def _can_proactive_broadcast(self, *, umo: str = "", group_id: str = "") -> bool:
        """Gate helper for background/timeout proactive broadcasts."""
        if self._closed or not self._ready or self._legacy_conflicts:
            return False
        if (umo or group_id) and series_panels.is_series_group_excluded(
            getattr(self, "context", None), umo=umo, group_id=group_id
        ):
            return False
        return not self._is_in_quiet_hours()

    async def _emit_engine_signal(self, **kwargs: Any) -> dict[str, Any]:
        return await emit_entertainment_signal(self.context, **kwargs)

    @staticmethod
    def _event_umo_and_gid(event: AstrMessageEvent | None) -> tuple[str, str]:
        if event is None:
            return "", ""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        gid = ""
        get_gid = getattr(event, "get_group_id", None)
        if callable(get_gid):
            try:
                gid = str(get_gid() or "").strip()
            except Exception:
                gid = ""
        if not gid:
            gid = str(getattr(event, "group_id", "") or "").strip()
        return umo, gid

    def _guard_event(
        self,
        event: AstrMessageEvent | None,
        key: str,
        *,
        explicit: bool,
    ) -> tuple[Any | None, Any | None]:
        """Unified event gating distinguishing explicit commands from passive listeners.

        - Both explicit commands and passive listeners honor per-group module switches
          (``per_group_modules``), scope disables, and Chat Dynamics ``exclude_groups``.
        - Passive listeners also silently drop during ``quiet_hours``.
        """
        ev_umo, ev_gid = self._event_umo_and_gid(event)
        if explicit:
            if self._legacy_conflicts:
                if LEGACY_ENGINE_PLUGIN_MAP.get(key) in self._legacy_conflicts:
                    return None, None
                conflicts = "、".join(self._legacy_conflicts)
                return None, (
                    event.plain_result(
                        f"群间游戏当前因检测到旧版独立插件（{conflicts}）处于待机模式，停用旧插件并重载后即可启用。"
                    )
                    if event is not None
                    else None
                )
            if not self._ready or self._closed:
                return None, (
                    event.plain_result("群间游戏尚未就绪，请稍后再试。")
                    if event is not None
                    else None
                )
            if self._is_event_excluded_by_core(event):
                return None, (
                    event.plain_result("本群已被对话主控排除，群间游戏已停用。")
                    if event is not None
                    else None
                )
            if self._is_scope_disabled_event(event):
                return None, (
                    event.plain_result(
                        "群间游戏已在本群停用，可在群间控制台中重新开启。"
                    )
                    if event is not None
                    else None
                )
            if not self.module_enabled(key, umo=ev_umo, group_id=ev_gid):
                return None, (
                    self._disabled_reply(event, key) if event is not None else None
                )
            eng = self.engines.get(key)
            if eng is None:
                return None, (
                    event.plain_result("群间游戏尚未就绪，请稍后再试。")
                    if event is not None
                    else None
                )
            return eng, None

        if (
            not self._ready
            or self._closed
            or self._legacy_conflicts
            or not self.module_enabled(key, umo=ev_umo, group_id=ev_gid)
        ):
            return None, None
        if self._is_event_excluded_by_core(event) or self._is_scope_disabled_event(
            event
        ):
            return None, None
        if self._is_in_quiet_hours():
            return None, None
        return self.engines.get(key), None

    def _guard_passive_event(
        self, event: AstrMessageEvent | None, key: str
    ) -> Any | None:
        eng, _ = self._guard_event(event, key, explicit=False)
        return eng

    def get_series_health(self) -> dict[str, Any]:
        """Strictly synchronous, read-only health diagnostics for series panel."""
        providers: list[dict[str, Any]] = []
        issues: list[str] = []
        metrics: dict[str, Any] = {}
        ready = bool(
            self._ready
            and not self._closed
            and not self._legacy_conflicts
            and self.store is not None
        )
        if self._legacy_conflicts:
            issues.append(f"检测到旧版娱乐插件冲突: {', '.join(self._legacy_conflicts)}")
        elif self._closed:
            issues.append("群间游戏已关闭")
        elif not self._ready or self.store is None:
            issues.append("群间游戏尚未就绪")

        ctx = getattr(self, "context", None)

        def _inspect_provider(
            role: str, explicit: str, enabled: bool, disabled_detail: str
        ) -> None:
            if not enabled:
                providers.append(
                    {
                        "role": role,
                        "provider_id": str(explicit or "").strip(),
                        "status": "disabled",
                        "detail": disabled_detail,
                    }
                )
                return
            own = str(explicit or "").strip()
            resolved = series_panels.resolve_series_provider(ctx, own, role="reply")
            if not resolved:
                providers.append(
                    {
                        "role": role,
                        "provider_id": "",
                        "status": "session_default",
                        "detail": "跟随会话默认模型",
                    }
                )
                return
            prov = series_panels.find_provider_silently(ctx, resolved)
            if prov is not None:
                providers.append(
                    {
                        "role": role,
                        "provider_id": resolved,
                        "status": "ok",
                        "detail": "显式配置可用" if own else "继承主控回复模型可用",
                    }
                )
            else:
                providers.append(
                    {
                        "role": role,
                        "provider_id": resolved,
                        "status": "missing",
                        "detail": "未在已加载 Provider 中找到（可能冷启动或已移除）",
                    }
                )
                issues.append(f"{role} 配置的 Provider 不可用: {resolved}")

        raw_cfg = self.raw_config if isinstance(self.raw_config, dict) else {}
        wte_cfg = raw_cfg.get("what_to_eat")
        if not isinstance(wte_cfg, dict):
            wte_cfg = {}
        wte_enabled = self.module_enabled("what_to_eat") and bool(
            wte_cfg.get("llm_enabled", True)
        )
        _inspect_provider(
            "what_to_eat_llm",
            str(wte_cfg.get("llm_provider_id", "") or ""),
            wte_enabled,
            "吃什么模块或 LLM 未启用",
        )

        tg_cfg = raw_cfg.get("truth_game")
        if not isinstance(tg_cfg, dict):
            tg_cfg = {}
        tg_source = str(tg_cfg.get("question_source", "llm_first") or "llm_first")
        tg_enabled = self.module_enabled("truth_game") and tg_source != "bank_only"
        _inspect_provider(
            "truth_game_llm",
            str(tg_cfg.get("llm_provider_id", "") or ""),
            tg_enabled,
            "真心话模块关闭或仅题库模式",
        )

        try:
            tg_eng = self.engines.get("truth_game")
            bj_eng = self.engines.get("blackjack21")
            active_tg = len(
                getattr(getattr(tg_eng, "room_manager", None), "rooms", {}) or {}
            )
            active_bj = len(getattr(bj_eng, "rooms", {}) or {})
            metrics["active_rooms"] = active_tg + active_bj
            metrics["modules_enabled"] = sum(
                1 for v in self.module_states().values() if v
            )
            if self.store is not None and not self._closed:
                stats = self.store.stats_summary()
                metrics["open_activities"] = int(stats.get("open_activities") or 0)
                metrics["completed_activities"] = int(
                    stats.get("completed_activities") or 0
                )
        except Exception:
            pass

        return {
            "ready": ready,
            "degraded": bool(issues) or not ready,
            "providers": providers,
            "issues": issues,
            "metrics": metrics,
        }

    @classmethod
    def _iso_time(cls, ts: float | None = None, *, tz=None) -> str:
        eff_tz = tz or cls._tz_shanghai()
        dt = (
            datetime.fromtimestamp(float(ts), tz=eff_tz)
            if ts is not None
            else datetime.now(tz=eff_tz)
        )
        return dt.isoformat(timespec="seconds")

    @classmethod
    def _date_window(cls, date_str: str = "today", *, tz=None) -> tuple[float, float]:
        eff_tz = tz or cls._tz_shanghai()
        raw = str(date_str or "today").strip().lower()
        now = datetime.now(tz=eff_tz)
        if not raw or raw == "today":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif raw == "yesterday":
            start = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            try:
                parsed = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=eff_tz)
                start = parsed
            except ValueError:
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp()

    def get_series_summary(self) -> dict[str, Any]:
        """Synchronous, read-only v2 series summary for presence badge/pulse."""
        now_iso = self._iso_time(tz=self._series_tz())
        try:
            if self._legacy_conflicts:
                cnt = len(self._legacy_conflicts)
                return {
                    "badge": {
                        "count": cnt,
                        "tone": "danger",
                        "label": f"{cnt} 项旧插件冲突",
                    },
                    "pulse": {
                        "headline": "检测到旧版娱乐插件冲突，停用后重载可恢复",
                        "updated_at": now_iso,
                    },
                }
            if self._closed or not self._ready or self.store is None:
                return {
                    "badge": {"count": 0, "tone": "muted", "label": "待就绪"},
                    "pulse": {"headline": "群间游戏尚未就绪", "updated_at": now_iso},
                }
            stats = self.store.stats_summary()
            open_cnt = int(stats.get("open_activities") or 0)
            completed_cnt = int(stats.get("completed_activities") or 0)
            active_rooms = 0
            for key in ("truth_game", "blackjack21"):
                eng = self.engines.get(key)
                if eng is None:
                    continue
                if key == "truth_game":
                    rm = getattr(eng, "room_manager", None)
                    active_rooms += len(getattr(rm, "rooms", {}) or {})
                else:
                    active_rooms += len(getattr(eng, "rooms", {}) or {})
            total_active = open_cnt + active_rooms
            if total_active > 0:
                label = (
                    f"{open_cnt} 局进行中"
                    if open_cnt > 0
                    else f"{active_rooms} 房娱乐中"
                )
                headline = (
                    f"{open_cnt} 项协作进行中 · 累计 {completed_cnt} 份成果"
                    if open_cnt > 0
                    else f"{active_rooms} 个娱乐房间进行中 · 累计 {completed_cnt} 份成果"
                )
                return {
                    "badge": {"count": total_active, "tone": "info", "label": label},
                    "pulse": {"headline": headline, "updated_at": now_iso},
                }
            return {
                "badge": {"count": 0, "tone": "accent", "label": "空闲"},
                "pulse": {
                    "headline": f"暂无进行中协作 · 累计 {completed_cnt} 份成果",
                    "updated_at": now_iso,
                },
            }
        except Exception:
            return {
                "badge": {"count": 0, "tone": "muted", "label": "暂不可用"},
                "pulse": {"headline": "群间游戏摘要暂不可用", "updated_at": now_iso},
            }

    def get_series_scope_status(self, umo: str) -> dict[str, Any]:
        """Per-group status for Chat Dynamics v2 group control matrix."""
        target = str(umo or "").strip()
        scope_ver = int(getattr(self, "_scope_version", 0) or 0)
        try:
            ready = bool(self._ready and not self._closed and self.store is not None)
            global_enabled = bool(self.activities_enabled()) if ready else False
            if not ready or self.store is None:
                detail = "群间游戏未就绪"
                return {
                    "umo": target,
                    "enabled": None if not ready else global_enabled,
                    "writable": False,
                    "mode": "denylist",
                    "supported": False,
                    "persona_id": "",
                    "activity_today": None,
                    "summary": detail,
                    "detail": detail,
                    "version": f"0:{scope_ver}:0:0",
                }
            stats = self.store.stats_summary(target)
            open_cnt = int(stats.get("open_activities") or 0)
            completed_cnt = int(stats.get("completed_activities") or 0)
            tz = self._series_tz()
            since_ts, _ = self._date_window("today", tz=tz)
            today_cnt = self.store.activity_count_since(since_ts, umo=target)
            excluded_by_core = bool(
                target
                and series_panels.is_series_group_excluded(
                    getattr(self, "context", None), umo=target
                )
            )
            if excluded_by_core:
                detail = "主控排除（群间游戏被动触发已静默）"
                return {
                    "umo": target,
                    "enabled": False,
                    "writable": False,
                    "mode": "excluded_by_core",
                    "supported": False,
                    "excluded_by_core": True,
                    "persona_id": "",
                    "activity_today": today_cnt,
                    "summary": detail,
                    "detail": detail,
                    "version": f"0:{scope_ver}:{open_cnt}:{completed_cnt}",
                }
            group_disabled = bool(target and self._is_scope_disabled(target))
            effective_enabled = bool(global_enabled and not group_disabled)
            if effective_enabled:
                detail = f"{open_cnt} 项进行中 · {completed_cnt} 份成果"
            elif group_disabled:
                detail = "本群已停用群间游戏"
            else:
                detail = "协作活动已关闭"
            return {
                "umo": target,
                "enabled": effective_enabled,
                "writable": True,
                "mode": "denylist",
                "supported": True,
                "excluded_by_core": False,
                "persona_id": "",
                "activity_today": today_cnt,
                "summary": detail,
                "detail": detail,
                "version": f"{int(effective_enabled)}:{scope_ver}:{open_cnt}:{completed_cnt}",
            }
        except Exception:
            detail = "状态暂不可用"
            return {
                "umo": target,
                "enabled": None,
                "writable": False,
                "mode": "denylist",
                "supported": False,
                "persona_id": "",
                "activity_today": None,
                "summary": detail,
                "detail": detail,
                "version": f"0:{scope_ver}:0:0",
            }

    async def set_series_scope_enabled(
        self,
        umo: str,
        enabled: bool,
        expected_version: str | int | None = None,
    ) -> dict[str, Any]:
        """Optimistic-locked toggle for enabling/disabling Dynamics Games in a specific group UMO."""
        target = str(umo or "").strip()
        if not target:
            return {
                "ok": False,
                "umo": "",
                "enabled": False,
                "version": "0",
                "reason": "invalid_umo",
                "summary": "缺少目标群 UMO",
            }

        def _apply() -> dict[str, Any]:
            with self._get_config_lock():
                current = self.get_series_scope_status(target)
                current_version = str(current.get("version") or "0")
                current_enabled = bool(current.get("enabled") or False)
                if not self._ready or self._closed or self.store is None:
                    return {
                        "ok": False,
                        "umo": target,
                        "enabled": current_enabled,
                        "version": current_version,
                        "reason": "not_ready",
                        "summary": "群间游戏尚未就绪",
                    }
                if (
                    expected_version is not None
                    and str(expected_version).strip() != ""
                    and str(expected_version).strip() != current_version
                ):
                    return {
                        "ok": False,
                        "conflict": True,
                        "umo": target,
                        "enabled": current_enabled,
                        "version": current_version,
                        "reason": "version_conflict",
                        "summary": "群间游戏群控状态已变更，请刷新后重试",
                    }
                want_enabled = bool(enabled)
                if current.get("excluded_by_core") and want_enabled:
                    return {
                        "ok": False,
                        "umo": target,
                        "enabled": False,
                        "version": current_version,
                        "reason": "excluded_by_core",
                        "summary": "该群已被对话主控排除，请先在对话主控取消排除后再启用",
                    }

                act = self.raw_config.get("activities")
                if not isinstance(act, dict):
                    act = {"enabled": bool(self.raw_config.get("enabled", True))}
                    self.raw_config["activities"] = act
                disabled = self._disabled_groups()
                gid = self._extract_group_id_from_umo(target)
                if want_enabled:
                    disabled.discard(target)
                    if gid:
                        disabled.discard(gid)
                    if not bool(act.get("enabled", True)):
                        act["enabled"] = True
                else:
                    disabled.add(target)
                act["disabled_groups"] = sorted(disabled)
                self._scope_version = int(getattr(self, "_scope_version", 0) or 0) + 1
                self._persist_raw_config()

                updated = self.get_series_scope_status(target)
                return {
                    "ok": True,
                    "umo": target,
                    "enabled": bool(updated.get("enabled") or False),
                    "version": str(updated.get("version") or "0"),
                    "reason": "ok",
                    "summary": str(updated.get("summary") or ""),
                }

        return await asyncio.to_thread(_apply)

    def activate_series_runtime(self) -> dict[str, Any]:
        """Activate Dynamics Games runtime gate for the Series Setup Wizard."""
        if self._legacy_conflicts:
            detail = f"检测到旧版娱乐插件冲突: {', '.join(self._legacy_conflicts)}"
            return {
                "ok": False,
                "activated": False,
                "ready": False,
                "enabled": False,
                "reason": "legacy_conflicts",
                "detail": detail,
            }
        if self._closed:
            return {
                "ok": False,
                "activated": False,
                "ready": False,
                "enabled": False,
                "reason": "closed",
                "detail": "群间游戏已关闭",
            }
        with self._get_config_lock():
            act = self.raw_config.get("activities")
            if not isinstance(act, dict):
                act = {}
                self.raw_config["activities"] = act
            act["enabled"] = True
            self._scope_version = int(getattr(self, "_scope_version", 0) or 0) + 1
            self._persist_raw_config()
        ready = bool(self._ready and not self._closed and self.store is not None)
        return {
            "ok": True,
            "activated": True,
            "ready": ready,
            "enabled": True,
            "reason": "ok",
            "detail": "群间游戏运行态已激活" if ready else "已开启运行开关（等待初始化就绪）",
        }

    def get_active_game_state(self, umo: str) -> dict[str, Any]:
        """Aggregate active collaborative activities and live room state across all 5 engines.

        Used by Chat Dynamics to switch into companion mode and avoid interrupting live games.
        """
        target = str(umo or "").strip()
        gid = self._extract_group_id_from_umo(target)
        empty = {
            "schema_version": 1,
            "umo": target,
            "active": False,
            "engines": [],
            "engine_rooms": {},
            "activities": [],
            "companion_mode_hint": "",
        }
        if self._closed or not self._ready or not target:
            return empty
        if self._is_scope_disabled(target, gid):
            return empty

        activities: list[dict[str, Any]] = []
        if self.store is not None and self.activities_enabled():
            try:
                open_items = self.store.list_activities(
                    umo=target, status="open", limit=10
                )
                activities = [
                    {
                        "id": item["id"],
                        "category": item["category"],
                        "category_label": item["category_label"],
                        "title": item["title"],
                        "status": item["status"],
                        "current_stage": item["current_stage"],
                        "participants": int(item.get("participant_count") or 0),
                        "contributions": int(item.get("contribution_count") or 0),
                        "progress_pct": int(item.get("progress_pct") or 0),
                    }
                    for item in open_items
                ]
            except Exception:
                activities = []

        active_engines: list[str] = []
        playing_labels: list[str] = []
        waiting_labels: list[str] = []
        engine_rooms: dict[str, dict[str, Any]] = {}

        # 1. truth_game
        tg_eng = self.engines.get("truth_game")
        tg_room = None
        if tg_eng is not None and self.module_enabled("truth_game"):
            rm = getattr(tg_eng, "room_manager", None)
            rooms = getattr(rm, "rooms", {}) or {}
            tg_room = rooms.get(target)
            if tg_room is None and gid:
                cand = rooms.get(gid)
                if cand is not None and (not getattr(cand, "umo", "") or cand.umo == target):
                    tg_room = cand
        if tg_room is not None and getattr(tg_room, "phase", "") in ("waiting", "playing"):
            phase = str(tg_room.phase)
            active_engines.append("truth_game")
            (playing_labels if phase == "playing" else waiting_labels).append("真心话")
            engine_rooms["truth_game"] = {
                "active": True,
                "phase": phase,
                "round_stage": str(getattr(tg_room, "round_stage", "") or ""),
                "players": len(getattr(tg_room, "players", {}) or {}),
                "advance_count": int(getattr(tg_room, "advance_count", 0) or 0),
            }
        else:
            engine_rooms["truth_game"] = {"active": False, "phase": "idle", "players": 0}

        # 2. blackjack21
        bj_eng = self.engines.get("blackjack21")
        bj_room = None
        if bj_eng is not None and self.module_enabled("blackjack21"):
            rooms = getattr(bj_eng, "rooms", {}) or {}
            bj_room = rooms.get(target)
            if bj_room is None and gid:
                cand = rooms.get(gid)
                if cand is not None and str(getattr(cand, "room_key", "")) in (target, gid):
                    bj_room = cand
        if bj_room is not None and getattr(bj_room, "phase", "") in ("waiting", "playing"):
            phase = str(bj_room.phase)
            active_engines.append("blackjack21")
            (playing_labels if phase == "playing" else waiting_labels).append("21点")
            engine_rooms["blackjack21"] = {
                "active": True,
                "phase": phase,
                "round_no": int(getattr(bj_room, "round_no", 0) or 0),
                "players": len(getattr(bj_room, "players", {}) or {}),
                "joker_mode": bool(getattr(bj_room, "joker_mode", False)),
            }
        else:
            engine_rooms["blackjack21"] = {"active": False, "phase": "idle", "players": 0}

        # 3. rollpig (stateless daily draw)
        engine_rooms["rollpig"] = {
            "active": False,
            "enabled": bool(self.engines.get("rollpig") is not None and self.module_enabled("rollpig")),
        }

        # 4. what_to_eat (stateless food recommendation)
        engine_rooms["what_to_eat"] = {
            "active": False,
            "enabled": bool(
                self.engines.get("what_to_eat") is not None
                and self.module_enabled("what_to_eat")
            ),
        }

        # 5. steam_monitor (background Steam playing presence in this group)
        sm_eng = self.engines.get("steam_monitor")
        sm_playing_games: list[str] = []
        sm_playing_count = 0
        sm_enabled = bool(sm_eng is not None and self.module_enabled("steam_monitor"))
        if sm_enabled and gid:
            sm_store = getattr(sm_eng, "store", None)
            if sm_store is not None:
                try:
                    group_players = set(sm_store.list_group_players(gid) or [])
                    if group_players:
                        active_map = sm_store.load_active_sessions() or {}
                        for sid, sess in active_map.items():
                            if str(sid) in group_players and getattr(sess, "state", "") == "playing":
                                sm_playing_count += 1
                                gname = str(getattr(sess, "game_name", "") or "").strip()
                                if gname and gname not in sm_playing_games:
                                    sm_playing_games.append(gname)
                except Exception:
                    pass
        engine_rooms["steam_monitor"] = {
            "active": sm_playing_count > 0,
            "enabled": sm_enabled,
            "playing_count": sm_playing_count,
            "playing_games": sm_playing_games[:6],
        }

        active = bool(active_engines or activities)
        hint = ""
        if playing_labels:
            hint = (
                f"room_playing: 本群正在进行{'、'.join(playing_labels)}对局，"
                "请切入陪玩态（降低主动插话频率，不打断回合节奏，仅在被呼唤或自然接茬时简短回应）。"
            )
        elif waiting_labels:
            hint = (
                f"room_waiting: 本群有{'、'.join(waiting_labels)}房间正在等人开局，"
                "可轻松附和招人，避免长篇打岔。"
            )
        elif activities:
            titles = "、".join(f"《{a['title']}》" for a in activities[:3])
            hint = (
                f"activity_active: 本群正在开展协作活动（{titles}），"
                "可自然呼应活动进展，勿喧宾夺主。"
            )

        return {
            "schema_version": 1,
            "umo": target,
            "active": active,
            "engines": active_engines,
            "engine_rooms": engine_rooms,
            "activities": activities,
            "companion_mode_hint": hint,
        }

    async def get_active_game_state_async(self, umo: str) -> dict[str, Any]:
        """Offload get_active_game_state SQLite reads to a worker thread."""
        return await asyncio.to_thread(self.get_active_game_state, umo)

    async def create_activity_from_companion(
        self,
        umo: str,
        category: str,
        title: str,
        *,
        description: str = "",
        creator_name: str = "",
        source_plugin: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a collaborative group activity on behalf of a companion plugin (e.g. Horizons)."""
        clean_umo = str(umo or "").strip()
        meta_dict = dict(meta) if isinstance(meta, dict) else {}
        gid = (
            str(meta_dict.get("group_id") or "").strip()
            or self._extract_group_id_from_umo(clean_umo)
        )
        if not self._ready or self._closed or self.coordinator is None or self.store is None:
            return {
                "ok": False,
                "created": False,
                "reason": "not_ready",
                "message": "群间游戏尚未就绪",
                "activity_id": "",
                "activity": None,
            }
        if not clean_umo or not gid:
            return {
                "ok": False,
                "created": False,
                "reason": "invalid_umo",
                "message": "缺少有效的群会话 UMO",
                "activity_id": "",
                "activity": None,
            }
        if not self.activities_enabled() or self._is_scope_disabled(clean_umo, gid):
            return {
                "ok": False,
                "created": False,
                "reason": "disabled",
                "message": "群间协作活动在当前群未开启",
                "activity_id": "",
                "activity": None,
            }

        clean_title = str(title or "").strip()
        if not clean_title:
            return {
                "ok": False,
                "created": False,
                "reason": "invalid_title",
                "message": "活动标题不能为空",
                "activity_id": "",
                "activity": None,
            }

        from .core.models import resolve_category

        resolved_cat = resolve_category(str(category or ""))
        if not resolved_cat or resolved_cat == "mini_game":
            return {
                "ok": False,
                "created": False,
                "reason": "invalid_category",
                "message": f"未知活动类型: {category}",
                "activity_id": "",
                "activity": None,
            }

        if meta_dict.get("dedupe", True):
            open_acts = await asyncio.to_thread(
                self.store.list_activities,
                umo=clean_umo,
                category=resolved_cat,
                status="open",
                limit=20,
            )
            title_norm = clean_title.lower()
            for existing in open_acts:
                if str(existing.get("title") or "").strip().lower() == title_norm:
                    return {
                        "ok": True,
                        "created": False,
                        "reason": "already_exists",
                        "message": f"本群已有同名进行中的活动《{existing['title']}》",
                        "activity_id": existing["id"],
                        "activity": existing,
                        "source_plugin": str(source_plugin or ""),
                        "meta": meta_dict,
                    }

        clean_source = str(source_plugin or "").strip()
        clean_desc = str(description or "").strip()
        if clean_source and clean_source not in clean_desc:
            suffix = f"（由 {clean_source} 联动发起）"
            clean_desc = f"{clean_desc} {suffix}".strip() if clean_desc else suffix

        creator_uid = str(
            meta_dict.get("creator_uid")
            or meta_dict.get("uid")
            or f"companion:{clean_source or 'series'}"
        ).strip()
        resolved_creator_name = str(
            creator_name or meta_dict.get("creator_name") or clean_source or "群间伴生"
        ).strip()

        def _opt_int(val: Any) -> int | None:
            if val is None or val == "":
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        ok, msg, act = await self.coordinator.create_activity(
            umo=clean_umo,
            group_id=gid,
            category_raw=resolved_cat,
            title=clean_title,
            description=clean_desc,
            creator_uid=creator_uid,
            creator_name=resolved_creator_name,
            stages=meta_dict.get("stages"),
            target_count=_opt_int(meta_dict.get("target_count")),
            min_participants=_opt_int(meta_dict.get("min_participants")),
        )
        if not ok or act is None:
            return {
                "ok": False,
                "created": False,
                "reason": "create_failed",
                "message": msg,
                "activity_id": "",
                "activity": None,
            }
        return {
            "ok": True,
            "created": True,
            "reason": "ok",
            "message": msg,
            "activity_id": act["id"],
            "activity": act,
            "source_plugin": clean_source,
            "meta": meta_dict,
        }

    def series_search_items(
        self, query: str, scope: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        """Read-only federated search hook for Ctrl+K command palette."""
        clean = str(query or "").strip()
        if not clean or self._closed:
            return []
        cap = max(1, min(50, int(limit or 8)))
        target_scope = str(scope or "").strip()
        items: list[dict[str, Any]] = []
        try:
            if self._ready and self.store is not None:
                for hit in self.store.search_items(clean, umo=target_scope, limit=cap):
                    rec_type = hit.get("record_type")
                    data = hit.get("data") or {}
                    if rec_type == "activity":
                        aid = str(data.get("id") or "")
                        items.append(
                            {
                                "kind": "协作活动",
                                "title": f"[{aid}] {data.get('title') or ''}".strip(),
                                "subtitle": (
                                    f"{data.get('category_label') or '协作'} · "
                                    f"{data.get('status_label') or ''} · "
                                    f"{data.get('current_stage') or ''} · "
                                    f"{int(data.get('participant_count') or 0)} 人"
                                ),
                                "plugin_id": PLUGIN_ID,
                                "page": "games",
                                "deep_link": {
                                    "tab": "activities",
                                    "id": aid,
                                    "umo": str(data.get("umo") or ""),
                                },
                            }
                        )
                    elif rec_type == "outcome":
                        aid = str(data.get("activity_id") or "")
                        items.append(
                            {
                                "kind": "活动成果",
                                "title": f"[{aid}] {data.get('title') or ''}".strip(),
                                "subtitle": str(
                                    data.get("summary")
                                    or data.get("category_label")
                                    or "已完结成果"
                                )[:120],
                                "plugin_id": PLUGIN_ID,
                                "page": "games",
                                "deep_link": {
                                    "tab": "outcomes",
                                    "id": aid,
                                    "umo": str(data.get("umo") or ""),
                                },
                            }
                        )

            if len(items) < cap:
                lower_q = clean.lower()
                for spec in ENGINE_CATALOG:
                    if len(items) >= cap:
                        break
                    hay = " ".join(
                        [
                            str(spec.get("key") or ""),
                            str(spec.get("label") or ""),
                            str(spec.get("desc") or ""),
                            " ".join(spec.get("commands") or []),
                        ]
                    ).lower()
                    if lower_q in hay:
                        enabled = self.module_enabled(spec["key"])
                        items.append(
                            {
                                "kind": "娱乐引擎",
                                "title": str(spec.get("label") or spec.get("key")),
                                "subtitle": f"{'运行中' if enabled else '已停用'} · {spec.get('desc') or ''}"[:120],
                                "plugin_id": PLUGIN_ID,
                                "page": "games",
                                "deep_link": {"tab": "engines", "key": spec["key"]},
                            }
                        )
        except Exception:
            return items[:cap]
        return items[:cap]

    def get_journal_events(
        self,
        persona: str = "",
        scope: str = "",
        date: str = "today",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read-only daily events for Dynamics World's Today's Living Journal feed."""
        _ = persona
        if self._closed or not self._ready or self.store is None:
            return []
        cap = max(1, min(50, int(limit or 20)))
        try:
            tz = self._series_tz()
            since_ts, until_ts = self._date_window(date, tz=tz)
            rows = self.store.recent_journal_rows(
                since_ts=since_ts,
                until_ts=until_ts,
                umo=str(scope or "").strip(),
                limit=cap,
            )
            events: list[dict[str, Any]] = []
            for act in rows:
                aid = str(act.get("id") or "")
                status = str(act.get("status") or "")
                participants = int(act.get("participant_count") or 0)
                contribs = int(act.get("contribution_count") or 0)
                target = int(act.get("target_count") or 1)
                if status == "completed":
                    title = f"「{act.get('title') or aid}」已圆满完结并归档"
                    subtitle = f"{act.get('category_label') or '协作'} · {participants} 位群友参与 · 共 {contribs} 条记录"
                    tone = "accent"
                    route = "games?tab=outcomes&id=" + aid
                elif status == "recruiting":
                    title = f"发起了「{act.get('title') or aid}」正在招募成员"
                    subtitle = f"{act.get('category_label') or '协作'} · 已有 {participants} 位群友报名"
                    tone = "warm"
                    route = "games?tab=activities&id=" + aid
                else:
                    title = f"「{act.get('title') or aid}」进入「{act.get('current_stage') or '进行中'}」"
                    subtitle = f"已有 {participants} 位群友参与 · 进度 {contribs}/{target}"
                    tone = "info"
                    route = "games?tab=activities&id=" + aid
                events.append(
                    {
                        "id": f"act:{aid}",
                        "category": "games",
                        "source_label": "群间游戏",
                        "tone": tone,
                        "occurred_at": self._iso_time(
                            float(act.get("updated_at") or act.get("created_at") or 0),
                            tz=tz,
                        ),
                        "title": title,
                        "subtitle": subtitle,
                        "target_plugin": PLUGIN_ID,
                        "target_route": route,
                        "target_id": aid,
                    }
                )
            return events[:cap]
        except Exception:
            return []

    async def get_series_summary_async(self) -> dict[str, Any]:
        """Offload SQLite summary aggregation to a worker thread for async callers."""
        return await asyncio.to_thread(self.get_series_summary)

    async def get_series_scope_status_async(self, umo: str) -> dict[str, Any]:
        """Offload per-group SQLite scope status to a worker thread."""
        return await asyncio.to_thread(self.get_series_scope_status, umo)

    async def series_search_items_async(
        self, query: str, scope: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        """Offload federated SQLite search to a worker thread."""
        return await asyncio.to_thread(self.series_search_items, query, scope, limit)

    async def get_journal_events_async(
        self,
        persona: str = "",
        scope: str = "",
        date: str = "today",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Offload journal SQLite query to a worker thread."""
        return await asyncio.to_thread(
            self.get_journal_events, persona, scope, date, limit
        )

    # ------------------------------------------------------------------
    # LLM Tools (Group Activity Awareness)
    # ------------------------------------------------------------------
    @filter.llm_tool(name="list_group_activities")
    async def list_group_activities(self, event: AstrMessageEvent) -> str:
        """查询当前群正在进行的协作活动（组织共读、游戏约局、接龙创作、小型挑战、共同制作作品）及近期完结成果。"""
        if not self._ready or self.coordinator is None:
            return json.dumps({"available": False}, ensure_ascii=False)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ctx = await self.coordinator.get_group_activity_context(umo)
        return json.dumps(ctx, ensure_ascii=False)

    @filter.llm_tool(name="get_group_activity_detail")
    async def get_group_activity_detail(
        self, event: AstrMessageEvent, activity_id: str = ""
    ) -> str:
        """查看当前群指定活动（或最新活动）的报名成员、当前阶段与最近打卡/创作进展。

        Args:
            activity_id(string): 可选。活动编号（如 G101），留空则查看当前群最新进行中的活动。
        """
        if not self._ready or self.coordinator is None:
            return "群间游戏尚未初始化。"
        return await handle_detail_command(self.coordinator, event, activity_id)

    @filter.llm_tool(name="get_group_steam_status")
    async def get_group_steam_status(self, event: AstrMessageEvent) -> str:
        """只读查询当前群 Steam 监控群友的实时游戏状态（谁正在玩什么游戏、在线人数等）。"""
        if not self._ready or not self.module_enabled("steam_monitor"):
            return json.dumps({"available": False, "reason": "disabled"}, ensure_ascii=False)
        eng = self.engines.get("steam_monitor")
        if eng is None:
            return json.dumps({"available": False, "reason": "not_loaded"}, ensure_ascii=False)
        gid = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if not gid:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if ":GroupMessage:" in umo:
                gid = umo.rsplit(":", 1)[-1].strip()
        data = await eng.get_group_steam_status_summary(gid)
        return json.dumps(data, ensure_ascii=False)

    @filter.llm_tool(name="get_group_steam_rank")
    async def get_group_steam_rank(
        self, event: AstrMessageEvent, days: int = 1
    ) -> str:
        """只读查询当前群群友最近若干自然日内的 Steam 游戏时长排行榜。

        Args:
            days(number): 可选。统计天数（1=今日，7=近7日，30=近30日，默认 1）。
        """
        if not self._ready or not self.module_enabled("steam_monitor"):
            return json.dumps({"available": False, "reason": "disabled"}, ensure_ascii=False)
        eng = self.engines.get("steam_monitor")
        if eng is None:
            return json.dumps({"available": False, "reason": "not_loaded"}, ensure_ascii=False)
        gid = str(getattr(event, "get_group_id", lambda: "")() or "").strip()
        if not gid:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if ":GroupMessage:" in umo:
                gid = umo.rsplit(":", 1)[-1].strip()
        data = await eng.get_group_steam_rank_summary(gid, days=days)
        return json.dumps(data, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Layer 1: Unified Collaborative Activity Commands (/活动 | /群间游戏)
    # ------------------------------------------------------------------
    @filter.command_group("活动", alias={"群间游戏", "games"})
    def activity_group(self):
        """群间游戏：一起完成一件事（组织共读、游戏约局、接龙创作、小型挑战、共同制作作品）"""
        pass

    def _guard_activity_write(self, event: AstrMessageEvent):
        if not self.activities_enabled() or self.coordinator is None:
            return event.plain_result("群间协作活动功能当前未开启。")
        if self._is_scope_disabled_event(event):
            return event.plain_result("群间游戏已在本群停用，可在群间控制台中重新开启。")
        return None

    @activity_group.command("帮助", alias={"help", "说明"})
    async def act_help(self, event: AstrMessageEvent):
        """查看群间游戏协作指令说明"""
        yield event.plain_result(build_help_text(self.module_states()))

    @activity_group.command("娱乐", alias={"arcade", "modules", "模块", "小游戏"})
    async def act_arcade(self, event: AstrMessageEvent):
        """查看所有已整合娱乐与小游戏模块状态"""
        yield event.plain_result(build_arcade_text(self.module_states()))

    @activity_group.command("发起", alias={"create", "new", "创建", "新建"})
    async def act_create(self, event: AstrMessageEvent, args: str = ""):
        """发起一项群间协作活动"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        raw = self._extract_tail(event, ("发起", "create", "new", "创建", "新建"), args)
        yield event.plain_result(await handle_create_command(self.coordinator, event, raw))

    @activity_group.command("列表", alias={"list", "全部"})
    async def act_list(self, event: AstrMessageEvent):
        """查看本群活动列表"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        yield event.plain_result(await handle_list_command(self.coordinator, event))

    @activity_group.command("详情", alias={"状态", "status", "info", "进度"})
    async def act_detail(self, event: AstrMessageEvent, activity_id: str = ""):
        """查看活动报名名单、阶段与进度详情"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        yield event.plain_result(
            await handle_detail_command(self.coordinator, event, activity_id)
        )

    @activity_group.command("报名", alias={"加入", "join", "signup", "上车"})
    async def act_join(self, event: AstrMessageEvent, args: str = ""):
        """报名参加群间活动"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        raw = self._extract_tail(event, ("报名", "加入", "join", "signup", "上车"), args)
        yield event.plain_result(await handle_join_command(self.coordinator, event, raw))

    @activity_group.command("退出", alias={"leave", "跳车"})
    async def act_leave(self, event: AstrMessageEvent, args: str = ""):
        """退出尚未完结的活动"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("退出", "leave", "跳车"), args)
        yield event.plain_result(await handle_leave_command(self.coordinator, event, raw))

    @activity_group.command("开始", alias={"start", "发车"})
    async def act_start(self, event: AstrMessageEvent, args: str = ""):
        """结束招募进入正式第一阶段"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("开始", "start", "发车"), args)
        yield event.plain_result(
            await handle_stage_command(self.coordinator, event, raw, start_only=True)
        )

    @activity_group.command("阶段", alias={"推进", "下一阶段", "advance", "next", "stage"})
    async def act_stage(self, event: AstrMessageEvent, args: str = ""):
        """推进活动至下一阶段"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(
            event, ("阶段", "推进", "下一阶段", "advance", "next", "stage"), args
        )
        yield event.plain_result(
            await handle_stage_command(self.coordinator, event, raw, start_only=False)
        )

    @activity_group.command("打卡", alias={"checkin", "progress", "记录"})
    async def act_checkin(self, event: AstrMessageEvent, args: str = ""):
        """活动阶段打卡"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        raw = self._extract_tail(event, ("打卡", "checkin", "progress", "记录"), args)
        text, img_path = await handle_submit_with_media(
            self.coordinator, event, raw, kind="checkin"
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @activity_group.command("提交", alias={"续写", "submit", "contribute", "下一棒"})
    async def act_submit(self, event: AstrMessageEvent, args: str = ""):
        """提交接龙段落、共读笔记或共创片段"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        raw = self._extract_tail(
            event, ("提交", "续写", "submit", "contribute", "下一棒"), args
        )
        text, img_path = await handle_submit_with_media(
            self.coordinator, event, raw, kind="submission"
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @activity_group.command("撤回", alias={"withdraw", "undo", "撤销"})
    async def act_withdraw(self, event: AstrMessageEvent, args: str = ""):
        """撤回自己在当前群最近一条打卡/提交记录"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("撤回", "withdraw", "undo", "撤销"), args)
        yield event.plain_result(
            await handle_withdraw_command(self.coordinator, event, raw)
        )

    @activity_group.command("改绑", alias={"rebind", "转移打卡", "换绑"})
    async def act_rebind(self, event: AstrMessageEvent, args: str = ""):
        """将最近一条误投的打卡/提交记录改绑到指定活动编号"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("改绑", "rebind", "转移打卡", "换绑"), args)
        yield event.plain_result(
            await handle_rebind_command(self.coordinator, event, raw)
        )

    @activity_group.command("移交", alias={"转让", "transfer", "换房主"})
    async def act_transfer(self, event: AstrMessageEvent, args: str = ""):
        """发起人或群管理员移交活动主持权"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("移交", "转让", "transfer", "换房主"), args)
        yield event.plain_result(
            await handle_transfer_command(self.coordinator, event, raw)
        )

    @activity_group.command("配图", alias={"画报", "poster", "封面"})
    async def act_poster(self, event: AstrMessageEvent, activity_id: str = ""):
        """为活动生成图文画报（联动视觉工坊，无配图时自动回退 Pillow 画报）"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("配图", "画报", "poster", "封面"), activity_id)
        text, img_path = await handle_poster_command(self.coordinator, event, raw)
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @activity_group.command("完结", alias={"定稿", "结算", "finish", "complete", "done"})
    async def act_complete(self, event: AstrMessageEvent, args: str = ""):
        """完结定稿并同步群间记忆与群史记"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(
            event, ("完结", "定稿", "结算", "finish", "complete", "done"), args
        )
        text, poster_path = await handle_complete_with_media(
            self.coordinator, event, raw
        )
        yield event.plain_result(text)
        if poster_path:
            yield event.image_result(poster_path)

    @activity_group.command("取消", alias={"流局", "cancel", "abort"})
    async def act_cancel(self, event: AstrMessageEvent, args: str = ""):
        """取消活动（流局不入群史记）"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("取消", "流局", "cancel", "abort"), args)
        yield event.plain_result(
            await handle_cancel_command(self.coordinator, event, raw)
        )

    @activity_group.command("清理", alias={"cleanup", "prune"})
    async def act_cleanup(self, event: AstrMessageEvent, args: str = ""):
        """群管理员手动清理超时滞留活动"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("清理", "cleanup", "prune"), args)
        yield event.plain_result(
            await handle_cleanup_command(self.coordinator, event, raw)
        )

    @activity_group.command("成果", alias={"导出", "outcome", "result", "export", "查看成果"})
    async def act_outcome(self, event: AstrMessageEvent, activity_id: str = ""):
        """查看已完结活动的完整汇编成果"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        yield event.plain_result(
            await handle_outcome_command(self.coordinator, event, activity_id)
        )

    # Category quick shortcuts
    @filter.command("共读", alias={"组织共读"})
    async def shortcut_reading(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起或打卡组织共读"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        ev_umo, ev_gid = self._event_umo_and_gid(event)
        raw = self._extract_tail(event, ("组织共读", "共读"), args)
        text, img_path = await handle_category_shortcut_with_media(
            self.coordinator,
            event,
            "reading",
            raw,
            module_states=self.module_states(umo=ev_umo, group_id=ev_gid),
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @filter.command("约局", alias={"游戏约局", "开黑"})
    async def shortcut_matchmaking(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起或查看游戏约局"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        ev_umo, ev_gid = self._event_umo_and_gid(event)
        raw = self._extract_tail(event, ("游戏约局", "约局", "开黑"), args)
        text, img_path = await handle_category_shortcut_with_media(
            self.coordinator,
            event,
            "matchmaking",
            raw,
            module_states=self.module_states(umo=ev_umo, group_id=ev_gid),
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @filter.command("接龙", alias={"接龙创作", "故事接龙"})
    async def shortcut_relay(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起接龙创作或续写下一棒"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        ev_umo, ev_gid = self._event_umo_and_gid(event)
        raw = self._extract_tail(event, ("接龙创作", "故事接龙", "接龙"), args)
        text, img_path = await handle_category_shortcut_with_media(
            self.coordinator,
            event,
            "relay",
            raw,
            module_states=self.module_states(umo=ev_umo, group_id=ev_gid),
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @filter.command("挑战", alias={"小型挑战", "打卡挑战"})
    async def shortcut_challenge(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起小型挑战或提交打卡"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        ev_umo, ev_gid = self._event_umo_and_gid(event)
        raw = self._extract_tail(event, ("小型挑战", "打卡挑战", "挑战"), args)
        text, img_path = await handle_category_shortcut_with_media(
            self.coordinator,
            event,
            "challenge",
            raw,
            module_states=self.module_states(umo=ev_umo, group_id=ev_gid),
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @filter.command("共创", alias={"共同制作", "共同制作作品"})
    async def shortcut_creation(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起共同制作作品、提交共创片段或调用常用协作子指令"""
        blocked = self._guard_activity_write(event)
        if blocked is not None:
            yield blocked
            return
        ev_umo, ev_gid = self._event_umo_and_gid(event)
        raw = self._extract_tail(event, ("共同制作作品", "共同制作", "共创"), args)
        text, img_path = await handle_category_shortcut_with_media(
            self.coordinator,
            event,
            "creation",
            raw,
            module_states=self.module_states(umo=ev_umo, group_id=ev_gid),
        )
        yield event.plain_result(text)
        if img_path:
            yield event.image_result(img_path)

    @staticmethod
    def _extract_tail(
        event: AstrMessageEvent, subcommands: tuple[str, ...], fallback: str = ""
    ) -> str:
        """Preserve multi-word arguments while anchoring strictly to the command prefix."""
        full = str(getattr(event, "message_str", "") or "").strip()
        if not full:
            return fallback

        def _strip_prefix_noise(text: str) -> str:
            head = text.lstrip()
            # Strip optional leading @mention or CQ:at segments before command prefix
            while True:
                m_cq = re.match(r"^\[CQ:at,[^\]]*\]\s*", head)
                if m_cq:
                    head = head[m_cq.end() :].lstrip()
                    continue
                m_at = re.match(r"^@\S+\s+", head)
                if m_at:
                    head = head[m_at.end() :].lstrip()
                    continue
                break
            if head and head[0] in "/!！.。":
                head = head[1:].lstrip()
            return head

        ordered = sorted(
            (str(s) for s in subcommands if str(s)),
            key=len,
            reverse=True,
        )

        def _match_start(text: str) -> str | None:
            lowered = text.lower()
            for sub in ordered:
                sub_lower = sub.lower()
                if not lowered.startswith(sub_lower):
                    continue
                rest = text[len(sub) :]
                # Require boundary for ASCII/alphanumeric commands so e.g. 'set' doesn't match 'settings'
                if sub.isascii() and rest and not rest[0].isspace():
                    continue
                return rest.strip()
            return None

        cleaned = _strip_prefix_noise(full)
        # 1) Direct top-level command prefix match (e.g. /开黑 一起约局)
        matched = _match_start(cleaned)
        if matched is not None:
            return matched or fallback
        # 2) Registered command-group subcommand match (e.g. /活动 new ..., /steam set ...)
        group_split = re.match(r"^(\S+)\s+(.*)$", cleaned, re.S)
        if group_split and group_split.group(1).lower() in {
            "活动",
            "群间游戏",
            "共创",
            "games",
            "steam",
        }:
            matched = _match_start(group_split.group(2).lstrip())
            if matched is not None:
                return matched or fallback
        return fallback

    def _disabled_reply(self, event: AstrMessageEvent, key: str):
        label = MODULE_LABEL_MAP.get(key, key)
        return event.plain_result(f"「{label}」模块当前已在群间游戏控制台关闭。")

    def _guard_engine(
        self, event: AstrMessageEvent, key: str
    ) -> tuple[Any | None, Any | None]:
        """Return ``(engine, None)`` when ready and enabled, else ``(None, optional_reply)``."""
        return self._guard_event(event, key, explicit=True)

    # ------------------------------------------------------------------
    # Engine 1: 真心话 (truth_game)
    # ------------------------------------------------------------------
    @filter.command("真心话", alias=["truth_create", "开真心话"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_create(self, event: AstrMessageEvent):
        """创建真心话房间"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_room_create(event):
            yield r

    @filter.command("报名真心话", alias=["truth_join", "加入真心话"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_join(self, event: AstrMessageEvent):
        """加入真心话房间"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_room_join(event):
            yield r

    @filter.command("开始真心话", alias=["truth_start"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_start(self, event: AstrMessageEvent):
        """开始真心话对局"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_game_start(event):
            yield r

    @filter.command("下一题", alias=["truth_next"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_next(self, event: AstrMessageEvent):
        """推进真心话下一题"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_next_question(event):
            yield r

    @filter.command("跳过", alias=["truth_skip"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_skip(self, event: AstrMessageEvent):
        """跳过当前真心话题目"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_skip_question(event):
            yield r

    @filter.command("退出真心话", alias=["truth_leave"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_leave(self, event: AstrMessageEvent):
        """退出真心话房间"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_room_leave(event):
            yield r

    @filter.command("结束真心话", alias=["truth_end"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_end(self, event: AstrMessageEvent):
        """结束真心话并生成总结"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_room_end(event):
            yield r

    @filter.command("真心话状态", alias=["truth_status"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_status(self, event: AstrMessageEvent):
        """查看真心话房间状态"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_room_status(event):
            yield r

    @filter.command("真心话帮助", alias=["truth_help", "真心话规则"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_help(self, event: AstrMessageEvent):
        """查看真心话帮助"""
        eng, reply = self._guard_engine(event, "truth_game")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_help(event):
            yield r

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_on_group_message(self, event: AstrMessageEvent):
        eng = self._guard_passive_event(event, "truth_game")
        if eng is not None:
            async for r in eng.on_group_message(event):
                yield r

    # ------------------------------------------------------------------
    # Engine 2: 21点 (blackjack21)
    # ------------------------------------------------------------------
    @filter.command("21点帮助", alias={"blackjack_help", "21点规则", "帮助21点"})
    async def bj_help(self, event: AstrMessageEvent):
        """查看21点规则"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_help(event):
            yield r

    @filter.command("创建21点", alias={"blackjack_start", "开21点", "开房间21点"})
    async def bj_open(self, event: AstrMessageEvent):
        """创建21点房间"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_open(event):
            yield r

    @filter.command("加入21点", alias={"blackjack_join", "加入21"})
    async def bj_join(self, event: AstrMessageEvent):
        """加入21点房间"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_join(event):
            yield r

    @filter.command("退出21点", alias={"blackjack_leave", "离开21点"})
    async def bj_leave(self, event: AstrMessageEvent):
        """等待期退出21点房间"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_leave(event):
            yield r

    @filter.command("开始21点", alias={"blackjack_go", "启动21点游戏", "开打21点"})
    async def bj_deal(self, event: AstrMessageEvent):
        """开始21点对局"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_deal(event):
            yield r

    @filter.command("要牌", alias={"blackjack_hit", "21点要牌"})
    async def bj_hit(self, event: AstrMessageEvent):
        """21点要牌"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_hit(event):
            yield r

    @filter.command("停牌", alias={"blackjack_stand", "21点停牌", "不再要了"})
    async def bj_stand(self, event: AstrMessageEvent):
        """21点停牌"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_stand(event):
            yield r

    @filter.command("21点状态", alias={"blackjack_status", "对局状态"})
    async def bj_status(self, event: AstrMessageEvent):
        """查看21点状态"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_status(event):
            yield r

    @filter.command("结束21点", alias={"blackjack_end", "解散21点"})
    async def bj_end(self, event: AstrMessageEvent):
        """结束21点房间"""
        eng, reply = self._guard_engine(event, "blackjack21")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_end(event):
            yield r

    # ------------------------------------------------------------------
    # Engine 3: 今日小猪 (rollpig)
    # ------------------------------------------------------------------
    @filter.command(ROLLPIG_CMD, alias=ROLLPIG_ALIASES)
    async def rp_roll_pig(self, event: AstrMessageEvent):
        """抽取今日小猪"""
        eng, reply = self._guard_engine(event, "rollpig")
        if eng is None:
            if reply is not None:
                yield reply
            return
        await eng.roll_pig(event)

    @filter.regex(ROLLPIG_WAKE_FREE_PATTERN)
    async def rp_roll_pig_wake_free(self, event: AstrMessageEvent):
        eng = self._guard_passive_event(event, "rollpig")
        if eng is not None:
            await eng.roll_pig_wake_free(event)

    # ------------------------------------------------------------------
    # Engine 4: 是啊，吃什么 (what_to_eat)
    # ------------------------------------------------------------------
    @filter.command("口味", alias=["我的口味", "口味档案"])
    async def wte_taste_show(self, event: AstrMessageEvent):
        """查看自己的口味档案"""
        eng, reply = self._guard_engine(event, "what_to_eat")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_taste_show(event):
            yield r

    @filter.command("喜欢吃", alias=["爱吃"])
    async def wte_taste_like(self, event: AstrMessageEvent):
        """记录喜欢的口味/菜"""
        eng, reply = self._guard_engine(event, "what_to_eat")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_taste_like(event):
            yield r

    @filter.command("不吃", alias=["不喜欢", "忌口"])
    async def wte_taste_dislike(self, event: AstrMessageEvent):
        """记录忌口"""
        eng, reply = self._guard_engine(event, "what_to_eat")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_taste_dislike(event):
            yield r

    @filter.command("过敏")
    async def wte_taste_allergy(self, event: AstrMessageEvent):
        """记录过敏原"""
        eng, reply = self._guard_engine(event, "what_to_eat")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_taste_allergy(event):
            yield r

    @filter.command("清空口味", alias=["重置口味"])
    async def wte_taste_clear(self, event: AstrMessageEvent):
        """清空口味档案"""
        eng, reply = self._guard_engine(event, "what_to_eat")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_taste_clear(event):
            yield r

    @filter.command("删除口味", alias=["移除口味"])
    async def wte_taste_delete(self, event: AstrMessageEvent):
        """删除指定口味标签"""
        eng, reply = self._guard_engine(event, "what_to_eat")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_taste_delete(event):
            yield r

    @filter.event_message_type(EventMessageType.ALL)
    async def wte_on_message(self, event: AstrMessageEvent, *args, **kwargs):
        eng = self._guard_passive_event(event, "what_to_eat")
        if eng is not None:
            async for r in eng.on_what_to_eat(event, *args, **kwargs):
                yield r

    # ------------------------------------------------------------------
    # Engine 5: Steam 状态监控 (steam_monitor)
    # ------------------------------------------------------------------
    @filter.command_group("steam")
    def steam_group(self):
        """Steam 状态监控、成就推送、自然日时长排行与商店查价"""
        pass

    @steam_group.command("help", alias={"帮助", "说明"})
    async def sm_help(self, event: AstrMessageEvent):
        """查看 Steam 状态监控指令说明"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_help(event):
            yield r

    @steam_group.command("list", alias={"状态", "列表"})
    async def sm_list(self, event: AstrMessageEvent):
        """查看本群 Steam 监控玩家实时状态"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_list(event):
            yield r

    @steam_group.command("who", alias={"查人", "在干嘛"})
    async def sm_who(self, event: AstrMessageEvent, target: str = ""):
        """查询指定群友当前 Steam 状态"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("who", "查人", "在干嘛"), target)
        async for r in eng.cmd_who(event, raw):
            yield r

    @filter.command("steamwho")
    async def sm_steamwho(self, event: AstrMessageEvent, target: str = ""):
        """即时查询指定群友（或自己）当前 Steam 游戏状态"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("steamwho",), target)
        async for r in eng.cmd_who(event, raw):
            yield r

    @steam_group.command("rank", alias={"排行", "排行榜"})
    async def sm_rank(self, event: AstrMessageEvent, period: str = ""):
        """查看本群 Steam 游玩时长排行榜"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("rank", "排行榜", "排行"), period)
        async for r in eng.cmd_rank(event, raw):
            yield r

    @steam_group.command("price", alias={"查价", "价格"})
    async def sm_price(self, event: AstrMessageEvent, query: str = ""):
        """查询 Steam 游戏价格、史低与跨区比价"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("price", "查价", "价格"), query)
        async for r in eng.cmd_price(event, raw, quick=False):
            yield r

    @steam_group.command("px")
    async def sm_px(self, event: AstrMessageEvent, query: str = ""):
        """快捷查价（直接返回首条匹配）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("px",), query)
        async for r in eng.cmd_px(event, raw):
            yield r

    @steam_group.command("game", alias={"详情"})
    async def sm_game(self, event: AstrMessageEvent, appid: str = ""):
        """查询 Steam 游戏商店详情卡片"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("game", "详情"), appid)
        async for r in eng.cmd_game(event, raw):
            yield r

    @steam_group.command("openbox", alias={"开盒"})
    async def sm_openbox(self, event: AstrMessageEvent, target: str = ""):
        """查看玩家 Steam 公开档案开盒卡片"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("openbox", "开盒"), target)
        async for r in eng.cmd_openbox(event, raw):
            yield r

    @steam_group.command("bind", alias={"绑定"})
    async def sm_bind(
        self, event: AstrMessageEvent, steamid: str = "", nickname: str = ""
    ):
        """自助绑定自己的 SteamID 并加入本群监控"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("bind", "绑定"), steamid)
        parts = raw.split(maxsplit=1) if raw else []
        sid_arg = parts[0] if parts else steamid
        nick_arg = parts[1] if len(parts) > 1 else nickname
        async for r in eng.cmd_bind(event, sid_arg, nick_arg):
            yield r

    @steam_group.command("unbind", alias={"解绑"})
    async def sm_unbind(self, event: AstrMessageEvent):
        """解除自己的 QQ 与 SteamID 绑定"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_unbind(event):
            yield r

    @steam_group.command("addid", alias={"添加"})
    async def sm_addid(self, event: AstrMessageEvent, args: str = ""):
        """添加 SteamID 到本群监控（普通成员仅限添加自己，管理员可批量/代绑他人）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("addid", "添加"), args)
        async for r in eng.cmd_addid(event, raw):
            yield r

    @steam_group.command("delid", alias={"删除"})
    async def sm_delid(
        self, event: AstrMessageEvent, steamid: str = "", group_id: str = ""
    ):
        """从监控列表移除 SteamID（普通成员仅限移除自己绑定的 ID）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("delid", "删除"), steamid)
        parts = raw.split(maxsplit=1) if raw else []
        sid_arg = parts[0] if parts else steamid
        gid_arg = parts[1] if len(parts) > 1 else group_id
        async for r in eng.cmd_delid(event, sid_arg, gid_arg):
            yield r

    @steam_group.command("on", alias={"开启"})
    async def sm_on(self, event: AstrMessageEvent):
        """开启本群 Steam 状态监控推送（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_on(event):
            yield r

    @steam_group.command("off", alias={"关闭"})
    async def sm_off(self, event: AstrMessageEvent):
        """关闭本群 Steam 状态监控推送（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_off(event):
            yield r

    @steam_group.command("achievement_on")
    async def sm_achievement_on(self, event: AstrMessageEvent):
        """开启本群 Steam 成就推送（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_achievement_on(event):
            yield r

    @steam_group.command("achievement_off")
    async def sm_achievement_off(self, event: AstrMessageEvent):
        """关闭本群 Steam 成就推送（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_achievement_off(event):
            yield r

    @steam_group.command("rank_on")
    async def sm_rank_on(self, event: AstrMessageEvent, param: str = ""):
        """管理每日定时排行推送（管理员）：all|list|test|del"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("rank_on",), param)
        async for r in eng.cmd_rank_on(event, raw):
            yield r

    @steam_group.command("push_group")
    async def sm_push_group(self, event: AstrMessageEvent, steamid: str = ""):
        """添加本群到指定 SteamID 的副群联动推送路由（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("push_group",), steamid)
        async for r in eng.cmd_push_group(event, raw):
            yield r

    @steam_group.command("delpush_group")
    async def sm_delpush_group(
        self, event: AstrMessageEvent, steamid: str = "", target_group: str = ""
    ):
        """移除副群联动推送路由（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("delpush_group",), steamid)
        parts = raw.split(maxsplit=1) if raw else []
        sid_arg = parts[0] if parts else steamid
        gid_arg = parts[1] if len(parts) > 1 else target_group
        async for r in eng.cmd_delpush_group(event, sid_arg, gid_arg):
            yield r

    @steam_group.command("clear_groupids")
    async def sm_clear_groupids(self, event: AstrMessageEvent, group_id: str = ""):
        """清空指定群（默认本群）的监控列表（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("clear_groupids",), group_id)
        async for r in eng.cmd_clear_groupids(event, raw):
            yield r

    @steam_group.command("clear_allids")
    async def sm_clear_allids(self, event: AstrMessageEvent):
        """清空所有群聊的监控列表与活跃状态（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_clear_allids(event):
            yield r

    @steam_group.command("alllist")
    async def sm_alllist(self, event: AstrMessageEvent, mode: str = "img"):
        """查看全群监控玩家状态（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("alllist",), mode)
        async for r in eng.cmd_alllist(event, raw or "img"):
            yield r

    @steam_group.command("allrank")
    async def sm_allrank(self, event: AstrMessageEvent, period: str = ""):
        """查看全群 Steam 游玩时长排行榜（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("allrank",), period)
        async for r in eng.cmd_allrank(event, raw):
            yield r

    @steam_group.command("config")
    async def sm_config(self, event: AstrMessageEvent):
        """查看 Steam 监控当前配置（管理员，敏感信息自动脱敏）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_config(event):
            yield r

    @steam_group.command("set")
    async def sm_set(
        self, event: AstrMessageEvent, key: str = "", value: str = ""
    ):
        """热更新 Steam 监控配置参数（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        raw = self._extract_tail(event, ("set",), "")
        parts = raw.split(maxsplit=1) if raw else []
        k_arg = parts[0] if parts else key
        v_arg = parts[1] if len(parts) > 1 else value
        async for r in eng.cmd_set(event, k_arg, v_arg):
            yield r

    @steam_group.command("rs")
    async def sm_rs(self, event: AstrMessageEvent):
        """重置 Steam 监控运行状态并重新静默同步基线（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_rs(event):
            yield r

    @steam_group.command("clear_cache", alias={"清除缓存"})
    async def sm_clear_cache(self, event: AstrMessageEvent):
        """清理 Steam 头像、封面与游戏名缓存（管理员）"""
        eng, reply = self._guard_engine(event, "steam_monitor")
        if eng is None:
            if reply is not None:
                yield reply
            return
        async for r in eng.cmd_clear_cache(event):
            yield r

    @filter.event_message_type(EventMessageType.ALL)
    async def sm_on_message(self, event: AstrMessageEvent):
        """接收 Steam 商店查价的候选序号回复"""
        eng = self._guard_passive_event(event, "steam_monitor")
        if eng is not None:
            async for r in eng.on_message(event):
                yield r
