"""Steam 状态监控引擎 (SteamMonitorEngine) — 生命周期、调度编排与指令处理。

注意：本模块位于 engines/ 下，严格禁止使用 @register 或 @filter 装饰器；
所有指令路由与 LLM Tool 均由宿主插件 main.py 统一注册并转发至此引擎。
"""

from __future__ import annotations

import asyncio
import os
import re
import ssl
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

from astrbot.api import logger

from ...runtime import PLUGIN_ID, Component
try:
    from ... import series_panels
except Exception:  # pragma: no cover - standalone fallback
    series_panels = None  # type: ignore[assignment]
from .achievements import AchievementService
from .migration import migrate_from_legacy_plugin
from .price_service import PriceService
from .renderer import SteamCardRenderer
from .state_machine import (
    ActiveSession,
    SessionEvent,
    aggregate_playtime_window,
    apply_poll_observation,
    end_game_tip,
    format_duration,
    natural_day_bounds,
    should_skip_game,
)
from .steam_client import (
    ALLOWED_CA_FILE_EXTENSIONS,
    SteamClient,
    validate_api_base_url,
)
from .store import SteamMonitorStore

try:
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import At, Image, Plain
except ImportError:  # pragma: no cover - standalone test fallback
    MessageChain = None  # type: ignore[assignment]
    At = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    Plain = None  # type: ignore[assignment]

try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - standalone test fallback
    StarTools = None  # type: ignore[assignment]


DEFAULT_STEAM_MONITOR_CONFIG: dict[str, Any] = {
    "steam_api_key": "",
    "sgdb_api_key": "",
    "itad_api_key": "",
    "poll_interval_seconds": 60,
    "exit_debounce_seconds": 180,
    "retry_times": 3,
    "max_group_size": 30,
    "enable_achievement_poll": True,
    "achievement_poll_interval_seconds": 180,
    "enable_game_start_notify": True,
    "enable_game_end_notify": True,
    "enable_network_fluctuation_notify": False,
    "notify_send_image": True,
    "notify_send_text": True,
    "game_filter_mode": "全部游戏",
    "game_filter_ids": "",
    "rank_push_hour": 8,
    "rank_push_minute": 30,
    "price_currency": "CNY",
    "price_region": "CN",
    "price_compare_regions": "UA",
    "enable_proxy": False,
    "proxy_url": "",
    "steam_api_base": "",
    "steam_store_base": "",
    "sgdb_api_base": "",
    "itad_api_base": "",
    "ssl_ca_file": "",
    "cache_avatar_hours": 24,
    "cache_cover_hours": 168,
    "detailed_poll_log": False,
}

MAX_SEND_QUEUE_SIZE = 256
MAX_GROUP_LAST_SENT = 512
DEFAULT_TRANSLATE_TIMEOUT_SECONDS = 10.0

SECRET_CONFIG_KEYS: frozenset[str] = frozenset(
    {"steam_api_key", "sgdb_api_key", "itad_api_key"}
)
API_BASE_CONFIG_KEYS: frozenset[str] = frozenset(
    {"steam_api_base", "steam_store_base", "sgdb_api_base", "itad_api_base"}
)
SENSITIVE_CONFIG_KEYS: frozenset[str] = (
    SECRET_CONFIG_KEYS | API_BASE_CONFIG_KEYS | frozenset({"ssl_ca_file", "proxy_url"})
)

VALID_FILTER_MODES: frozenset[str] = frozenset({"全部游戏", "白名单", "黑名单"})

_AT_TEXT_PATTERN = re.compile(
    r"\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@[^\s()]*\((\d+)\)|@(\d{5,15})"
)
_SELECTION_INDEX_PATTERN = re.compile(r"^\s*([1-9]\d?)\s*$")
_THINK_CLOSE_PATTERN = re.compile(r"</(?:thinking|think)[^>]*>", flags=re.IGNORECASE)
_THINK_TAG_PATTERN = re.compile(r"</?(?:thinking|think)[^>]*>", flags=re.IGNORECASE)
_TRANSLATE_PREFIX_PATTERN = re.compile(
    r"^(?:英文名|翻译结果|Translation)\s*[:：]?\s*", flags=re.IGNORECASE
)

HELP_TEXT = """🎮 Steam 状态监控指令一览

【全员可用】
• /steam list — 查看本群监控玩家实时状态
• /steamwho [@用户] / 在干嘛 [@用户] — 即时查询绑定玩家状态（不填查自己）
• /steam rank [天数|week|month|yesterday] — 查看本群游玩时长排行（默认今日）
• /steam price <游戏名/链接> — 查询价格、史低与跨区比价（多匹配可回复序号）
• /steam px <游戏名/链接> — 查价快捷版，直接返回首条匹配
• /steam game <AppID/链接> — 查看 Steam 游戏商店详情卡
• /steam openbox [SteamID/链接/好友码/@用户] — 玩家 Steam 档案开盒
• /steam bind <SteamID/链接/好友码> [昵称] — 绑定自己的 Steam 并加入本群监控
• /steam unbind — 解除自己的 QQ 绑定
• /steam addid <SteamID/链接/好友码> [昵称] — 自助将自己的 Steam 加入本群
• /steam delid [SteamID] — 仅可移除自己绑定的 SteamID
• /steam help — 查看本帮助

【管理员专用】
• /steam on | /steam off — 开启/关闭本群状态监控推送
• /steam achievement_on | /steam achievement_off — 开启/关闭本群成就推送
• /steam rank_on [all|list|test|del] — 管理每日定时昨日排行推送
• /steam push_group <SteamID> | /steam delpush_group <SteamID> [群号] — 跨群副群推送路由
• /steam addid <ID1,ID2> [@用户] [昵称] | /steam delid <ID> [群号] — 代管群成员监控
• /steam clear_groupids [群号] | /steam clear_allids — 清空单群/全部监控列表
• /steam alllist [img|text] | /steam allrank [天数] — 全局玩家状态与全局时长排行
• /steam config | /steam set <参数> <值> — 查看或热更新配置
• /steam rs | /steam clear_cache — 重置运行状态 / 清理头像封面与游戏名缓存"""


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in ("1", "true", "yes", "y", "on", "开", "开启", "是"):
        return True
    if text in ("0", "false", "no", "n", "off", "关", "关闭", "否"):
        return False
    raise ValueError(f"无法解析布尔值: {raw!r}（请使用 true/false）")


def _mask_secret(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "(未配置)"
    if len(text) <= 6:
        return "******"
    return f"{text[:3]}******{text[-3:]}"


def _build_image_component(image_ref: str) -> Any:
    if Image is None:
        return None
    ref = str(image_ref or "").strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://")):
        return Image.fromURL(ref)
    if ref.startswith("base64://"):
        return Image.fromBase64(ref[len("base64://") :])
    return Image.fromFileSystem(ref)


def parse_rank_period(
    period: str, *, now_dt: datetime | None = None
) -> tuple[str, str, str, str]:
    """Parse rank period string into (start_day, end_day, period_label, date_label)."""
    now_local = now_dt if now_dt is not None else datetime.now().astimezone()
    today = now_local.date()
    raw = str(period or "").strip().lower()

    if raw in ("", "today", "今日", "今天", "1", "1d"):
        day_s = today.strftime("%Y-%m-%d")
        return day_s, day_s, "今日", today.strftime("%Y/%m/%d")

    if raw in ("yesterday", "昨日", "昨天", "yd"):
        yday = today - timedelta(days=1)
        day_s = yday.strftime("%Y-%m-%d")
        return day_s, day_s, "昨日", yday.strftime("%Y/%m/%d")

    if raw in ("week", "weekly", "周", "本周", "近7天", "7d", "7"):
        days = 7
        label = "近7日"
    elif raw in ("month", "monthly", "月", "本月", "近30天", "30d", "30"):
        days = 30
        label = "近30日"
    else:
        digits = raw.rstrip("d天日")
        if digits.isdigit():
            days = max(1, min(365, int(digits)))
            label = "今日" if days == 1 else f"近{days}日"
        else:
            day_s = today.strftime("%Y-%m-%d")
            return day_s, day_s, "今日", today.strftime("%Y/%m/%d")

    start_date = today - timedelta(days=days - 1)
    start_s = start_date.strftime("%Y-%m-%d")
    end_s = today.strftime("%Y-%m-%d")
    if start_s == end_s:
        date_label = today.strftime("%Y/%m/%d")
    else:
        date_label = f"{start_date.strftime('%Y/%m/%d')} ~ {today.strftime('%Y/%m/%d')}"
    return start_s, end_s, label, date_label


class SteamMonitorEngine(Component):
    """Built-in Steam status monitoring, achievement tracking, playtime ranking, and store query engine."""

    def __init__(
        self,
        context: Any,
        config: dict[str, Any] | None = None,
        *,
        data_dir: Path | None = None,
        start_background_tasks: bool = True,
        html_render_fn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(context)
        self.logger = getattr(self, "logger", None) or logger
        self.config: dict[str, Any] = config if config is not None else {}
        self._data_dir_override = Path(data_dir) if data_dir is not None else None
        self._start_background_tasks = bool(start_background_tasks)
        self._html_render_override = html_render_fn

        self.data_dir: Path | None = None
        self.store: SteamMonitorStore | None = None
        self.steam_client: SteamClient | None = None
        self.achievement_service: AchievementService | None = None
        self.price_service: PriceService | None = None
        self.renderer: SteamCardRenderer | None = None
        self._migration_summary: dict[str, Any] | None = None

        self._poll_task: asyncio.Task[Any] | None = None
        self._daily_push_task: asyncio.Task[Any] | None = None
        self._send_worker_task: asyncio.Task[Any] | None = None
        self._send_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._group_last_sent: dict[str, float] = {}
        self._min_group_send_interval: float = 1.2
        self._temp_card_refs: dict[str, int] = {}

        self._baseline_synced: bool = False
        self._last_poll_ts: float = 0.0
        self._initialized: bool = False
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Config & Host Helpers
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        if key in self.config and self.config[key] is not None:
            return self.config[key]
        if default is not None:
            return default
        return DEFAULT_STEAM_MONITOR_CONFIG.get(key)

    def _is_module_enabled(self) -> bool:
        owner = self.owner
        if owner is not None and callable(getattr(owner, "module_enabled", None)):
            try:
                return bool(owner.module_enabled("steam_monitor"))
            except Exception:
                return True
        return True

    def _series_tz(self) -> Any:
        resolver = (
            getattr(series_panels, "resolve_series_timezone", None)
            if series_panels is not None
            else None
        )
        if callable(resolver):
            try:
                tz = resolver(getattr(self, "context", None))
                if tz is not None:
                    return tz
            except Exception:
                pass
        return datetime.now().astimezone().tzinfo

    def _can_proactive_broadcast(self, *, umo: str = "", group_id: str = "") -> bool:
        """Enforce owner proactive broadcast gate (readiness, exclude_groups, quiet_hours)."""
        if self._closed:
            return False
        owner = self.owner
        gate = (
            getattr(owner, "_can_proactive_broadcast", None)
            if owner is not None
            else None
        )
        if callable(gate):
            try:
                return bool(gate(umo=umo, group_id=group_id))
            except Exception:
                return False
        if series_panels is not None:
            ctx = getattr(self, "context", None)
            if umo or group_id:
                excl_fn = getattr(series_panels, "is_series_group_excluded", None)
                if callable(excl_fn):
                    try:
                        if excl_fn(ctx, umo=umo, group_id=group_id):
                            return False
                    except Exception:
                        pass
            quiet_fn = getattr(series_panels, "is_series_in_quiet_hours", None)
            if callable(quiet_fn):
                try:
                    if quiet_fn(ctx):
                        return False
                except Exception:
                    pass
        return True

    def _acquire_temp_card(self, image_ref: str | None) -> None:
        if not image_ref or not SteamCardRenderer.is_temp_card_path(image_ref):
            return
        key = str(image_ref)
        self._temp_card_refs[key] = self._temp_card_refs.get(key, 0) + 1

    async def _release_temp_card(self, image_ref: str | None) -> None:
        if not image_ref or not SteamCardRenderer.is_temp_card_path(image_ref):
            return
        key = str(image_ref)
        remaining = self._temp_card_refs.get(key, 0) - 1
        if remaining > 0:
            self._temp_card_refs[key] = remaining
            return
        self._temp_card_refs.pop(key, None)
        await asyncio.to_thread(SteamCardRenderer.cleanup_temp_file, key)

    async def _cleanup_temp_card_if_unused(self, image_ref: str | None) -> None:
        if not image_ref or not SteamCardRenderer.is_temp_card_path(image_ref):
            return
        key = str(image_ref)
        if self._temp_card_refs.get(key, 0) > 0:
            return
        await asyncio.to_thread(SteamCardRenderer.cleanup_temp_file, key)

    def _resolve_html_render_fn(self) -> Callable[..., Any] | None:
        if self._html_render_override is not None:
            return self._html_render_override
        if self.owner is not None and callable(getattr(self.owner, "html_render", None)):
            return self.owner.html_render
        fn = getattr(self, "html_render", None)
        return fn if callable(fn) else None

    @staticmethod
    def _group_id(event: Any) -> str:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            gid = str(getter() or "").strip()
            if gid and gid.lower() != "none":
                return gid
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if ":GroupMessage:" in umo:
            return umo.rsplit(":", 1)[-1].strip()
        return ""

    @staticmethod
    def _sender_id(event: Any) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            return str(getter() or "").strip()
        return ""

    @staticmethod
    def _session_umo(event: Any) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            return umo
        getter = getattr(event, "get_session_id", None)
        if callable(getter):
            return str(getter() or "").strip()
        return ""

    @staticmethod
    def _search_session_key(event: Any) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            return umo
        getter = getattr(event, "get_session_id", None)
        if callable(getter):
            sid = str(getter() or "").strip()
            if sid:
                return sid
        gid = SteamMonitorEngine._group_id(event)
        return f"group:{gid}" if gid else "default"

    @staticmethod
    def _is_admin(event: Any) -> bool:
        fn = getattr(event, "is_admin", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return bool(fn)

    @staticmethod
    def _event_text(event: Any) -> str:
        raw = getattr(event, "message_str", None)
        if raw is None:
            getter = getattr(event, "get_message_str", None)
            raw = getter() if callable(getter) else ""
        return str(raw or "").strip()

    @staticmethod
    def _extract_at_qqs(event: Any, text: str = "") -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        bot_id = ""
        self_getter = getattr(event, "get_self_id", None)
        if callable(self_getter):
            try:
                bot_id = str(self_getter() or "").strip()
            except Exception:
                bot_id = ""

        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None)
        if isinstance(chain, (list, tuple)):
            for seg in chain:
                is_at = (At is not None and isinstance(seg, At)) or (
                    seg.__class__.__name__.lower() == "at"
                )
                if is_at:
                    qq = str(getattr(seg, "qq", "") or "").strip()
                    if qq and qq.isdigit() and qq != bot_id and qq not in seen:
                        seen.add(qq)
                        found.append(qq)

        for match in _AT_TEXT_PATTERN.finditer(text or ""):
            qq = match.group(1) or match.group(2) or match.group(3) or match.group(4)
            if qq and qq != bot_id and qq not in seen:
                seen.add(qq)
                found.append(qq)

        return found

    def _cfg_int(self, key: str, default: int) -> int:
        raw = self._cfg(key, default)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    async def _record_group_session(self, event: Any, group_id: str | None = None) -> None:
        if self.store is None:
            return
        gid = group_id or self._group_id(event)
        umo = self._session_umo(event)
        if gid and umo and ":FriendMessage:" not in umo and ":PrivateMessage:" not in umo:
            await asyncio.to_thread(self.store.set_notify_session, gid, umo)

    def _make_reply(
        self,
        event: Any,
        image_ref: str | None,
        fallback_text: str,
        *,
        caption: str = "",
        prefer_image: bool = True,
    ) -> Any:
        if prefer_image and image_ref:
            if caption and hasattr(event, "chain_result") and Image is not None and Plain is not None:
                img_comp = _build_image_component(image_ref)
                if img_comp is not None:
                    return event.chain_result([img_comp, Plain(caption)])
            if image_ref.startswith(("http://", "https://")):
                if hasattr(event, "chain_result") and Image is not None:
                    return event.chain_result([Image.fromURL(image_ref)])
            if hasattr(event, "image_result"):
                return event.image_result(image_ref)
        return event.plain_result(fallback_text)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        if self._initialized and not self._closed:
            return
        self._closed = False

        if self._data_dir_override is not None:
            data_dir = self._data_dir_override
        elif StarTools is not None:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_ID))
        else:
            data_dir = Path.cwd() / "data" / "plugin_data" / PLUGIN_ID
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir

        db_path = data_dir / "steam_monitor.db"
        self.store = await asyncio.to_thread(SteamMonitorStore, db_path)

        try:
            self._migration_summary = await asyncio.to_thread(
                migrate_from_legacy_plugin, self.store, self.config
            )
            if self._migration_summary and self._migration_summary.get("migrated"):
                self.logger.info(
                    "[SteamMonitor] 已完成旧版 steam_status_monitor_V3 数据迁移: %s",
                    self._migration_summary,
                )
        except Exception:
            self.logger.exception("[SteamMonitor] 旧版数据迁移异常（已安全跳过，不影响启动）")

        last_poll_raw = await asyncio.to_thread(self.store.get_kv, "last_poll_at", "")
        try:
            self._last_poll_ts = float(last_poll_raw) if last_poll_raw else 0.0
        except ValueError:
            self._last_poll_ts = 0.0

        self.steam_client = SteamClient(
            config_getter=lambda: self.config,
            data_dir=data_dir / "steam_monitor_cache",
            store=self.store,
        )
        self.achievement_service = AchievementService(
            self.steam_client,
            config_getter=lambda: self.config,
            on_unlocked=self._on_achievements_unlocked,
        )
        self.price_service = PriceService(
            self.steam_client,
            config_getter=lambda: self.config,
            llm_translate_fn=self._translate_game_query,
        )
        self.renderer = SteamCardRenderer(html_render_fn=self._resolve_html_render_fn())
        await asyncio.to_thread(self.renderer.cleanup_stale_temp_files)
        self._send_queue = asyncio.Queue(maxsize=MAX_SEND_QUEUE_SIZE)

        if self._start_background_tasks:
            self._send_worker_task = asyncio.create_task(
                self._notification_send_loop(), name="steam_monitor_send_queue"
            )
            self._poll_task = asyncio.create_task(
                self._polling_loop(), name="steam_monitor_poll_loop"
            )
            self._daily_push_task = asyncio.create_task(
                self._daily_rank_push_loop(), name="steam_monitor_daily_rank_loop"
            )

        self._initialized = True
        self.logger.info("[SteamMonitor] Steam 状态监控引擎初始化完成 (db=%s)", db_path)

    async def _reset_achievement_service(self) -> None:
        if self.achievement_service is not None:
            await self.achievement_service.aclose()
        if self.steam_client is not None:
            self.achievement_service = AchievementService(
                self.steam_client,
                config_getter=lambda: self.config,
                on_unlocked=self._on_achievements_unlocked,
            )

    async def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._initialized = False

        tasks: list[asyncio.Task[Any]] = []
        for task in (self._poll_task, self._daily_push_task, self._send_worker_task):
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        self._poll_task = None
        self._daily_push_task = None
        self._send_worker_task = None

        if self.achievement_service is not None:
            try:
                await self.achievement_service.aclose()
            except Exception:
                self.logger.exception("[SteamMonitor] 关闭成就服务异常")

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self.steam_client is not None:
            try:
                await self.steam_client.aclose()
            except Exception:
                self.logger.exception("[SteamMonitor] 关闭 Steam HTTP 客户端池异常")

        if self._temp_card_refs:
            leftover = list(self._temp_card_refs.keys())
            self._temp_card_refs.clear()
            for path in leftover:
                await asyncio.to_thread(SteamCardRenderer.cleanup_temp_file, path)

        self._group_last_sent.clear()
        self.logger.info("[SteamMonitor] Steam 状态监控引擎已停止")

    # ------------------------------------------------------------------
    # Runtime Summary (for Web Console)
    # ------------------------------------------------------------------
    def get_runtime_summary(self) -> dict[str, Any]:
        if self.store is None:
            return {
                "monitored_groups": 0,
                "enabled_groups": 0,
                "monitored_players": 0,
                "playing_count": 0,
                "confirming_exit_count": 0,
                "last_poll_at": 0.0,
                "last_poll_label": "尚未轮询",
                "last_rank_push_date": "",
                "achievement_tracking_count": 0,
                "has_api_key": bool(str(self._cfg("steam_api_key", "") or "").strip()),
            }
        stats = self.store.stats_summary()
        last_poll_at = max(self._last_poll_ts, float(stats.get("last_poll_at") or 0.0))
        if last_poll_at > 0:
            last_poll_label = datetime.fromtimestamp(
                last_poll_at, tz=self._series_tz()
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_poll_label = "尚未轮询"
        return {
            "monitored_groups": int(stats.get("monitored_groups") or 0),
            "enabled_groups": int(stats.get("enabled_groups") or 0),
            "monitored_players": int(stats.get("monitored_players") or 0),
            "playing_count": int(stats.get("active_playing") or 0),
            "confirming_exit_count": int(stats.get("active_confirming_exit") or 0),
            "last_poll_at": last_poll_at,
            "last_poll_label": last_poll_label,
            "last_rank_push_date": str(stats.get("last_rank_push_date") or ""),
            "achievement_tracking_count": (
                self.achievement_service.active_count() if self.achievement_service else 0
            ),
            "has_api_key": bool(str(self._cfg("steam_api_key", "") or "").strip()),
        }

    # ------------------------------------------------------------------
    # Monotonic Polling Loop & Global Batch Poll
    # ------------------------------------------------------------------
    async def _polling_loop(self) -> None:
        next_tick = time.monotonic()
        while not self._closed:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("[SteamMonitor] 轮询周期执行异常")

            interval = max(30, int(self._cfg("poll_interval_seconds", 60) or 60))
            next_tick += interval
            delay = next_tick - time.monotonic()
            if delay <= 0:
                next_tick = time.monotonic()
                delay = 1.0
            await asyncio.sleep(delay)

    async def poll_once(
        self, *, now: float | None = None, silent: bool | None = None
    ) -> dict[str, Any]:
        """Run one global batch poll cycle across all enabled groups."""
        if self.store is None or self.steam_client is None:
            return {"skipped": "not_initialized", "polled": 0, "events": 0}
        if not self._is_module_enabled():
            return {"skipped": "module_disabled", "polled": 0, "events": 0}
        if not str(self._cfg("steam_api_key", "") or "").strip():
            return {"skipped": "no_api_key", "polled": 0, "events": 0}

        now_ts = float(now if now is not None else time.time())
        is_silent = (not self._baseline_synced) if silent is None else bool(silent)

        sids = await asyncio.to_thread(
            self.store.list_all_monitored_steam_ids, only_enabled_groups=True
        )
        if not sids:
            if silent is None:
                self._baseline_synced = True
            self._last_poll_ts = now_ts
            await asyncio.to_thread(self.store.set_kv, "last_poll_at", f"{now_ts:.3f}")
            return {"polled": 0, "failed": 0, "events": 0, "silent": is_silent}

        statuses, failed_sids = await self.steam_client.fetch_player_statuses_batch(sids)
        active_map = await asyncio.to_thread(self.store.load_active_sessions)
        bindings_map = await asyncio.to_thread(self.store.get_bindings_map, sids)

        debounce_seconds = max(30, int(self._cfg("exit_debounce_seconds", 180) or 180))
        filter_mode = str(self._cfg("game_filter_mode", "全部游戏") or "全部游戏")
        filter_ids = str(self._cfg("game_filter_ids", "") or "")
        ach_global = bool(self._cfg("enable_achievement_poll", True))
        detailed_log = bool(self._cfg("detailed_poll_log", False))

        if is_silent:
            await self._apply_silent_baseline(
                sids=sids,
                statuses=statuses,
                failed_sids=failed_sids,
                active_map=active_map,
                bindings_map=bindings_map,
                now_ts=now_ts,
                filter_mode=filter_mode,
                filter_ids=filter_ids,
                ach_global=ach_global,
            )
            self._baseline_synced = True
            self._last_poll_ts = now_ts
            if detailed_log:
                self.logger.info(
                    "[SteamMonitor] 首轮静默基线同步完成: polled=%d failed=%d",
                    len(sids),
                    len(failed_sids),
                )
            return {
                "polled": len(sids),
                "failed": len(failed_sids),
                "events": 0,
                "silent": True,
            }

        emitted_events: list[SessionEvent] = []
        profile_updates: list[tuple[str, str, str]] = []
        upsert_sessions: list[ActiveSession] = []
        delete_session_sids: list[str] = []
        play_sessions: list[tuple[str, str, str, float, float, float, str]] = []

        for sid in sids:
            existing = active_map.get(sid)
            binding = bindings_map.get(sid)
            api_ok = sid not in failed_sids
            if api_ok:
                st = statuses.get(sid) or {}
                persona = str(st.get("name") or "").strip()
                avatar = str(st.get("avatarfull") or st.get("avatar") or "").strip()
                if persona or avatar:
                    profile_updates.append((sid, persona, avatar))
                display_name = SteamMonitorStore.resolve_display_name_from_binding(
                    binding, sid, persona
                )
                obs_gid = str(st.get("gameid") or "").strip() or None
                obs_gname = str(st.get("gameextrainfo") or "").strip()
                if obs_gid:
                    zh_name, _ = await self.steam_client.get_game_names(obs_gid, obs_gname)
                    if zh_name:
                        obs_gname = zh_name
            else:
                display_name = (
                    existing.player_name
                    if existing and existing.player_name
                    else SteamMonitorStore.resolve_display_name_from_binding(binding, sid, sid)
                )
                avatar = existing.avatar_url if existing else ""
                obs_gid = None
                obs_gname = ""

            next_session, sid_events = apply_poll_observation(
                existing,
                obs_gid,
                now=now_ts,
                steam_id=sid,
                game_name=obs_gname,
                player_name=display_name,
                avatar_url=avatar,
                api_ok=api_ok,
                debounce_seconds=debounce_seconds,
            )

            if next_session is not None:
                upsert_sessions.append(next_session)
            elif existing is not None:
                delete_session_sids.append(sid)

            for ev in sid_events:
                if ev.kind == "closed" and ev.ended_at is not None and ev.duration_sec > 0:
                    play_sessions.append(
                        (
                            ev.steam_id,
                            ev.game_id,
                            ev.game_name,
                            ev.started_at,
                            ev.ended_at,
                            ev.duration_sec,
                            "live",
                        )
                    )
                emitted_events.append(ev)

        await asyncio.to_thread(
            self.store.commit_poll_batch,
            profile_updates=profile_updates,
            upsert_sessions=upsert_sessions,
            delete_session_sids=delete_session_sids,
            play_sessions=play_sessions,
            last_poll_at=now_ts,
        )
        self._last_poll_ts = now_ts

        for ev in emitted_events:
            await self._handle_session_event(
                ev,
                filter_mode=filter_mode,
                filter_ids=filter_ids,
                ach_global=ach_global,
            )

        if detailed_log:
            self.logger.info(
                "[SteamMonitor] 轮询完成: polled=%d failed=%d events=%d",
                len(sids),
                len(failed_sids),
                len(emitted_events),
            )
        return {
            "polled": len(sids),
            "failed": len(failed_sids),
            "events": len(emitted_events),
            "silent": False,
        }

    async def _apply_silent_baseline(
        self,
        *,
        sids: list[str],
        statuses: dict[str, dict[str, Any]],
        failed_sids: set[str],
        active_map: dict[str, ActiveSession],
        bindings_map: dict[str, dict[str, Any]] | None = None,
        now_ts: float,
        filter_mode: str,
        filter_ids: str,
        ach_global: bool,
    ) -> None:
        assert self.store is not None and self.steam_client is not None
        if bindings_map is None:
            bindings_map = await asyncio.to_thread(self.store.get_bindings_map, sids)
        sid_set = set(sids)
        profile_updates: list[tuple[str, str, str]] = []
        upsert_sessions: list[ActiveSession] = []
        delete_session_sids: list[str] = [
            stale_sid for stale_sid in list(active_map.keys()) if stale_sid not in sid_set
        ]
        ach_candidates: list[tuple[str, str, str, str, str]] = []

        for sid in sids:
            if sid in failed_sids:
                continue
            st = statuses.get(sid) or {}
            persona = str(st.get("name") or "").strip()
            avatar = str(st.get("avatarfull") or st.get("avatar") or "").strip()
            if persona or avatar:
                profile_updates.append((sid, persona, avatar))
            display_name = SteamMonitorStore.resolve_display_name_from_binding(
                bindings_map.get(sid), sid, persona
            )
            obs_gid = str(st.get("gameid") or "").strip() or None
            obs_gname = str(st.get("gameextrainfo") or "").strip()
            existing = active_map.get(sid)

            if obs_gid:
                zh_name, _ = await self.steam_client.get_game_names(obs_gid, obs_gname)
                gname = zh_name or obs_gname or f"App {obs_gid}"
                if existing is not None and existing.game_id == obs_gid:
                    refreshed = ActiveSession(
                        steam_id=sid,
                        game_id=obs_gid,
                        game_name=gname,
                        started_at=existing.started_at,
                        state="playing",
                        exited_at=None,
                        exit_deadline=None,
                        last_seen_at=now_ts,
                        consecutive_failures=0,
                        player_name=display_name,
                        avatar_url=avatar or existing.avatar_url,
                    )
                else:
                    refreshed = ActiveSession(
                        steam_id=sid,
                        game_id=obs_gid,
                        game_name=gname,
                        started_at=now_ts,
                        state="playing",
                        exited_at=None,
                        exit_deadline=None,
                        last_seen_at=now_ts,
                        consecutive_failures=0,
                        player_name=display_name,
                        avatar_url=avatar,
                    )
                upsert_sessions.append(refreshed)

                if (
                    ach_global
                    and self.achievement_service is not None
                    and not should_skip_game(filter_mode, filter_ids, obs_gid)
                ):
                    ach_candidates.append(
                        (sid, obs_gid, gname, display_name, refreshed.avatar_url)
                    )
            else:
                if existing is not None:
                    delete_session_sids.append(sid)

        await asyncio.to_thread(
            self.store.commit_poll_batch,
            profile_updates=profile_updates,
            upsert_sessions=upsert_sessions,
            delete_session_sids=delete_session_sids,
            last_poll_at=now_ts,
        )

        for sid, obs_gid, gname, display_name, avatar_url in ach_candidates:
            ach_groups = await asyncio.to_thread(
                self.store.get_notify_target_groups,
                sid,
                only_monitor_enabled=True,
                only_achievement_enabled=True,
            )
            if ach_groups and self.achievement_service is not None:
                self.achievement_service.start_tracking(
                    sid, obs_gid, gname, display_name, avatar_url
                )

    async def _handle_session_event(
        self,
        ev: SessionEvent,
        *,
        filter_mode: str,
        filter_ids: str,
        ach_global: bool,
    ) -> None:
        assert self.store is not None and self.steam_client is not None and self.renderer is not None

        if ev.kind == "closed" and self.achievement_service is not None:
            self.achievement_service.stop_tracking(ev.steam_id, delayed_final_check=True)

        if should_skip_game(filter_mode, filter_ids, ev.game_id):
            return

        target_groups = await asyncio.to_thread(
            self.store.get_notify_target_groups,
            ev.steam_id,
            only_monitor_enabled=True,
            only_achievement_enabled=False,
        )
        if not target_groups:
            return

        zh_name, en_name = await self.steam_client.get_game_names(ev.game_id, ev.game_name)
        game_name = zh_name or ev.game_name or f"App {ev.game_id}"
        display_name = await asyncio.to_thread(
            self.store.resolve_display_name, ev.steam_id, ev.player_name
        )

        if ev.kind == "started":
            if ach_global and self.achievement_service is not None:
                ach_groups = await asyncio.to_thread(
                    self.store.get_notify_target_groups,
                    ev.steam_id,
                    only_monitor_enabled=True,
                    only_achievement_enabled=True,
                )
                if ach_groups:
                    self.achievement_service.start_tracking(
                        ev.steam_id, ev.game_id, game_name, display_name, ev.avatar_url
                    )

            if not bool(self._cfg("enable_game_start_notify", True)):
                return

            cover_res, online_res, frame_res = await asyncio.gather(
                self.steam_client.get_game_cover_url(ev.game_id),
                self.steam_client.get_game_online_count(ev.game_id),
                self.steam_client.fetch_avatar_frame_url(ev.steam_id),
                return_exceptions=True,
            )
            cover_url = (
                cover_res
                if isinstance(cover_res, str)
                else SteamClient.default_game_cover_url(ev.game_id)
            )
            online_count = online_res if isinstance(online_res, int) else None
            avatar_frame_url = frame_res if isinstance(frame_res, str) else ""
            payload = {
                "player_name": display_name,
                "avatar_url": ev.avatar_url,
                "avatar_frame_url": avatar_frame_url,
                "game_name": game_name,
                "game_name_en": en_name,
                "appid": ev.game_id,
                "cover_url": cover_url,
                "online_count": online_count,
                "started_at": ev.started_at,
                "steam_id": ev.steam_id,
            }
            send_image = bool(self._cfg("notify_send_image", True))
            if send_image:
                image_ref, fallback_text = await self.renderer.render_game_start(payload)
            else:
                image_ref = None
                fallback_text = f"🎮 {display_name} 开始游玩 {game_name}"
            short_text = f"🎮 {display_name} 开始游玩 {game_name}"
            await self._broadcast_to_groups(
                target_groups, image_ref=image_ref, text=short_text, fallback_text=fallback_text
            )
            return

        if ev.kind == "closed":
            if not bool(self._cfg("enable_game_end_notify", True)):
                return

            cover_res, frame_res = await asyncio.gather(
                self.steam_client.get_game_cover_url(ev.game_id),
                self.steam_client.fetch_avatar_frame_url(ev.steam_id),
                return_exceptions=True,
            )
            cover_url = (
                cover_res
                if isinstance(cover_res, str)
                else SteamClient.default_game_cover_url(ev.game_id)
            )
            avatar_frame_url = frame_res if isinstance(frame_res, str) else ""
            duration_min = ev.duration_sec / 60.0
            dur_text = format_duration(ev.duration_sec)
            tip = end_game_tip(duration_min)
            payload = {
                "player_name": display_name,
                "avatar_url": ev.avatar_url,
                "avatar_frame_url": avatar_frame_url,
                "game_name": game_name,
                "game_name_en": en_name,
                "appid": ev.game_id,
                "cover_url": cover_url,
                "duration_sec": ev.duration_sec,
                "duration_text": dur_text,
                "ended_at": ev.ended_at or time.time(),
                "tip": tip,
            }
            send_image = bool(self._cfg("notify_send_image", True))
            if send_image:
                image_ref, fallback_text = await self.renderer.render_game_end(payload)
            else:
                image_ref = None
                fallback_text = f"👋 {display_name} 结束了 {game_name}（游玩 {dur_text}）\n{tip}"
            short_text = f"👋 {display_name} 结束了 {game_name}，本次游玩 {dur_text}"
            await self._broadcast_to_groups(
                target_groups, image_ref=image_ref, text=short_text, fallback_text=fallback_text
            )
            return

        if ev.kind == "fluctuation":
            if not bool(self._cfg("enable_network_fluctuation_notify", False)):
                return
            msg = f"⚡ {display_name} 疑似网络波动，已重新连回 {game_name}（会话时长继续累计）"
            await self._broadcast_to_groups(
                target_groups, image_ref=None, text=msg, fallback_text=msg
            )

    async def _on_achievements_unlocked(self, payload: dict[str, Any]) -> None:
        if self.store is None or self.steam_client is None or self.renderer is None:
            return
        if not self._is_module_enabled() or not bool(self._cfg("enable_achievement_poll", True)):
            return

        steam_id = str(payload.get("steam_id") or "").strip()
        appid = str(payload.get("appid") or "").strip()
        if not steam_id or not appid:
            return

        filter_mode = str(self._cfg("game_filter_mode", "全部游戏") or "全部游戏")
        filter_ids = str(self._cfg("game_filter_ids", "") or "")
        if should_skip_game(filter_mode, filter_ids, appid):
            return

        target_groups = await asyncio.to_thread(
            self.store.get_notify_target_groups,
            steam_id,
            only_monitor_enabled=True,
            only_achievement_enabled=True,
        )
        if not target_groups:
            return

        display_name = await asyncio.to_thread(
            self.store.resolve_display_name, steam_id, str(payload.get("player_name") or "")
        )
        cover_res, frame_res = await asyncio.gather(
            self.steam_client.get_game_cover_url(appid),
            self.steam_client.fetch_avatar_frame_url(steam_id),
            return_exceptions=True,
        )
        cover_url = (
            cover_res
            if isinstance(cover_res, str)
            else SteamClient.default_game_cover_url(appid)
        )
        avatar_frame_url = frame_res if isinstance(frame_res, str) else ""
        enriched = {
            **payload,
            "player_name": display_name,
            "cover_url": cover_url,
            "avatar_frame_url": avatar_frame_url,
            "timestamp": time.time(),
        }
        send_image = bool(self._cfg("notify_send_image", True))
        if send_image:
            image_ref, fallback_text = await self.renderer.render_achievement(enriched)
        else:
            image_ref = None
            ach_names = "、".join(
                str(a.get("display_name") or a.get("apiname") or "")
                for a in (payload.get("achievements") or [])[:5]
            )
            fallback_text = (
                f"🏆 {display_name} 在 {payload.get('game_name') or appid} 解锁了成就：{ach_names}"
            )
        count = len(payload.get("achievements") or [])
        short_text = f"🏆 {display_name} 在 {payload.get('game_name') or appid} 解锁了 {count} 项新成就！"
        await self._broadcast_to_groups(
            target_groups, image_ref=image_ref, text=short_text, fallback_text=fallback_text
        )

    # ------------------------------------------------------------------
    # Independent Daily Rank Push Loop (Natural Day 00:00)
    # ------------------------------------------------------------------
    async def _daily_rank_push_loop(self) -> None:
        while not self._closed:
            try:
                await self.check_daily_rank_push()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("[SteamMonitor] 每日排行推送检查异常")
            await asyncio.sleep(30.0)

    async def check_daily_rank_push(
        self, *, now_dt: datetime | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Check and fire daily yesterday-playtime rank push once per day."""
        if self.store is None or self.renderer is None:
            return {"pushed": False, "reason": "not_initialized"}
        if not force and not self._is_module_enabled():
            return {"pushed": False, "reason": "module_disabled"}

        now_local = now_dt if now_dt is not None else datetime.now(self._series_tz())
        push_hour = max(0, min(23, self._cfg_int("rank_push_hour", 8)))
        push_minute = max(0, min(59, self._cfg_int("rank_push_minute", 30)))
        scheduled_dt = now_local.replace(
            hour=push_hour, minute=push_minute, second=0, microsecond=0
        )

        yesterday_dt = now_local - timedelta(days=1)
        yesterday_day = yesterday_dt.strftime("%Y-%m-%d")
        date_label = yesterday_dt.strftime("%Y/%m/%d")

        if not force:
            if now_local < scheduled_dt:
                return {"pushed": False, "reason": "before_schedule", "day_key": yesterday_day}
            last_pushed = await asyncio.to_thread(self.store.get_kv, "last_rank_push_date", "")
            if last_pushed == yesterday_day:
                return {"pushed": False, "reason": "already_pushed", "day_key": yesterday_day}
            # Respect global quiet hours / owner readiness before claiming today's push slot
            if not self._can_proactive_broadcast():
                return {"pushed": False, "reason": "proactive_blocked", "day_key": yesterday_day}

        rank_groups = await asyncio.to_thread(self.store.list_rank_push_groups)
        if not rank_groups:
            return {"pushed": False, "reason": "no_groups", "day_key": yesterday_day}

        # Persist push date IMMEDIATELY before sending so restarts never double-push.
        if not force:
            await asyncio.to_thread(self.store.set_kv, "last_rank_push_date", yesterday_day)

        now_ts = now_local.timestamp()
        pushed_groups: list[str] = []
        skipped_empty: list[str] = []
        skipped_blocked: list[str] = []
        global_render_cache: tuple[str | None, str, int] | None = None
        global_card_guarded = False

        try:
            for gs in rank_groups:
                gid = str(gs.get("group_id") or "").strip()
                umo = str(gs.get("notify_session") or "").strip() or (
                    await asyncio.to_thread(self.store.get_notify_session, gid)
                )
                if not gid or not umo:
                    continue
                if not force and not self._can_proactive_broadcast(umo=umo, group_id=gid):
                    skipped_blocked.append(gid)
                    continue

                is_global = bool(gs.get("rank_push_global"))
                if is_global:
                    if global_render_cache is None:
                        entries = await self._compute_rank_entries(
                            group_id=None,
                            start_day=yesterday_day,
                            end_day=yesterday_day,
                            now_ts=now_ts,
                            tz=now_local.tzinfo,
                        )
                        if not entries:
                            global_render_cache = (None, "", 0)
                        else:
                            img_ref, fb_txt = await self._render_rank_card(
                                entries=entries,
                                title="全群昨日 Steam 游玩时长榜",
                                date_label=date_label,
                                group_label="全群汇总",
                                now_ts=now_ts,
                            )
                            if img_ref:
                                self._acquire_temp_card(img_ref)
                                global_card_guarded = True
                            global_render_cache = (img_ref, fb_txt, len(entries))
                    img_ref, fb_txt, count = global_render_cache
                    if count == 0:
                        skipped_empty.append(gid)
                        continue
                    short_txt = f"📊 昨日（{date_label}）全群 Steam 游玩时长榜已出炉！"
                    delivered = await self._deliver_notification(
                        group_id=gid,
                        session_umo=umo,
                        image_ref=img_ref,
                        text=short_txt,
                        fallback_text=fb_txt,
                        immediate=(force or self._send_worker_task is None),
                        respect_proactive_gate=not force,
                    )
                else:
                    entries = await self._compute_rank_entries(
                        group_id=gid,
                        start_day=yesterday_day,
                        end_day=yesterday_day,
                        now_ts=now_ts,
                        tz=now_local.tzinfo,
                    )
                    if not entries:
                        skipped_empty.append(gid)
                        continue
                    img_ref, fb_txt = await self._render_rank_card(
                        entries=entries,
                        title="昨日 Steam 游玩时长榜",
                        date_label=date_label,
                        group_label=f"群 {gid}",
                        now_ts=now_ts,
                    )
                    short_txt = f"📊 昨日（{date_label}）本群 Steam 游玩时长榜已出炉！"
                    self._acquire_temp_card(img_ref)
                    try:
                        delivered = await self._deliver_notification(
                            group_id=gid,
                            session_umo=umo,
                            image_ref=img_ref,
                            text=short_txt,
                            fallback_text=fb_txt,
                            immediate=(force or self._send_worker_task is None),
                            respect_proactive_gate=not force,
                        )
                    finally:
                        await self._release_temp_card(img_ref)

                if delivered:
                    pushed_groups.append(gid)
        finally:
            if global_card_guarded and global_render_cache is not None:
                await self._release_temp_card(global_render_cache[0])

        return {
            "pushed": len(pushed_groups) > 0,
            "pushed_groups": pushed_groups,
            "skipped_empty_groups": skipped_empty,
            "skipped_blocked_groups": skipped_blocked,
            "day_key": yesterday_day,
            "date_label": date_label,
        }

    # ------------------------------------------------------------------
    # Rate-Limited Notification Queue & Proactive Delivery
    # ------------------------------------------------------------------
    async def _broadcast_to_groups(
        self,
        group_ids: list[str],
        *,
        image_ref: str | None,
        text: str,
        fallback_text: str,
    ) -> None:
        if self.store is None:
            await self._cleanup_temp_card_if_unused(image_ref)
            return
        seen_umos: set[str] = set()
        self._acquire_temp_card(image_ref)
        try:
            for gid in group_ids:
                umo = await asyncio.to_thread(self.store.get_notify_session, gid)
                if not umo or umo in seen_umos:
                    continue
                if not self._can_proactive_broadcast(umo=umo, group_id=gid):
                    continue
                seen_umos.add(umo)
                await self._deliver_notification(
                    group_id=gid,
                    session_umo=umo,
                    image_ref=image_ref,
                    text=text,
                    fallback_text=fallback_text,
                    immediate=(self._send_worker_task is None),
                    respect_proactive_gate=True,
                )
        finally:
            await self._release_temp_card(image_ref)

    async def _deliver_notification(
        self,
        *,
        group_id: str,
        session_umo: str,
        image_ref: str | None,
        text: str,
        fallback_text: str,
        immediate: bool = False,
        respect_proactive_gate: bool = True,
    ) -> bool:
        if respect_proactive_gate and not self._can_proactive_broadcast(
            umo=session_umo, group_id=group_id
        ):
            return False

        send_image = bool(self._cfg("notify_send_image", True))
        send_text = bool(self._cfg("notify_send_text", True))
        if not send_image and not send_text:
            return False

        effective_img = image_ref if send_image else None
        if effective_img:
            effective_text = text if send_text else ""
        else:
            # Graceful fallback: if image was requested but failed/unavailable, send fallback_text
            effective_text = fallback_text or text

        if not effective_img and not effective_text:
            return False

        item = {
            "group_id": group_id,
            "session_umo": session_umo,
            "image_ref": effective_img,
            "text": effective_text,
            "fallback_text": fallback_text or text,
            "respect_proactive_gate": respect_proactive_gate,
        }
        self._acquire_temp_card(effective_img)
        if immediate or self._send_queue is None:
            await self._send_to_session(item)
            return True
        try:
            self._send_queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.logger.warning(
                "[SteamMonitor] 通知发送队列已满 (%d)，丢弃推送: group=%s",
                self._send_queue.maxsize,
                group_id,
            )
            await self._release_temp_card(effective_img)
            return False

    async def _notification_send_loop(self) -> None:
        assert self._send_queue is not None
        while not self._closed:
            try:
                item = await self._send_queue.get()
            except asyncio.CancelledError:
                raise
            try:
                gid = str(item.get("group_id") or "")
                now_m = time.monotonic()
                last_m = self._group_last_sent.get(gid, 0.0)
                wait_s = self._min_group_send_interval - (now_m - last_m)
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                await self._send_to_session(item)
                if gid in self._group_last_sent:
                    self._group_last_sent.pop(gid, None)
                self._group_last_sent[gid] = time.monotonic()
                while len(self._group_last_sent) > MAX_GROUP_LAST_SENT:
                    self._group_last_sent.pop(next(iter(self._group_last_sent)), None)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("[SteamMonitor] 队列消息发送失败: %s", item.get("session_umo"))
            finally:
                self._send_queue.task_done()

    async def _send_to_session(self, item: dict[str, Any]) -> None:
        image_ref = item.get("image_ref")
        try:
            umo = str(item.get("session_umo") or "").strip()
            gid = str(item.get("group_id") or "").strip()
            if not umo:
                return
            if item.get("respect_proactive_gate", True) and not self._can_proactive_broadcast(
                umo=umo, group_id=gid
            ):
                return
            send_fn = getattr(self.context, "send_message", None)
            if not callable(send_fn):
                return

            text = str(item.get("text") or "").strip()
            fallback_text = str(item.get("fallback_text") or text).strip()

            if MessageChain is not None and Plain is not None:
                chain_items: list[Any] = []
                if text:
                    chain_items.append(Plain(text))
                if image_ref:
                    comp = _build_image_component(str(image_ref))
                    if comp is not None:
                        chain_items.append(comp)
                if not chain_items and fallback_text:
                    chain_items.append(Plain(fallback_text))
                if not chain_items:
                    return
                try:
                    await send_fn(umo, MessageChain(chain_items))
                    return
                except Exception:
                    if image_ref and fallback_text:
                        self.logger.warning(
                            "[SteamMonitor] 图片推送失败，自动降级为纯文本: umo=%s", umo
                        )
                        await send_fn(umo, MessageChain([Plain(fallback_text)]))
                    else:
                        raise
            else:
                payload = {"image_ref": image_ref, "text": text or fallback_text}
                await send_fn(umo, payload)
        finally:
            await self._release_temp_card(str(image_ref) if image_ref else None)

    # ------------------------------------------------------------------
    # Optional LLM Translation Helper for Price Search
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_provider_id(provider: Any) -> str:
        if provider is None:
            return "default"
        meta = getattr(provider, "meta", None)
        if callable(meta):
            try:
                meta = meta()
            except Exception:
                meta = None
        if meta is not None:
            for attr in ("id", "provider_id"):
                val = getattr(meta, attr, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        for attr in ("provider_id", "id"):
            val = getattr(provider, attr, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return "default"

    async def _translate_game_query(self, query: str) -> str:
        try:
            provider = None
            provider_id = ""
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                provider = getter()
            if provider is None and series_panels is not None:
                resolve_fn = getattr(series_panels, "resolve_series_provider", None)
                find_fn = getattr(series_panels, "find_provider_silently", None)
                if callable(resolve_fn) and callable(find_fn):
                    pid = resolve_fn(self.context, "", role="reply")
                    if pid:
                        provider = find_fn(self.context, pid)
                        provider_id = str(pid)
            if provider is None or not callable(getattr(provider, "text_chat", None)):
                return query
            if not provider_id:
                provider_id = self._extract_provider_id(provider)

            raw_timeout = self._cfg(
                "translate_timeout_seconds", DEFAULT_TRANSLATE_TIMEOUT_SECONDS
            )
            try:
                timeout_sec = max(1.0, float(raw_timeout))
            except (TypeError, ValueError):
                timeout_sec = DEFAULT_TRANSLATE_TIMEOUT_SECONDS

            async def _invoke() -> Any:
                async with asyncio.timeout(timeout_sec):
                    return await provider.text_chat(
                        prompt=(
                            "请将以下游戏名翻译为 Steam 商店使用的英文官方名称，"
                            f"仅输出英文名，不要输出其他内容：{query}"
                        ),
                        contexts=[],
                        image_urls=[],
                        func_tool=None,
                        system_prompt="",
                    )

            llm_caller = (
                getattr(series_panels, "series_llm_call", None)
                if series_panels is not None
                else None
            )
            if callable(llm_caller):
                response = await llm_caller(
                    self.context,
                    provider_id,
                    PLUGIN_ID,
                    "steam_translate",
                    _invoke,
                    acquire_timeout=min(10.0, timeout_sec),
                )
            else:
                response = await _invoke()

            raw = str(getattr(response, "completion_text", "") or "").strip()
            closes = list(_THINK_CLOSE_PATTERN.finditer(raw))
            if closes:
                raw = raw[closes[-1].end() :]
            else:
                raw = _THINK_TAG_PATTERN.sub("", raw)
            translated = _TRANSLATE_PREFIX_PATTERN.sub("", raw).strip().strip('`"“”\'')
            return translated or query
        except TimeoutError:
            self.logger.warning("[SteamMonitor] LLM 译名超时，回退原始查询: %s", query)
            return query
        except Exception as exc:
            busy_check = (
                getattr(series_panels, "is_provider_budget_busy_error", None)
                if series_panels is not None
                else None
            )
            if callable(busy_check) and busy_check(exc):
                self.logger.debug("[SteamMonitor] 共享模型队列繁忙，译名回退原始查询: %s", query)
                return query
            self.logger.debug("[SteamMonitor] LLM 译名失败，回退原始查询: %s", exc)
            return query

    # ------------------------------------------------------------------
    # Shared Data & Rendering Helpers (Status List / Rank)
    # ------------------------------------------------------------------
    async def _build_player_rows(
        self,
        sids: list[str],
        *,
        group_labels: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        assert self.store is not None and self.steam_client is not None
        if not sids:
            return []
        statuses, failed_sids = await self.steam_client.fetch_player_statuses_batch(sids)
        active_map = await asyncio.to_thread(self.store.load_active_sessions)
        bindings_map = await asyncio.to_thread(self.store.get_bindings_map, sids)
        now_ts = time.time()
        rows: list[dict[str, Any]] = []
        profile_updates: list[tuple[str, str, str]] = []

        for sid in sids:
            st = statuses.get(sid) or {}
            active = active_map.get(sid)
            binding = bindings_map.get(sid)
            api_failed = sid in failed_sids
            persona = str(st.get("name") or "").strip()
            avatar = str(st.get("avatarfull") or st.get("avatar") or "").strip()
            if persona or avatar:
                profile_updates.append((sid, persona, avatar))
            display_name = SteamMonitorStore.resolve_display_name_from_binding(
                binding,
                sid,
                persona or (active.player_name if active else ""),
            )
            if not avatar and binding:
                avatar = str(binding.get("last_avatar_url") or "")
            if not avatar and active:
                avatar = active.avatar_url

            game_id = str(st.get("gameid") or "").strip()
            game_name = str(st.get("gameextrainfo") or "").strip()
            if not game_id and api_failed and active is not None and active.state == "playing":
                game_id = active.game_id
                game_name = active.game_name

            if game_id:
                zh_name, _ = await self.steam_client.get_game_names(game_id, game_name)
                game_name = zh_name or game_name or f"App {game_id}"
                cover_url = await self.steam_client.get_game_cover_url(game_id)
                dur_sec = (
                    max(0.0, now_ts - active.started_at)
                    if active is not None and active.game_id == game_id
                    else 0.0
                )
                status_key = "in_game"
                status_text = f"正在游玩 {game_name}"
            else:
                cover_url = ""
                dur_sec = 0.0
                pstate = int(st.get("personastate") or 0) if st else 0
                if pstate in (1, 5, 6):
                    status_key = "online"
                    status_text = "在线"
                elif pstate in (2, 3, 4):
                    status_key = "away"
                    status_text = "离开" if pstate != 2 else "忙碌"
                else:
                    status_key = "offline"
                    status_text = "查询失败" if api_failed else "离线"

            row: dict[str, Any] = {
                "steam_id": sid,
                "player_name": display_name,
                "avatar_url": avatar,
                "status": status_key,
                "personastate": int(st.get("personastate") or 0) if st else 0,
                "status_text": status_text,
                "game_id": game_id,
                "game_name": game_name,
                "cover_url": cover_url,
                "duration_sec": dur_sec,
                "duration_text": format_duration(dur_sec) if dur_sec > 0 else "",
                "last_logoff": st.get("lastlogoff") or 0,
            }
            if group_labels and sid in group_labels:
                row["group_label"] = group_labels[sid]
            rows.append(row)

        if profile_updates:
            await asyncio.to_thread(self.store.update_player_profiles_batch, profile_updates)
        return rows

    async def _compute_rank_entries(
        self,
        *,
        group_id: str | None,
        start_day: str,
        end_day: str,
        now_ts: float,
        tz: Any = None,
    ) -> list[dict[str, Any]]:
        assert self.store is not None
        if group_id is not None:
            target_sids = set(
                await asyncio.to_thread(self.store.get_group_effective_steam_ids, group_id)
            )
            if not target_sids:
                return []
        else:
            target_sids = set(
                await asyncio.to_thread(
                    self.store.list_all_monitored_steam_ids, only_enabled_groups=False
                )
            )
            if not target_sids:
                return []

        start_ts, _ = natural_day_bounds(start_day, tz=tz)
        _, end_ts = natural_day_bounds(end_day, tz=tz)

        closed_sessions = await asyncio.to_thread(
            self.store.query_play_sessions_overlapping, start_ts, end_ts, target_sids
        )
        active_sessions = list((await asyncio.to_thread(self.store.load_active_sessions)).values())
        legacy_rows = await asyncio.to_thread(
            self.store.query_legacy_daily_playtime, start_day, end_day, target_sids
        )

        ranked = aggregate_playtime_window(
            closed_sessions,
            active_sessions,
            legacy_rows,
            start_day=start_day,
            end_day=end_day,
            now_ts=now_ts,
            target_sids=target_sids,
            tz=tz,
        )
        ranked_sids = [str(item["sid"]) for item in ranked]
        bindings_map = (
            await asyncio.to_thread(self.store.get_bindings_map, ranked_sids)
            if ranked_sids
            else {}
        )
        enriched: list[dict[str, Any]] = []
        for idx, item in enumerate(ranked, 1):
            sid = str(item["sid"])
            binding = bindings_map.get(sid)
            display_name = SteamMonitorStore.resolve_display_name_from_binding(
                binding, sid, ""
            )
            avatar_url = str(binding.get("last_avatar_url") or "") if binding else ""
            top_gid = str(item.get("top_game_id") or "")
            top_gname = ""
            if item.get("games"):
                top_gname = str(item["games"][0].get("name") or "")
            top_cover_url = ""
            if top_gid and self.steam_client is not None:
                try:
                    top_cover_url = await self.steam_client.get_game_cover_url(top_gid)
                except Exception:
                    top_cover_url = SteamClient.default_game_cover_url(top_gid)
            enriched.append(
                {
                    "rank": idx,
                    "sid": sid,
                    "steam_id": sid,
                    "player_name": display_name,
                    "avatar_url": avatar_url,
                    "total_seconds": float(item["total_seconds"]),
                    "total_minutes": float(item["total_minutes"]),
                    "top_game_id": top_gid,
                    "top_game_name": top_gname,
                    "top_cover_url": top_cover_url,
                    "games": item.get("games") or [],
                }
            )
        return enriched

    async def _render_rank_card(
        self,
        *,
        entries: list[dict[str, Any]],
        title: str,
        date_label: str,
        group_label: str,
        now_ts: float,
    ) -> tuple[str | None, str]:
        assert self.renderer is not None
        total_seconds = sum(float(e.get("total_seconds") or 0.0) for e in entries)
        return await self.renderer.render_rank(
            {
                "title": title,
                "date_label": date_label,
                "group_label": group_label,
                "player_count": len(entries),
                "total_seconds": total_seconds,
                "timestamp": now_ts,
                "entries": entries,
            }
        )

    # ------------------------------------------------------------------
    # Read-Only LLM Tool Helpers
    # ------------------------------------------------------------------
    async def get_group_steam_status_summary(self, group_id: str) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if self.store is None:
            return {"available": False, "group_id": gid, "reason": "engine_not_ready"}
        sids = await asyncio.to_thread(self.store.get_group_effective_steam_ids, gid)
        if not sids:
            return {
                "available": True,
                "group_id": gid,
                "monitored_count": 0,
                "playing_count": 0,
                "online_count": 0,
                "offline_count": 0,
                "playing": [],
                "online": [],
                "summary_text": "本群暂未配置任何 Steam 监控玩家。",
            }

        playing: list[dict[str, Any]] = []
        online: list[dict[str, Any]] = []
        offline_count = 0
        has_api_key = bool(str(self._cfg("steam_api_key", "") or "").strip())

        if has_api_key and self.steam_client is not None:
            rows = await self._build_player_rows(sids)
            for row in rows:
                if row["status"] == "in_game":
                    playing.append(
                        {
                            "player_name": row["player_name"],
                            "steam_id": row["steam_id"],
                            "game_name": row["game_name"],
                            "game_id": row["game_id"],
                            "duration_text": row["duration_text"],
                            "duration_minutes": round(float(row["duration_sec"]) / 60.0, 1),
                        }
                    )
                elif row["status"] in ("online", "away"):
                    online.append(
                        {
                            "player_name": row["player_name"],
                            "steam_id": row["steam_id"],
                            "status": row["status_text"],
                        }
                    )
                else:
                    offline_count += 1
        else:
            active_map = await asyncio.to_thread(self.store.load_active_sessions)
            now_ts = time.time()
            for sid in sids:
                act = active_map.get(sid)
                if act is not None and act.state == "playing":
                    dur_sec = max(0.0, now_ts - act.started_at)
                    playing.append(
                        {
                            "player_name": await asyncio.to_thread(
                                self.store.resolve_display_name, sid, act.player_name
                            ),
                            "steam_id": sid,
                            "game_name": act.game_name or f"App {act.game_id}",
                            "game_id": act.game_id,
                            "duration_text": format_duration(dur_sec),
                            "duration_minutes": round(dur_sec / 60.0, 1),
                        }
                    )
                else:
                    offline_count += 1

        if playing:
            lines = [
                f"{p['player_name']} 正在玩《{p['game_name']}》"
                + (f"（已玩 {p['duration_text']}）" if p["duration_text"] else "")
                for p in playing
            ]
            summary = f"本群共监控 {len(sids)} 人，当前 {len(playing)} 人在游戏中：" + "；".join(lines)
        else:
            summary = f"本群共监控 {len(sids)} 人，当前无人正在玩 Steam 游戏（在线 {len(online)} 人，离线 {offline_count} 人）。"

        return {
            "available": True,
            "group_id": gid,
            "monitored_count": len(sids),
            "playing_count": len(playing),
            "online_count": len(online),
            "offline_count": offline_count,
            "playing": playing,
            "online": online,
            "summary_text": summary,
        }

    async def get_group_steam_rank_summary(
        self, group_id: str, days: int = 1
    ) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if self.store is None:
            return {"available": False, "group_id": gid, "reason": "engine_not_ready"}
        clamped_days = max(1, min(90, int(days or 1)))
        tz = self._series_tz()
        now_local = datetime.now(tz)
        start_day, end_day, label, date_label = parse_rank_period(
            str(clamped_days), now_dt=now_local
        )
        now_ts = now_local.timestamp()
        entries = await self._compute_rank_entries(
            group_id=gid, start_day=start_day, end_day=end_day, now_ts=now_ts, tz=tz
        )
        rankings = [
            {
                "rank": e["rank"],
                "player_name": e["player_name"],
                "steam_id": e["steam_id"],
                "total_minutes": round(float(e["total_minutes"]), 1),
                "duration_text": format_duration(float(e["total_seconds"])),
                "top_game_name": e["top_game_name"],
                "games": [
                    {
                        "name": g.get("name", ""),
                        "minutes": round(float(g.get("minutes") or 0.0), 1),
                    }
                    for g in (e.get("games") or [])[:5]
                ],
            }
            for e in entries
        ]
        if rankings:
            top_str = "；".join(
                f"第{r['rank']}名 {r['player_name']} {r['duration_text']}（主玩 {r['top_game_name'] or '未知'}）"
                for r in rankings[:5]
            )
            summary = f"本群{label}（{date_label}）Steam 时长排行：{top_str}"
        else:
            summary = f"本群{label}（{date_label}）暂无 Steam 游玩时长记录。"

        return {
            "available": True,
            "group_id": gid,
            "days": clamped_days,
            "period_label": label,
            "start_day": start_day,
            "end_day": end_day,
            "date_label": date_label,
            "total_players": len(rankings),
            "rankings": rankings,
            "summary_text": summary,
        }

    # ------------------------------------------------------------------
    # Interactive Price Candidate Selection Listener
    # ------------------------------------------------------------------
    async def on_message(self, event: Any) -> AsyncIterator[Any]:
        if self.price_service is None or self.renderer is None:
            return
        session_key = self._search_session_key(event)
        if not self.price_service.has_pending_selection(session_key):
            return
        text = self._event_text(event)
        if not text or text.startswith("/"):
            return
        match = _SELECTION_INDEX_PATTERN.match(text)
        if not match:
            return

        stop_fn = getattr(event, "stop_event", None)
        if callable(stop_fn):
            stop_fn()

        idx = int(match.group(1))
        candidate = self.price_service.select_candidate(session_key, idx)
        if candidate is None:
            yield event.plain_result("候选序号无效，请回复候选列表中的有效数字序号。")
            return

        card = await self.price_service.build_price_card(candidate)
        if not card:
            yield event.plain_result("获取该游戏价格详情失败，请稍后重试。")
            return
        img_ref, fallback_text = await self.renderer.render_game_detail(card)
        caption = f"🔗 Steam 商店：{card['store_url']}" if card.get("store_url") else ""
        try:
            yield self._make_reply(event, img_ref, fallback_text, caption=caption)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    # ------------------------------------------------------------------
    # Command Handlers — All Users
    # ------------------------------------------------------------------
    async def cmd_help(self, event: Any) -> AsyncIterator[Any]:
        yield event.plain_result(HELP_TEXT)

    async def cmd_list(self, event: Any) -> AsyncIterator[Any]:
        if self.store is None or self.steam_client is None or self.renderer is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam list。")
            return
        await self._record_group_session(event, gid)
        if not str(self._cfg("steam_api_key", "") or "").strip():
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        sids = await asyncio.to_thread(self.store.get_group_effective_steam_ids, gid)
        if not sids:
            yield event.plain_result("本群暂未添加监控的 Steam 账号，可使用 /steam bind <SteamID> 绑定加入。")
            return
        rows = await self._build_player_rows(sids)
        img_ref, fallback_text = await self.renderer.render_player_list(
            {
                "title": "本群 Steam 玩家实时状态",
                "group_label": f"群 {gid}",
                "is_single": False,
                "updated_at": time.time(),
                "players": rows,
            }
        )
        try:
            yield self._make_reply(event, img_ref, fallback_text)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_who(self, event: Any, target: str = "") -> AsyncIterator[Any]:
        if self.store is None or self.steam_client is None or self.renderer is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        if not str(self._cfg("steam_api_key", "") or "").strip():
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return

        at_qqs = self._extract_at_qqs(event, target)
        target_clean = str(target or "").strip()
        qq_id = ""
        if at_qqs:
            qq_id = at_qqs[0]
        elif target_clean:
            stripped = target_clean.lstrip("@").strip()
            if stripped.isdigit():
                qq_id = stripped
        else:
            qq_id = self._sender_id(event)

        sid = ""
        if qq_id:
            binding = await asyncio.to_thread(self.store.get_binding_by_qq, qq_id)
            if binding:
                sid = str(binding.get("steam_id") or "")
        if not sid and target_clean and not at_qqs:
            resolved = await self.steam_client.resolve_steam_input(target_clean)
            if resolved:
                sid = resolved

        if not sid:
            who_label = f"QQ {qq_id}" if qq_id else (target_clean or "你")
            yield event.plain_result(
                f"{who_label} 尚未绑定 SteamID，可使用 /steam bind <SteamID/链接/好友码> 自助绑定。"
            )
            return

        rows = await self._build_player_rows([sid])
        if not rows:
            yield event.plain_result(f"无法获取 SteamID {sid} 的状态，请稍后重试。")
            return

        row = rows[0]
        frame_url = await self.steam_client.fetch_avatar_frame_url(sid)
        if frame_url:
            row["avatar_frame_url"] = frame_url
        img_ref, fallback_text = await self.renderer.render_player_list(
            {
                "title": f"{row['player_name']} 在干嘛？",
                "is_single": True,
                "updated_at": time.time(),
                "players": [row],
            }
        )
        try:
            yield self._make_reply(event, img_ref, fallback_text)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_rank(self, event: Any, period: str = "") -> AsyncIterator[Any]:
        if self.store is None or self.renderer is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam rank。")
            return
        await self._record_group_session(event, gid)
        tz = self._series_tz()
        now_local = datetime.now(tz)
        start_day, end_day, label, date_label = parse_rank_period(
            period, now_dt=now_local
        )
        now_ts = now_local.timestamp()
        entries = await self._compute_rank_entries(
            group_id=gid, start_day=start_day, end_day=end_day, now_ts=now_ts, tz=tz
        )
        if not entries:
            yield event.plain_result(f"本群{label}（{date_label}）暂无 Steam 游玩记录。")
            return
        img_ref, fallback_text = await self._render_rank_card(
            entries=entries,
            title=f"本群{label} Steam 游玩时长榜",
            date_label=date_label,
            group_label=f"群 {gid}",
            now_ts=now_ts,
        )
        try:
            yield self._make_reply(event, img_ref, fallback_text)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_price(
        self, event: Any, query: str = "", *, quick: bool = False
    ) -> AsyncIterator[Any]:
        if self.price_service is None or self.renderer is None:
            yield event.plain_result("Steam 商店查价服务尚未就绪。")
            return
        clean_query = str(query or "").strip()
        cmd_name = "px" if quick else "price"
        if not clean_query:
            yield event.plain_result(f"用法：/steam {cmd_name} <游戏名或 Steam 商店链接>")
            return

        session_key = self._search_session_key(event)
        candidate: dict[str, Any] | None = None

        if (
            not quick
            and clean_query.isdigit()
            and self.price_service.has_pending_selection(session_key)
        ):
            candidate = self.price_service.select_candidate(session_key, int(clean_query))
            if candidate is None:
                yield event.plain_result("候选序号无效，请回复候选列表中的有效数字序号。")
                return
        else:
            candidates = await self.price_service.search_candidates(
                clean_query, session_key=None if quick else session_key
            )
            if not candidates:
                yield event.plain_result(f"未找到与「{clean_query}」匹配的 Steam 游戏，或商店暂时无法访问。")
                return
            if not quick and len(candidates) > 1:
                yield event.plain_result(
                    self.price_service.format_candidates_prompt(candidates)
                )
                return
            candidate = candidates[0]

        card = await self.price_service.build_price_card(candidate)
        if not card:
            yield event.plain_result(f"未能获取「{clean_query}」的价格详情，请稍后重试。")
            return
        img_ref, fallback_text = await self.renderer.render_game_detail(card)
        caption = f"🔗 Steam 商店：{card['store_url']}" if card.get("store_url") else ""
        try:
            yield self._make_reply(event, img_ref, fallback_text, caption=caption)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_px(self, event: Any, query: str = "") -> AsyncIterator[Any]:
        async for r in self.cmd_price(event, query, quick=True):
            yield r

    async def cmd_game(self, event: Any, appid_raw: str = "") -> AsyncIterator[Any]:
        if self.price_service is None or self.renderer is None:
            yield event.plain_result("Steam 商店服务尚未就绪。")
            return
        raw = str(appid_raw or "").strip()
        appid = raw if raw.isdigit() else (PriceService.extract_appid(raw) or "")
        if not appid:
            yield event.plain_result("用法：/steam game <Steam AppID 或商店链接>")
            return
        card = await self.price_service.build_game_detail_card(appid)
        if not card:
            yield event.plain_result(f"未找到 Steam 游戏 AppID：{appid}，或商店暂时无法访问。")
            return
        img_ref, fallback_text = await self.renderer.render_game_detail(card)
        caption = f"🔗 Steam 商店：{card['store_url']}" if card.get("store_url") else ""
        try:
            yield self._make_reply(event, img_ref, fallback_text, caption=caption)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_openbox(self, event: Any, target: str = "") -> AsyncIterator[Any]:
        if self.store is None or self.steam_client is None or self.renderer is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        if not str(self._cfg("steam_api_key", "") or "").strip():
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return

        target_clean = str(target or "").strip()
        at_qqs = self._extract_at_qqs(event, target_clean)
        sid = ""
        if at_qqs:
            binding = await asyncio.to_thread(self.store.get_binding_by_qq, at_qqs[0])
            if binding:
                sid = str(binding.get("steam_id") or "")
        elif not target_clean:
            sender = self._sender_id(event)
            if sender:
                binding = await asyncio.to_thread(self.store.get_binding_by_qq, sender)
                if binding:
                    sid = str(binding.get("steam_id") or "")
        else:
            resolved = await self.steam_client.resolve_steam_input(target_clean)
            if resolved:
                sid = resolved
            elif target_clean.isdigit():
                binding = await asyncio.to_thread(self.store.get_binding_by_qq, target_clean)
                if binding:
                    sid = str(binding.get("steam_id") or "")

        if not sid:
            yield event.plain_result(
                "用法：/steam openbox <17位SteamID64 / 社区链接 / 自定义ID / 好友码 / @已绑定用户>"
            )
            return

        profile = await self.steam_client.fetch_openbox_profile(sid)
        if not profile or not profile.get("summary"):
            yield event.plain_result(f"无法获取 SteamID {sid} 的公开档案，请检查 ID 或 API Key。")
            return
        img_ref, fallback_text = await self.renderer.render_openbox(profile)
        try:
            yield self._make_reply(event, img_ref, fallback_text)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_bind(
        self, event: Any, steamid_raw: str = "", nickname: str = ""
    ) -> AsyncIterator[Any]:
        if self.store is None or self.steam_client is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        raw = str(steamid_raw or "").strip()
        if not raw:
            yield event.plain_result("用法：/steam bind <SteamID64/个人资料链接/自定义ID/好友码> [昵称]")
            return
        sender_id = self._sender_id(event)
        if not sender_id:
            yield event.plain_result("无法识别当前发送者 QQ，绑定失败。")
            return

        sid = await self.steam_client.resolve_steam_input(raw)
        if not sid:
            yield event.plain_result(
                f"无法解析「{raw}」为有效 SteamID。\n支持格式：17位SteamID64 / 个人资料链接 / 自定义ID / 好友码"
            )
            return

        nick_clean = str(nickname or "").strip()
        await asyncio.to_thread(
            self.store.upsert_binding,
            sid,
            qq_id=sender_id,
            nickname=nick_clean if nick_clean else None,
        )

        gid = self._group_id(event)
        group_note = ""
        if gid:
            await self._record_group_session(event, gid)
            current = await asyncio.to_thread(self.store.list_group_players, gid)
            max_size = max(1, int(self._cfg("max_group_size", 30) or 30))
            if sid in current:
                group_note = "（已在当前群监控列表中）"
            elif len(current) >= max_size:
                group_note = f"（当前群监控人数已达上限 {max_size} 人，仅完成 QQ 绑定）"
            else:
                await asyncio.to_thread(self.store.add_group_player, gid, sid)
                group_note = "，并已自动加入本群监控列表"

        nick_note = f"，备注昵称：{nick_clean}" if nick_clean else ""
        yield event.plain_result(f"✅ 已将你的 QQ({sender_id}) 绑定至 SteamID {sid}{nick_note}{group_note}。")

    async def cmd_unbind(self, event: Any) -> AsyncIterator[Any]:
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        sender_id = self._sender_id(event)
        if not sender_id:
            yield event.plain_result("无法识别当前发送者 QQ。")
            return
        existing = await asyncio.to_thread(self.store.get_binding_by_qq, sender_id)
        if not existing:
            yield event.plain_result("你当前尚未绑定任何 SteamID。")
            return
        sid = str(existing.get("steam_id") or "")
        await asyncio.to_thread(self.store.delete_binding_by_qq, sender_id)
        yield event.plain_result(
            f"✅ 已解除 QQ({sender_id}) 与 SteamID {sid} 的绑定。（如需退出本群监控可使用 /steam delid {sid}）"
        )

    async def cmd_addid(self, event: Any, raw_args: str = "") -> AsyncIterator[Any]:
        """Add SteamID(s) to current group.

        - Non-admin: allowed ONLY for self-addid (single ID, no @other user); auto-binds to sender QQ.
        - Admin: allowed to add multiple comma-separated IDs and optionally bind @user [nickname].
        """
        if self.store is None or self.steam_client is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam addid。")
            return

        args_text = str(raw_args or "").strip()
        if not args_text:
            yield event.plain_result("用法：/steam addid <SteamID/链接/好友码> [@用户] [昵称]")
            return

        sender_id = self._sender_id(event)
        is_admin = self._is_admin(event)
        at_qqs = self._extract_at_qqs(event, args_text)

        # Strip @mention tokens from args_text so we cleanly split steam_raw and nickname
        cleaned_text = _AT_TEXT_PATTERN.sub(" ", args_text).strip()
        parts = [p for p in cleaned_text.split() if p]
        if not parts:
            yield event.plain_result("请提供有效的 SteamID / 链接 / 好友码。")
            return

        steam_part = parts[0]
        nickname = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        raw_items = [item.strip() for item in re.split(r"[,，]+", steam_part) if item.strip()]
        if not raw_items:
            yield event.plain_result("请提供有效的 SteamID / 链接 / 好友码。")
            return

        target_qq = at_qqs[0] if at_qqs else ""
        managing_others = bool(target_qq and target_qq != sender_id) or len(raw_items) > 1
        if managing_others and not is_admin:
            yield event.plain_result(
                "❌ 权限不足：普通群成员仅可使用 /steam addid <自己的SteamID> [昵称] 添加自己；"
                "替他人添加或批量添加仅限 Bot 管理员。"
            )
            return

        resolved_sids: list[str] = []
        invalid_items: list[str] = []
        for item in raw_items:
            sid = await self.steam_client.resolve_steam_input(item)
            if sid and sid.isdigit() and len(sid) == 17:
                if sid not in resolved_sids:
                    resolved_sids.append(sid)
            else:
                invalid_items.append(item)

        if invalid_items:
            yield event.plain_result(
                f"以下输入无法解析为有效 SteamID：{', '.join(invalid_items)}\n"
                "支持格式：17位SteamID64 / 个人资料链接 / 自定义ID / 好友码"
            )
            return

        await self._record_group_session(event, gid)
        current = await asyncio.to_thread(self.store.list_group_players, gid)
        max_size = max(1, int(self._cfg("max_group_size", 30) or 30))

        added: list[str] = []
        existed: list[str] = []
        skipped_limit: list[str] = []
        current_set = set(current)

        for sid in resolved_sids:
            if sid in current_set:
                existed.append(sid)
                continue
            if len(current_set) >= max_size:
                skipped_limit.append(sid)
                continue
            await asyncio.to_thread(self.store.add_group_player, gid, sid)
            current_set.add(sid)
            added.append(sid)

        # Binding behavior:
        # - Admin with @user: bind that user to resolved_sids[0]
        # - Non-admin self-addid: bind sender_id to resolved_sids[0]
        # - Admin without @user but with nickname: store nickname on resolved_sids[0]
        bind_qq = target_qq if target_qq else (sender_id if not is_admin else "")
        if len(resolved_sids) == 1 and (bind_qq or nickname):
            await asyncio.to_thread(
                self.store.upsert_binding,
                resolved_sids[0],
                qq_id=bind_qq or None,
                nickname=nickname or None,
            )

        lines: list[str] = []
        if added:
            lines.append(f"✅ 已添加至本群监控：{', '.join(added)}")
        if existed:
            lines.append(f"ℹ️ 已在监控列表中：{', '.join(existed)}")
        if skipped_limit:
            lines.append(f"⚠️ 超出单群上限({max_size}人)未添加：{', '.join(skipped_limit)}")
        if len(resolved_sids) == 1 and bind_qq:
            lines.append(
                f"🔗 已绑定 QQ({bind_qq}) ↔ {resolved_sids[0]}"
                + (f"（昵称：{nickname}）" if nickname else "")
            )
        elif len(resolved_sids) == 1 and nickname:
            lines.append(f"🏷️ 已设置备注昵称：{nickname}")
        yield event.plain_result("\n".join(lines) if lines else "无变更。")

    async def cmd_delid(
        self, event: Any, steamid_raw: str = "", group_id_param: str = ""
    ) -> AsyncIterator[Any]:
        """Remove a SteamID from monitoring.

        - Non-admin: may ONLY remove their own bound SteamID from the current group.
        - Admin: may remove any SteamID from current or specified group.
        """
        if self.store is None or self.steam_client is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return

        is_admin = self._is_admin(event)
        sender_id = self._sender_id(event)
        current_gid = self._group_id(event)
        explicit_gid = str(group_id_param or "").strip()

        if explicit_gid and explicit_gid != current_gid and not is_admin:
            yield event.plain_result("❌ 权限不足：跨群删除监控仅限 Bot 管理员。")
            return

        target_gid = explicit_gid or current_gid
        if not target_gid:
            yield event.plain_result("请在群聊中使用 /steam delid，或管理员指定群号。")
            return

        raw = str(steamid_raw or "").strip()
        if not raw:
            if not is_admin and sender_id:
                own = await asyncio.to_thread(self.store.get_binding_by_qq, sender_id)
                if own and own.get("steam_id"):
                    raw = str(own["steam_id"])
            if not raw:
                yield event.plain_result("用法：/steam delid <SteamID/链接/好友码> [群号]")
                return

        sid = await self.steam_client.resolve_steam_input(raw)
        if not sid:
            yield event.plain_result("无法解析为有效 SteamID，支持格式：17位SteamID64 / 链接 / 好友码")
            return

        if not is_admin:
            own = (
                await asyncio.to_thread(self.store.get_binding_by_qq, sender_id)
                if sender_id
                else None
            )
            if not own or str(own.get("steam_id") or "") != sid:
                yield event.plain_result(
                    "❌ 权限不足：普通成员仅可移除自己绑定的 SteamID；移除他人需 Bot 管理员权限。"
                )
                return

        removed_primary = await asyncio.to_thread(
            self.store.remove_group_player, target_gid, sid
        )
        removed_route = False
        if not removed_primary:
            removed_route = await asyncio.to_thread(
                self.store.remove_push_route, sid, target_gid
            )

        if not removed_primary and not removed_route:
            yield event.plain_result(f"群 {target_gid} 的监控列表或副群路由中不存在 SteamID {sid}。")
            return

        remaining_groups = await asyncio.to_thread(self.store.get_groups_for_steam_id, sid)
        remaining_routes = await asyncio.to_thread(self.store.list_push_routes, steam_id=sid)
        if not remaining_groups and not remaining_routes:
            await asyncio.to_thread(self.store.delete_active_session, sid)
            if self.achievement_service is not None:
                self.achievement_service.stop_tracking(sid, delayed_final_check=False)

        if removed_route:
            yield event.plain_result(f"✅ 已移除群 {target_gid} 对 SteamID {sid} 的副群推送路由。")
        else:
            yield event.plain_result(f"✅ 已从群 {target_gid} 监控列表中移除 SteamID {sid}。")

    # ------------------------------------------------------------------
    # Command Handlers — Bot Admins Only
    # ------------------------------------------------------------------
    async def cmd_on(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam on。")
            return
        await self._record_group_session(event, gid)
        await asyncio.to_thread(self.store.set_group_switch, gid, "monitor_enabled", True)
        count = len(await asyncio.to_thread(self.store.get_group_effective_steam_ids, gid))
        api_tip = (
            ""
            if str(self._cfg("steam_api_key", "") or "").strip()
            else "\n⚠️ 当前尚未配置 steam_api_key，请在插件配置中填写后生效。"
        )
        yield event.plain_result(
            f"✅ 已开启本群（{gid}）Steam 状态监控推送（当前监控 {count} 人）。{api_tip}"
        )

    async def cmd_off(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam off。")
            return
        await asyncio.to_thread(self.store.set_group_switch, gid, "monitor_enabled", False)
        yield event.plain_result(f"✅ 已关闭本群（{gid}）Steam 状态监控推送。")

    async def cmd_achievement_on(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam achievement_on。")
            return
        await self._record_group_session(event, gid)
        await asyncio.to_thread(self.store.set_group_switch, gid, "achievement_enabled", True)
        yield event.plain_result(f"✅ 已开启本群（{gid}）Steam 成就解锁推送。")

    async def cmd_achievement_off(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam achievement_off。")
            return
        await asyncio.to_thread(self.store.set_group_switch, gid, "achievement_enabled", False)
        yield event.plain_result(f"✅ 已关闭本群（{gid}）Steam 成就解锁推送。")

    async def cmd_rank_on(self, event: Any, param: str = "") -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return

        gid = self._group_id(event)
        parts = str(param or "").strip().split()
        action = parts[0].lower() if parts else ""
        push_hour = max(0, min(23, self._cfg_int("rank_push_hour", 8)))
        push_minute = max(0, min(59, self._cfg_int("rank_push_minute", 30)))
        sched = f"{push_hour:02d}:{push_minute:02d}"

        if action == "list":
            groups = await asyncio.to_thread(self.store.list_rank_push_groups)
            last_date = await asyncio.to_thread(self.store.get_kv, "last_rank_push_date", "")
            if not groups:
                yield event.plain_result(
                    f"📅 每日排行推送时间：{sched}\n当前暂无开启每日排行推送的群聊。"
                )
                return
            lines = [
                f"📅 每日排行推送时间：{sched}（上次推送日期：{last_date or '未推送'}）",
                "已开启群聊：",
            ]
            for gs in groups:
                mode = "全群汇总" if gs.get("rank_push_global") else "本群排行"
                lines.append(f"• 群 {gs['group_id']} [{mode}]")
            yield event.plain_result("\n".join(lines))
            return

        if action == "test":
            res = await self.check_daily_rank_push(force=True)
            pushed = res.get("pushed_groups") or []
            skipped = res.get("skipped_empty_groups") or []
            yield event.plain_result(
                f"🧪 昨日（{res.get('date_label', '')}）排行测试推送完成："
                f"已推送 {len(pushed)} 个群，跳过无记录群 {len(skipped)} 个。"
            )
            return

        if action in ("del", "off", "remove"):
            target_gid = parts[1].strip() if len(parts) > 1 else gid
            if not target_gid:
                yield event.plain_result("用法：/steam rank_on del [群号]")
                return
            await asyncio.to_thread(
                self.store.set_group_switch, target_gid, "rank_push_enabled", False
            )
            yield event.plain_result(f"✅ 已关闭群 {target_gid} 的每日排行榜定时推送。")
            return

        if not gid:
            yield event.plain_result("请在群聊中使用 /steam rank_on。")
            return
        await self._record_group_session(event, gid)
        is_global = action == "all"
        await asyncio.to_thread(
            self.store.upsert_group_settings,
            gid,
            notify_session=self._session_umo(event) or None,
            rank_push_enabled=True,
            rank_push_global=is_global,
        )
        mode_label = "全群汇总榜" if is_global else "本群昨日榜"
        yield event.plain_result(
            f"✅ 已为本群（{gid}）开启每日 {sched} 定时推送「{mode_label}」。"
        )

    async def cmd_push_group(self, event: Any, steamid_raw: str = "") -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None or self.steam_client is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = self._group_id(event)
        if not gid:
            yield event.plain_result("请在群聊中使用 /steam push_group <SteamID>。")
            return
        sid = await self.steam_client.resolve_steam_input(steamid_raw)
        if not sid:
            yield event.plain_result("用法：/steam push_group <SteamID/链接/好友码>")
            return
        await self._record_group_session(event, gid)
        await asyncio.to_thread(self.store.add_push_route, sid, gid)
        yield event.plain_result(f"✅ 已将本群（{gid}）加入 SteamID {sid} 的副群联动推送路由。")

    async def cmd_delpush_group(
        self, event: Any, steamid_raw: str = "", target_group: str = ""
    ) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None or self.steam_client is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = str(target_group or "").strip() or self._group_id(event)
        if not gid:
            yield event.plain_result("用法：/steam delpush_group <SteamID> [群号]")
            return
        sid = await self.steam_client.resolve_steam_input(steamid_raw)
        if not sid:
            yield event.plain_result("用法：/steam delpush_group <SteamID/链接/好友码> [群号]")
            return
        removed = await asyncio.to_thread(self.store.remove_push_route, sid, gid)
        if removed:
            yield event.plain_result(f"✅ 已移除群 {gid} 对 SteamID {sid} 的副群联动推送路由。")
        else:
            yield event.plain_result(f"群 {gid} 未配置对 SteamID {sid} 的副群推送路由。")

    async def cmd_clear_groupids(
        self, event: Any, group_id_param: str = ""
    ) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        gid = str(group_id_param or "").strip() or self._group_id(event)
        if not gid:
            yield event.plain_result("用法：/steam clear_groupids [群号]")
            return
        removed = await asyncio.to_thread(self.store.clear_group_players, gid)
        yield event.plain_result(f"✅ 已清空群 {gid} 的监控列表（共移除 {removed} 条记录）。")

    async def cmd_clear_allids(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        removed = await asyncio.to_thread(self.store.clear_all_players)
        await self._reset_achievement_service()
        if isinstance(removed, dict):
            gp = int(removed.get("group_players", 0))
            pr = int(removed.get("push_routes", 0))
            ac = int(removed.get("active_sessions", 0))
            detail = f"群监控 {gp} 条、副群路由 {pr} 条、活跃会话 {ac} 条"
        else:
            detail = f"共移除 {removed} 条记录"
        yield event.plain_result(f"✅ 已清空所有群的监控名单与活跃会话（{detail}）。")

    async def cmd_alllist(self, event: Any, mode: str = "img") -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None or self.steam_client is None or self.renderer is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        if not str(self._cfg("steam_api_key", "") or "").strip():
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return

        all_map = await asyncio.to_thread(self.store.list_all_group_players)
        sid_groups: dict[str, list[str]] = {}
        for gid, sids in all_map.items():
            for sid in sids:
                sid_groups.setdefault(sid, []).append(gid)
        all_sids = sorted(sid_groups.keys())
        if not all_sids:
            yield event.plain_result("当前所有群聊均无监控中的 SteamID。")
            return

        group_labels = {
            sid: ",".join(gids[:2]) + ("…" if len(gids) > 2 else "")
            for sid, gids in sid_groups.items()
        }
        rows = await self._build_player_rows(all_sids, group_labels=group_labels)
        if str(mode or "").strip().lower() == "text":
            lines = [f"🌐 全群 Steam 玩家状态（共 {len(rows)} 人）"]
            for r in rows:
                extra = f"《{r['game_name']}》" if r["status"] == "in_game" else r["status_text"]
                lines.append(f"• {r['player_name']} ({r['steam_id']}) [{r.get('group_label','')}] - {extra}")
            yield event.plain_result("\n".join(lines))
            return

        img_ref, fallback_text = await self.renderer.render_player_list(
            {
                "title": "全群 Steam 玩家实时状态",
                "group_label": f"共 {len(all_map)} 群 · {len(rows)} 人",
                "is_single": False,
                "updated_at": time.time(),
                "players": rows,
            }
        )
        try:
            yield self._make_reply(event, img_ref, fallback_text)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_allrank(self, event: Any, period: str = "") -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None or self.renderer is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        tz = self._series_tz()
        now_local = datetime.now(tz)
        start_day, end_day, label, date_label = parse_rank_period(
            period, now_dt=now_local
        )
        now_ts = now_local.timestamp()
        entries = await self._compute_rank_entries(
            group_id=None, start_day=start_day, end_day=end_day, now_ts=now_ts, tz=tz
        )
        if not entries:
            yield event.plain_result(f"全群{label}（{date_label}）暂无 Steam 游玩记录。")
            return
        img_ref, fallback_text = await self._render_rank_card(
            entries=entries,
            title=f"全群{label} Steam 游玩时长榜",
            date_label=date_label,
            group_label="全群汇总",
            now_ts=now_ts,
        )
        try:
            yield self._make_reply(event, img_ref, fallback_text)
        finally:
            await self._cleanup_temp_card_if_unused(img_ref)

    async def cmd_config(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        lines = ["⚙️ Steam 状态监控当前配置："]
        for key in DEFAULT_STEAM_MONITOR_CONFIG:
            val = self._cfg(key)
            if key in SECRET_CONFIG_KEYS:
                lines.append(f"• {key}: {_mask_secret(val)}")
            else:
                lines.append(f"• {key}: {val}")
        yield event.plain_result("\n".join(lines))

    @classmethod
    def _is_group_chat(cls, event: Any) -> bool:
        if cls._group_id(event):
            return True
        umo = cls._session_umo(event)
        if ":GroupMessage:" in umo:
            return True
        return False

    def _validate_ssl_ca_file(self, raw_path: str) -> str:
        val = str(raw_path or "").strip()
        if not val:
            return ""
        if "\x00" in val or ".." in Path(val).parts:
            raise ValueError("ssl_ca_file 路径不允许包含非法字符或上级目录穿越")
        expanded = Path(os.path.expanduser(val))
        if expanded.suffix.lower() not in ALLOWED_CA_FILE_EXTENSIONS:
            raise ValueError(
                f"ssl_ca_file 仅允许证书文件扩展名（{', '.join(sorted(ALLOWED_CA_FILE_EXTENSIONS))}）"
            )
        resolved = expanded.resolve()
        if not resolved.is_file():
            raise ValueError("ssl_ca_file 指向的文件不存在或不是普通文件")
        allowed_roots: list[Path] = []
        if self.data_dir is not None:
            allowed_roots.append(self.data_dir.resolve())
        for sys_root in ("/etc/ssl/certs", "/etc/pki/tls/certs"):
            p = Path(sys_root)
            if p.exists():
                allowed_roots.append(p.resolve())
        if not allowed_roots or not any(
            resolved == root or root in resolved.parents for root in allowed_roots
        ):
            raise ValueError(
                "ssl_ca_file 仅允许位于插件数据目录或系统 CA 证书目录内"
            )
        try:
            ssl.create_default_context(cafile=str(resolved))
        except Exception as exc:
            raise ValueError(f"ssl_ca_file 不是有效的 CA 证书文件：{exc}") from exc
        return str(resolved)

    @staticmethod
    def _validate_proxy_url(raw_url: str) -> str:
        val = str(raw_url or "").strip()
        if not val:
            return ""
        sp = urlsplit(val)
        if (sp.scheme or "").lower() not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError("proxy_url 仅支持 http/https/socks5/socks5h 协议")
        if not sp.hostname:
            raise ValueError("proxy_url 缺少有效主机名")
        try:
            _ = sp.port
        except ValueError as exc:
            raise ValueError("proxy_url 端口无效") from exc
        return val

    async def cmd_set(
        self, event: Any, key: str = "", value: str = ""
    ) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        k = str(key or "").strip()
        v_raw = str(value or "").strip()
        if not k or k not in DEFAULT_STEAM_MONITOR_CONFIG:
            yield event.plain_result(
                f"无效配置项：{k or '(空)'}\n支持热更新参数包括：poll_interval_seconds, exit_debounce_seconds, "
                "max_group_size, enable_achievement_poll, enable_game_start_notify, enable_game_end_notify, "
                "enable_network_fluctuation_notify, notify_send_image, notify_send_text, "
                "game_filter_mode, game_filter_ids, rank_push_hour, rank_push_minute, "
                "price_currency, price_region, price_compare_regions, detailed_poll_log 等。"
            )
            return

        if k in SENSITIVE_CONFIG_KEYS and self._is_group_chat(event):
            yield event.plain_result(
                f"❌ 安全限制：禁止在群聊中修改敏感配置项（{k}），请使用管理员私聊或 AstrBot 管理面板配置。"
            )
            return

        default_val = DEFAULT_STEAM_MONITOR_CONFIG[k]
        try:
            if isinstance(default_val, bool):
                parsed: Any = _parse_bool(v_raw)
            elif isinstance(default_val, int):
                parsed = int(v_raw)
                if k == "poll_interval_seconds":
                    parsed = max(30, parsed)
                elif k == "exit_debounce_seconds":
                    parsed = max(30, parsed)
                elif k == "rank_push_hour":
                    if not (0 <= parsed <= 23):
                        raise ValueError("rank_push_hour 必须在 0~23 之间")
                elif k == "rank_push_minute":
                    if not (0 <= parsed <= 59):
                        raise ValueError("rank_push_minute 必须在 0~59 之间")
                elif k in ("retry_times", "max_group_size", "achievement_poll_interval_seconds"):
                    parsed = max(1, parsed)
                elif k in ("cache_avatar_hours", "cache_cover_hours"):
                    parsed = max(0, parsed)
            else:
                parsed = v_raw
                if k == "game_filter_mode" and parsed not in VALID_FILTER_MODES:
                    raise ValueError("game_filter_mode 仅支持：全部游戏 / 白名单 / 黑名单")
                elif k in API_BASE_CONFIG_KEYS:
                    parsed = validate_api_base_url(k, parsed)
                elif k == "ssl_ca_file":
                    parsed = self._validate_ssl_ca_file(parsed)
                elif k == "proxy_url":
                    parsed = self._validate_proxy_url(parsed)
        except Exception as exc:
            yield event.plain_result(f"❌ 参数值无效：{exc}")
            return

        self.config[k] = parsed
        save_fn = getattr(self.config, "save_config", None)
        if callable(save_fn):
            try:
                await asyncio.to_thread(save_fn)
            except Exception as exc:
                self.logger.warning("[SteamMonitor] 保存配置失败: %s", exc)

        shown = _mask_secret(parsed) if k in SECRET_CONFIG_KEYS else parsed
        yield event.plain_result(f"✅ 已更新 {k} = {shown}（已立即生效）。")

    async def cmd_rs(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        if self.store is None:
            yield event.plain_result("Steam 状态监控引擎尚未就绪。")
            return
        await asyncio.to_thread(self.store.reset_runtime_state)
        await self._reset_achievement_service()
        self._baseline_synced = False
        self._last_poll_ts = 0.0
        yield event.plain_result("✅ 已重置 Steam 监控运行状态（活跃会话与缓存已清理，下一轮将静默重新同步基线）。")

    async def cmd_clear_cache(self, event: Any) -> AsyncIterator[Any]:
        if not self._is_admin(event):
            yield event.plain_result("❌ 权限不足，该指令仅限 Bot 管理员使用。")
            return
        cleared_files: dict[str, int] = {}
        if self.steam_client is not None:
            cleared_files = await asyncio.to_thread(self.steam_client.clear_caches)
        cleared_games = 0
        if self.store is not None:
            cleared_games = await asyncio.to_thread(self.store.clear_game_cache)
        parts = [f"{k}={v}" for k, v in cleared_files.items()]
        parts.append(f"game_cache={cleared_games}")
        yield event.plain_result(f"✅ 已清理 Steam 缓存（{', '.join(parts)}）。")
