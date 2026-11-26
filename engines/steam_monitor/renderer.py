"""Steam Monitor HTML/CSS card renderer with plain-text graceful fallback."""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from astrbot.api import logger

from .steam_client import sanitize_avatar_frame_url, sanitize_render_image_url

STEAM_CARD_TEMP_PREFIX = "steam_card_"
STEAM_CARD_TEMP_SUFFIX = ".png"
DEFAULT_STALE_TEMP_MAX_AGE_SECONDS = 3600.0

_DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_STEAM_HEADER_CDN = (
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
)
_RANK_PALETTE = [
    "#66c0f4",
    "#90ba3c",
    "#f5a623",
    "#b388ff",
    "#ff6b8b",
    "#26c6da",
    "#ffd54f",
    "#7cb342",
]
_PERSONA_STATE_TEXT = {
    0: "离线",
    1: "在线",
    2: "忙碌",
    3: "离开",
    4: "打盹",
    5: "想交易",
    6: "想玩游戏",
}
_REGION_LABELS = {
    "CN": "国区 (CN)",
    "US": "美区 (US)",
    "JP": "日区 (JP)",
    "UA": "乌区 (UA)",
    "HK": "港区 (HK)",
    "TW": "台区 (TW)",
    "TR": "土区 (TR)",
    "AR": "阿区 (AR)",
    "RU": "俄区 (RU)",
    "GB": "英区 (GB)",
    "EU": "欧区 (EU)",
    "KR": "韩区 (KR)",
}


def steam_header_url(appid: str | int | None) -> str:
    """Return official Steam CDN header cover URL for an appid, or empty string."""
    aid = str(appid or "").strip()
    if not aid or not aid.isdigit():
        return ""
    return _STEAM_HEADER_CDN.format(appid=aid)


def _initial_char(name: str) -> str:
    text = (name or "").strip()
    return text[0].upper() if text else "S"


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _to_int(val: Any, default: int | None = None) -> int | None:
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _format_duration_cn(
    *,
    seconds: float | None = None,
    minutes: float | None = None,
    hours: float | None = None,
    short: bool = False,
) -> str:
    if seconds is None:
        if minutes is not None:
            seconds = float(minutes) * 60.0
        elif hours is not None:
            seconds = float(hours) * 3600.0
        else:
            seconds = 0.0
    total_sec = max(0.0, float(seconds))
    total_min = total_sec / 60.0
    if total_sec < 60.0:
        if total_sec <= 0:
            return "0分钟" if not short else "0m"
        return "<1分钟" if not short else "<1m"
    if total_min < 60.0:
        m = max(1, round(total_min))
        return f"{m}分钟" if not short else f"{m}m"
    hrs = int(total_min // 60)
    rem_min = round(total_min - hrs * 60)
    if rem_min == 60:
        hrs += 1
        rem_min = 0
    if short:
        return f"{hrs}h {rem_min}m" if rem_min > 0 else f"{hrs}h"
    return f"{hrs}小时{rem_min}分" if rem_min > 0 else f"{hrs}小时"


def _format_hours_compact(hours: float) -> str:
    h = max(0.0, float(hours))
    if h >= 100:
        return f"{h:.0f} 小时"
    if h >= 10:
        return f"{h:.1f} 小时"
    if h >= 1:
        return f"{h:.1f} 小时"
    mins = round(h * 60)
    if mins <= 0 and h > 0:
        return "<1 分钟"
    return f"{mins} 分钟"


def _format_timestamp(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, (int, float)):
        if val <= 0:
            return ""
        try:
            return datetime.fromtimestamp(float(val)).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return ""
    return str(val).strip()


def _format_last_logoff(val: Any) -> str:
    if val is None or val == "":
        return ""
    if isinstance(val, str):
        return val.strip()
    try:
        ts = float(val)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    diff = max(0.0, time.time() - ts)
    if diff < 60:
        return "刚刚离线"
    if diff < 3600:
        return f"{int(diff // 60)} 分钟前离线"
    if diff < 86400:
        return f"{int(diff // 3600)} 小时前离线"
    if diff < 86400 * 30:
        return f"{int(diff // 86400)} 天前离线"
    return f"上次在线 {datetime.fromtimestamp(ts).strftime('%Y-%m-%d')}"


def _default_end_tip(duration_min: float) -> str:
    if duration_min < 15:
        return "刚热身就下机了？一定是突然有事！"
    if duration_min < 60:
        return "轻松小玩了一把，劳逸结合刚刚好。"
    if duration_min < 180:
        return "酣畅淋漓的一局，记得起来喝口水活动一下！"
    if duration_min < 360:
        return "高强度爆肝完毕，眼睛和肩膀都要好好休息哦。"
    return "史诗级马拉松会话！建议立刻补水伸懒腰！"


def _review_badge_class(summary: str, percent: float | None) -> str:
    s = summary or ""
    if any(k in s for k in ("差评", "Negative")):
        return "badge-red"
    if any(k in s for k in ("褒贬不一", "Mixed")):
        return "badge-amber"
    if percent is not None:
        if percent < 40:
            return "badge-red"
        if percent < 70:
            return "badge-amber"
    return "badge-blue"


class SteamCardRenderer:
    """Renders Steam Monitor dark-theme cards via Jinja2 + AstrBot html_render."""

    def __init__(
        self,
        html_render_fn: Callable | None = None,
        template_dir: Path | None = None,
    ) -> None:
        self._html_render_fn = html_render_fn
        self._template_dir = Path(template_dir) if template_dir else _DEFAULT_TEMPLATE_DIR
        self._env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render_html(self, template_name: str, context: dict[str, Any]) -> str:
        template = self._env.get_template(template_name)
        return template.render(**context)

    @staticmethod
    def _write_temp_png(data: bytes, temp_dir: Path | None = None) -> str:
        kwargs: dict[str, Any] = {
            "prefix": STEAM_CARD_TEMP_PREFIX,
            "suffix": STEAM_CARD_TEMP_SUFFIX,
            "delete": False,
        }
        if temp_dir is not None:
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            kwargs["dir"] = str(temp_dir)
        with tempfile.NamedTemporaryFile(**kwargs) as tmp:
            tmp.write(data)
            return tmp.name

    @staticmethod
    def is_temp_card_path(path: str | Path | None, *, temp_dir: Path | None = None) -> bool:
        """Return True iff `path` is a `steam_card_*.png` file inside the temp directory."""
        if not path:
            return False
        raw = str(path).strip()
        if not raw or raw.startswith(("http://", "https://", "base64://")):
            return False
        try:
            candidate = Path(raw).resolve()
            base_dir = Path(temp_dir).resolve() if temp_dir is not None else Path(tempfile.gettempdir()).resolve()
        except Exception:
            return False
        name = candidate.name
        if not (name.startswith(STEAM_CARD_TEMP_PREFIX) and name.endswith(STEAM_CARD_TEMP_SUFFIX)):
            return False
        try:
            candidate.relative_to(base_dir)
            return True
        except ValueError:
            return False

    @classmethod
    def cleanup_temp_file(cls, path: str | Path | None, *, temp_dir: Path | None = None) -> bool:
        """Delete a rendered `steam_card_*.png` temp file safely; returns True if deleted."""
        if not cls.is_temp_card_path(path, temp_dir=temp_dir):
            return False
        try:
            target = Path(str(path))
            if target.exists():
                target.unlink(missing_ok=True)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to delete temp steam card %s: %s", path, exc)
        return False

    @classmethod
    def cleanup_stale_temp_files(
        cls,
        *,
        max_age_seconds: float = DEFAULT_STALE_TEMP_MAX_AGE_SECONDS,
        temp_dir: Path | None = None,
        now: float | None = None,
    ) -> int:
        """Remove stale `steam_card_*.png` files from the temp directory (called on startup)."""
        base_dir = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
        if not base_dir.is_dir():
            return 0
        now_ts = float(now if now is not None else time.time())
        max_age = max(0.0, float(max_age_seconds))
        removed = 0
        pattern = f"{STEAM_CARD_TEMP_PREFIX}*{STEAM_CARD_TEMP_SUFFIX}"
        try:
            for entry in base_dir.glob(pattern):
                if not entry.is_file():
                    continue
                try:
                    mtime = os.path.getmtime(entry)
                except OSError:
                    continue
                if max_age <= 0.0 or (now_ts - mtime) >= max_age:
                    try:
                        entry.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to sweep stale steam card temp files: %s", exc)
        return removed

    async def _html_to_image(self, html: str, *, width: int = 760) -> str | None:
        if self._html_render_fn is None:
            return None

        # Playwright screenshot options compatible with AstrBot's html_render
        opts: dict[str, Any] = {
            "type": "png",
            "full_page": True,
            "scale": "device",
            "timeout": 30000,
        }
        _ = width  # Card width is enforced cleanly in HTML/CSS (.steam-card width)

        try:
            try:
                result = self._html_render_fn(html, {}, options=opts)
                if inspect.isawaitable(result):
                    result = await result
            except TypeError:
                result = self._html_render_fn(html, {}, False, opts)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Steam card html_render failed, falling back to text: %s", exc)
            return None

        if result is None:
            return None
        if isinstance(result, str):
            ref = result.strip()
            return ref if ref else None
        if isinstance(result, (bytes, bytearray)):
            try:
                return await asyncio.to_thread(self._write_temp_png, bytes(result))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist rendered PNG bytes: %s", exc)
                return None

        logger.warning("Unexpected html_render return type: %s", type(result).__name__)
        return None

    # ------------------------------------------------------------------
    # Context normalization & plain-text fallback builders
    # ------------------------------------------------------------------

    def _prepare_game_start(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        player_name = str(data.get("player_name") or data.get("name") or "未知玩家")
        appid = str(data.get("appid") or data.get("game_id") or data.get("gameid") or "").strip()
        game_name = str(
            data.get("game_name") or data.get("game_name_zh") or data.get("game_name_en") or "未知游戏"
        )
        game_name_en = str(data.get("game_name_en") or "").strip()
        cover_url = str(data.get("cover_url") or data.get("header_url") or "").strip()
        if not cover_url and appid:
            cover_url = steam_header_url(appid)

        online_count = _to_int(data.get("online_count"))
        online_count_text = (
            f"{online_count:,} 人当前在线" if online_count is not None and online_count >= 0 else ""
        )
        timestamp = _format_timestamp(data.get("timestamp") or data.get("started_at"))

        ctx = {
            "page_title": f"{player_name} 正在游玩 {game_name}",
            "card_width": 680,
            "player_name": player_name,
            "player_initial": _initial_char(player_name),
            "avatar_url": sanitize_render_image_url(data.get("avatar_url")),
            "avatar_frame_url": sanitize_avatar_frame_url(data.get("avatar_frame_url")),
            "game_name": game_name,
            "game_name_en": game_name_en,
            "appid": appid,
            "cover_url": sanitize_render_image_url(cover_url),
            "online_count": online_count,
            "online_count_text": online_count_text,
            "timestamp": timestamp,
            "steam_id": str(data.get("steam_id") or data.get("steamid") or "").strip(),
            "group_name": str(data.get("group_name") or "").strip(),
        }

        title_part = f"《{game_name}》"
        if game_name_en and game_name_en != game_name:
            title_part += f" ({game_name_en})"
        lines = [f"🎮 {player_name} 正在游玩 {title_part}"]
        meta_bits: list[str] = []
        if online_count_text:
            meta_bits.append(online_count_text)
        if timestamp:
            meta_bits.append(timestamp)
        if meta_bits:
            lines.append(" | ".join(meta_bits))
        return ctx, "\n".join(lines)

    def _prepare_game_end(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        player_name = str(data.get("player_name") or data.get("name") or "未知玩家")
        appid = str(data.get("appid") or data.get("game_id") or data.get("gameid") or "").strip()
        game_name = str(
            data.get("game_name") or data.get("game_name_zh") or data.get("game_name_en") or "未知游戏"
        )
        game_name_en = str(data.get("game_name_en") or "").strip()
        cover_url = str(data.get("cover_url") or data.get("header_url") or "").strip()
        if not cover_url and appid:
            cover_url = steam_header_url(appid)

        duration_sec = data.get("duration_sec")
        duration_min = data.get("duration_min")
        if duration_sec is not None:
            dur_min_val = _to_float(duration_sec) / 60.0
        elif duration_min is not None:
            dur_min_val = _to_float(duration_min)
        else:
            dur_min_val = 0.0

        duration_text = str(data.get("duration_text") or "").strip()
        if not duration_text:
            duration_text = _format_duration_cn(
                seconds=_to_float(duration_sec) if duration_sec is not None else None,
                minutes=_to_float(duration_min) if duration_min is not None else None,
            )

        timestamp = _format_timestamp(data.get("ended_at") or data.get("timestamp"))
        tip = str(data.get("tip") or "").strip() or _default_end_tip(dur_min_val)

        ctx = {
            "page_title": f"{player_name} 结束了游玩 {game_name}",
            "card_width": 680,
            "player_name": player_name,
            "player_initial": _initial_char(player_name),
            "avatar_url": sanitize_render_image_url(data.get("avatar_url")),
            "avatar_frame_url": sanitize_avatar_frame_url(data.get("avatar_frame_url")),
            "game_name": game_name,
            "game_name_en": game_name_en,
            "appid": appid,
            "cover_url": sanitize_render_image_url(cover_url),
            "duration_text": duration_text,
            "timestamp": timestamp,
            "tip": tip,
            "group_name": str(data.get("group_name") or "").strip(),
        }

        lines = [f"🏁 {player_name} 结束了《{game_name}》的游玩"]
        meta = f"⏱ 本次时长：{duration_text}"
        if timestamp:
            meta += f" | 结束时间：{timestamp}"
        lines.append(meta)
        if tip:
            lines.append(f"💬 {tip}")
        return ctx, "\n".join(lines)

    def _prepare_achievement(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        player_name = str(data.get("player_name") or data.get("name") or "未知玩家")
        appid = str(data.get("appid") or data.get("game_id") or data.get("gameid") or "").strip()
        game_name = str(data.get("game_name") or "未知游戏")
        cover_url = str(data.get("cover_url") or data.get("header_url") or "").strip()
        if not cover_url and appid:
            cover_url = steam_header_url(appid)
        timestamp = _format_timestamp(data.get("timestamp"))

        raw_achs = data.get("achievements") or []
        norm_achs: list[dict[str, Any]] = []
        for item in raw_achs:
            if not isinstance(item, dict):
                continue
            display_name = str(
                item.get("display_name") or item.get("name") or item.get("apiname") or "未知成就"
            )
            description = str(item.get("description") or "").strip()
            icon = sanitize_render_image_url(item.get("icon") or item.get("icon_url"))
            raw_pct = item.get("percent")
            if raw_pct is None:
                raw_pct = item.get("global_percent")
            pct = _to_float(raw_pct, -1.0) if raw_pct is not None else None
            if pct is not None and pct < 0:
                pct = None
            is_rare = pct is not None and pct < 10.0
            pct_text = f"{pct:.1f}%" if pct is not None else ""
            norm_achs.append(
                {
                    "display_name": display_name,
                    "description": description,
                    "icon": icon,
                    "percent": pct,
                    "percent_text": pct_text,
                    "is_rare": is_rare,
                }
            )

        ctx = {
            "page_title": f"{player_name} 解锁了 {game_name} 成就",
            "card_width": 720,
            "player_name": player_name,
            "player_initial": _initial_char(player_name),
            "avatar_url": sanitize_render_image_url(data.get("avatar_url")),
            "avatar_frame_url": sanitize_avatar_frame_url(data.get("avatar_frame_url")),
            "game_name": game_name,
            "appid": appid,
            "cover_url": sanitize_render_image_url(cover_url),
            "timestamp": timestamp,
            "achievements": norm_achs,
        }

        lines = [f"🏆 {player_name} 在《{game_name}》解锁了 {len(norm_achs)} 项新成就："]
        for ach in norm_achs:
            rare_tag = f" [★稀有 {ach['percent_text']}]" if ach["is_rare"] else (
                f" [全球 {ach['percent_text']}]" if ach["percent_text"] else ""
            )
            line = f"• {ach['display_name']}{rare_tag}"
            if ach["description"]:
                line += f" — {ach['description']}"
            lines.append(line)
        return ctx, "\n".join(lines)

    def _prepare_player_list(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        raw_players = data.get("players")
        if raw_players is None and "player" in data and isinstance(data["player"], dict):
            raw_players = [data["player"]]
        raw_players = raw_players or []

        is_single = bool(data.get("is_single", False))
        if not is_single and data.get("mode") == "single" and len(raw_players) == 1:
            is_single = True

        ingame_players: list[dict[str, Any]] = []
        online_players: list[dict[str, Any]] = []
        offline_players: list[dict[str, Any]] = []

        for item in raw_players:
            if not isinstance(item, dict):
                continue
            pname = str(item.get("player_name") or item.get("name") or item.get("steam_id") or "未知玩家")
            sid = str(item.get("steam_id") or item.get("steamid") or "").strip()
            game_name = str(item.get("game_name") or item.get("gameextrainfo") or "").strip()
            game_id = str(item.get("game_id") or item.get("appid") or item.get("gameid") or "").strip()
            cover_url = str(item.get("cover_url") or "").strip()
            if not cover_url and game_id:
                cover_url = steam_header_url(game_id)

            raw_status = str(item.get("status") or "").strip().lower()
            pstate = _to_int(item.get("personastate"))

            if game_name or raw_status in ("in_game", "ingame", "playing"):
                bucket = "ingame"
                dot_class = "ingame"
                default_status_text = "游戏中"
            elif raw_status in ("online", "away", "busy", "snooze", "looking_to_trade", "looking_to_play"):
                bucket = "online"
                dot_class = "away" if raw_status in ("away", "busy", "snooze") else "online"
                default_status_text = {
                    "online": "在线",
                    "away": "离开",
                    "busy": "忙碌",
                    "snooze": "打盹",
                    "looking_to_trade": "想交易",
                    "looking_to_play": "想玩游戏",
                }.get(raw_status, "在线")
            elif raw_status == "offline":
                bucket = "offline"
                dot_class = "offline"
                default_status_text = "离线"
            elif pstate is not None and pstate > 0:
                bucket = "online"
                dot_class = "away" if pstate in (2, 3, 4) else "online"
                default_status_text = _PERSONA_STATE_TEXT.get(pstate, "在线")
            else:
                bucket = "offline"
                dot_class = "offline"
                default_status_text = "离线"

            status_text = str(item.get("status_text") or "").strip() or default_status_text

            dur_text = str(item.get("duration_text") or "").strip()
            if not dur_text and bucket == "ingame":
                elapsed = item.get("duration_sec")
                if elapsed is None:
                    elapsed = item.get("elapsed_sec")
                if elapsed is not None:
                    dur_text = _format_duration_cn(seconds=_to_float(elapsed), short=False)

            last_logoff_text = str(item.get("last_logoff_text") or "").strip()
            if not last_logoff_text and item.get("last_logoff"):
                last_logoff_text = _format_last_logoff(item.get("last_logoff"))
            if not last_logoff_text and item.get("lastlogoff"):
                last_logoff_text = _format_last_logoff(item.get("lastlogoff"))

            norm = {
                "player_name": pname,
                "initial": _initial_char(pname),
                "steam_id": sid,
                "avatar_url": sanitize_render_image_url(item.get("avatar_url") or item.get("avatarfull")),
                "avatar_frame_url": sanitize_avatar_frame_url(item.get("avatar_frame_url")),
                "bucket": bucket,
                "dot_class": dot_class,
                "status_text": status_text,
                "game_name": game_name or ("未知游戏" if bucket == "ingame" else ""),
                "game_id": game_id,
                "cover_url": sanitize_render_image_url(cover_url),
                "duration_text": dur_text,
                "last_logoff_text": last_logoff_text,
                "group_label": str(item.get("group_label") or "").strip(),
            }
            if bucket == "ingame":
                ingame_players.append(norm)
            elif bucket == "online":
                online_players.append(norm)
            else:
                offline_players.append(norm)

        all_players = ingame_players + online_players + offline_players
        default_title = "Steam 玩家状态" if not is_single else "Steam 玩家实时状态"
        title = str(data.get("title") or default_title)
        group_label = str(data.get("group_label") or "").strip()
        subtitle = str(data.get("subtitle") or "").strip()
        timestamp = _format_timestamp(data.get("updated_at") or data.get("timestamp"))

        ctx = {
            "page_title": title,
            "card_width": 680 if is_single else 760,
            "title": title,
            "subtitle": subtitle,
            "group_label": group_label,
            "is_single": is_single,
            "timestamp": timestamp,
            "ingame_players": ingame_players,
            "online_players": online_players,
            "offline_players": offline_players,
            "all_players": all_players,
        }

        header = f"📋 {title}" + (f" [{group_label}]" if group_label else "")
        lines = [header]
        if not all_players:
            lines.append("当前没有监控中的 Steam 玩家。")
        else:
            if ingame_players:
                lines.append(f"🟢 正在游玩 ({len(ingame_players)})：")
                for p in ingame_players:
                    dur = f" ({p['duration_text']})" if p["duration_text"] else ""
                    grp = f" [{p['group_label']}]" if p["group_label"] else ""
                    lines.append(f"  • {p['player_name']}{grp} — 《{p['game_name']}》{dur}")
            if online_players:
                lines.append(f"🔵 在线/离开 ({len(online_players)})：")
                for p in online_players:
                    grp = f" [{p['group_label']}]" if p["group_label"] else ""
                    lines.append(f"  • {p['player_name']}{grp} ({p['status_text']})")
            if offline_players:
                lines.append(f"⚪ 离线 ({len(offline_players)})：")
                for p in offline_players:
                    grp = f" [{p['group_label']}]" if p["group_label"] else ""
                    lo = f" · {p['last_logoff_text']}" if p["last_logoff_text"] else ""
                    lines.append(f"  • {p['player_name']}{grp}{lo}")
        return ctx, "\n".join(lines)

    def _prepare_rank(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        title = str(data.get("title") or "游戏时长排行榜")
        date_label = str(data.get("date_label") or "").strip()
        if not date_label:
            start_d = str(data.get("start_date") or "").strip()
            end_d = str(data.get("end_date") or "").strip()
            if start_d and end_d and start_d != end_d:
                date_label = f"{start_d} ~ {end_d}"
            elif start_d:
                date_label = start_d
            elif end_d:
                date_label = end_d

        group_label = str(data.get("group_label") or "").strip()
        timestamp = _format_timestamp(data.get("generated_at") or data.get("timestamp"))

        raw_entries = data.get("entries")
        if raw_entries is None:
            raw_entries = data.get("rankings")
        if raw_entries is None:
            raw_entries = data.get("players")
        raw_entries = raw_entries or []

        parsed_entries: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_entries, start=1):
            if not isinstance(item, dict):
                continue
            pname = str(
                item.get("player_name") or item.get("name") or item.get("steam_id") or item.get("sid") or f"玩家{idx}"
            )
            raw_games = item.get("games") or []
            norm_games: list[dict[str, Any]] = []
            for g in raw_games:
                if not isinstance(g, dict):
                    continue
                g_sec = g.get("seconds")
                if g_sec is None:
                    if g.get("minutes") is not None:
                        g_sec = _to_float(g.get("minutes")) * 60.0
                    elif g.get("hours") is not None:
                        g_sec = _to_float(g.get("hours")) * 3600.0
                    else:
                        g_sec = 0.0
                g_sec_f = max(0.0, _to_float(g_sec))
                g_id = str(g.get("gameid") or g.get("game_id") or g.get("appid") or "").strip()
                g_name = str(g.get("name") or g.get("game_name") or (f"App {g_id}" if g_id else "未知游戏"))
                norm_games.append(
                    {
                        "gameid": g_id,
                        "name": g_name,
                        "seconds": g_sec_f,
                        "duration_text": _format_duration_cn(seconds=g_sec_f, short=True),
                    }
                )
            norm_games.sort(key=lambda x: x["seconds"], reverse=True)

            if item.get("total_seconds") is not None:
                total_sec = max(0.0, _to_float(item.get("total_seconds")))
            elif item.get("total_minutes") is not None:
                total_sec = max(0.0, _to_float(item.get("total_minutes")) * 60.0)
            elif item.get("total_hours") is not None:
                total_sec = max(0.0, _to_float(item.get("total_hours")) * 3600.0)
            else:
                total_sec = sum(g["seconds"] for g in norm_games)

            top_game_id = str(item.get("top_game_id") or item.get("top_appid") or "").strip()
            top_game_name = str(item.get("top_game_name") or "").strip()
            if norm_games:
                if not top_game_id:
                    top_game_id = norm_games[0]["gameid"]
                if not top_game_name:
                    top_game_name = norm_games[0]["name"]

            top_cover_url = sanitize_render_image_url(
                item.get("top_cover_url") or item.get("cover_url")
            )
            if not top_cover_url and top_game_id:
                top_cover_url = steam_header_url(top_game_id)

            rank_num = _to_int(item.get("rank"), idx) or idx
            parsed_entries.append(
                {
                    "rank": rank_num,
                    "player_name": pname,
                    "initial": _initial_char(pname),
                    "steam_id": str(item.get("steam_id") or item.get("sid") or "").strip(),
                    "avatar_url": sanitize_render_image_url(item.get("avatar_url")),
                    "total_seconds": total_sec,
                    "total_hours": total_sec / 3600.0,
                    "total_duration_text": _format_duration_cn(seconds=total_sec),
                    "top_game_id": top_game_id,
                    "top_game_name": top_game_name,
                    "top_cover_url": top_cover_url,
                    "games": norm_games,
                }
            )

        max_seconds = max((e["total_seconds"] for e in parsed_entries), default=0.0)

        for entry in parsed_entries:
            total_sec = entry["total_seconds"]
            ratio_pct = (total_sec / max_seconds * 100.0) if max_seconds > 0 else 0.0
            ratio_pct = max(0.0, min(100.0, ratio_pct))
            entry["ratio_pct"] = ratio_pct
            entry["ratio_pct_text"] = f"{round(ratio_pct)}%"

            games = entry["games"]
            games_sum = sum(g["seconds"] for g in games)
            denom = games_sum if games_sum > 0 else total_sec

            segments: list[dict[str, Any]] = []
            for g_idx, g in enumerate(games):
                color = _RANK_PALETTE[g_idx % len(_RANK_PALETTE)]
                share = (g["seconds"] / denom) if denom > 0 else 0.0
                share = max(0.0, min(1.0, share))
                g["color"] = color
                g["share_pct_text"] = f"{round(share * 100)}%"
                bar_w = max(0.0, ratio_pct * share)
                if bar_w > 0:
                    segments.append({"color": color, "bar_width_pct": bar_w})

            if not segments and ratio_pct > 0:
                segments.append({"color": _RANK_PALETTE[0], "bar_width_pct": ratio_pct})

            # Clamp total segment widths strictly <= 100.0% to guarantee zero overflow
            seg_total = sum(s["bar_width_pct"] for s in segments)
            if seg_total > 100.0 and seg_total > 0:
                scale = 100.0 / seg_total
                for s in segments:
                    s["bar_width_pct"] = round(s["bar_width_pct"] * scale, 2)
            else:
                for s in segments:
                    s["bar_width_pct"] = round(s["bar_width_pct"], 2)

            entry["segments"] = segments

        player_count = _to_int(data.get("player_count"))
        if player_count is None:
            player_count = len(parsed_entries)

        if data.get("total_hours") is not None:
            total_hours = max(0.0, _to_float(data.get("total_hours")))
        elif data.get("total_seconds") is not None:
            total_hours = max(0.0, _to_float(data.get("total_seconds")) / 3600.0)
        else:
            total_hours = sum(e["total_seconds"] for e in parsed_entries) / 3600.0

        total_hours_text = _format_hours_compact(total_hours)

        ctx = {
            "page_title": f"{title} ({date_label})" if date_label else title,
            "card_width": 800,
            "title": title,
            "date_label": date_label or "统计周期未知",
            "group_label": group_label,
            "player_count": player_count,
            "total_hours": total_hours,
            "total_hours_text": total_hours_text,
            "entries": parsed_entries,
            "timestamp": timestamp,
        }

        head = f"🏆 {title}"
        if date_label:
            head += f" ({date_label})"
        if group_label:
            head += f" [{group_label}]"
        lines = [head, f"共 {player_count} 人上榜 · 累计 {total_hours_text}"]
        if not parsed_entries:
            lines.append("该周期内暂无游玩记录。")
        else:
            for e in parsed_entries:
                top_str = f" | 主玩: {e['top_game_name']}" if e["top_game_name"] else ""
                lines.append(
                    f"#{e['rank']} {e['player_name']} — {e['total_duration_text']} ({e['ratio_pct_text']}){top_str}"
                )
        return ctx, "\n".join(lines)

    def _prepare_game_detail(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        appid = str(data.get("appid") or data.get("game_id") or "").strip()
        name = str(data.get("name") or data.get("game_name") or (f"App {appid}" if appid else "未知游戏"))
        name_en = str(data.get("name_en") or "").strip()
        header_url = sanitize_render_image_url(
            data.get("header_url") or data.get("header_image") or data.get("cover_url")
        )
        if not header_url and appid:
            header_url = steam_header_url(appid)

        developers = data.get("developers") or []
        if isinstance(developers, str):
            developers_text = developers
        else:
            developers_text = " / ".join(str(x) for x in developers if x)

        publishers = data.get("publishers") or []
        if isinstance(publishers, str):
            publishers_text = publishers
        else:
            publishers_text = " / ".join(str(x) for x in publishers if x)

        review_all = data.get("review_all") if isinstance(data.get("review_all"), dict) else {}
        review_zh = data.get("review_schinese") if isinstance(data.get("review_schinese"), dict) else {}
        review_src = review_all if review_all.get("text") else review_zh
        review_summary = str(data.get("review_summary") or review_src.get("text") or "").strip()
        review_percent = _to_int(
            data.get("review_percent")
            if data.get("review_percent") is not None
            else review_src.get("percent")
        )
        review_count = _to_int(
            data.get("review_count")
            if data.get("review_count") is not None
            else review_src.get("total")
        )
        review_count_text = f"{review_count:,}" if review_count is not None and review_count > 0 else ""
        review_badge_class = _review_badge_class(review_summary, review_percent)

        raw_tags = data.get("tags") or data.get("genres") or []
        tags = [str(t) for t in raw_tags if t][:12]

        price_obj = data.get("price") if isinstance(data.get("price"), dict) else {}
        is_free = bool(data.get("is_free", price_obj.get("is_free", False)))
        discount_percent = (
            _to_int(
                data.get("discount_percent")
                if data.get("discount_percent") is not None
                else price_obj.get("cut"),
                0,
            )
            or 0
        )
        price_formatted = str(
            data.get("price_formatted")
            or data.get("final_price_formatted")
            or price_obj.get("formatted")
            or ""
        ).strip()
        if not price_formatted and data.get("region_locked_note"):
            price_formatted = str(data.get("region_locked_note")).strip()
        original_price_formatted = str(
            data.get("original_price_formatted")
            or data.get("initial_price_formatted")
            or price_obj.get("regular_formatted")
            or ""
        ).strip()
        region = str(
            data.get("region") or price_obj.get("region") or data.get("primary_region") or ""
        ).strip().upper()

        raw_itad = data.get("itad")
        itad: dict[str, Any] | None = None
        if isinstance(raw_itad, dict):
            hist_price = str(
                raw_itad.get("historical_low_price")
                or raw_itad.get("history_low_formatted")
                or raw_itad.get("steam_low_formatted")
                or raw_itad.get("current_low_price")
                or raw_itad.get("current_price_formatted")
                or ""
            ).strip()
            if hist_price:
                hist_cut = _to_int(
                    raw_itad.get("historical_low_cut")
                    if raw_itad.get("historical_low_cut") is not None
                    else (
                        raw_itad.get("history_low_cut")
                        if raw_itad.get("history_low_cut") is not None
                        else raw_itad.get("steam_low_cut")
                    )
                )
                curr_low_price = str(
                    raw_itad.get("current_low_price")
                    or raw_itad.get("cdk_formatted")
                    or raw_itad.get("current_price_formatted")
                    or ""
                ).strip()
                curr_low_shop = str(
                    raw_itad.get("current_low_shop") or raw_itad.get("cdk_shop") or ""
                ).strip()
                is_hist_low = bool(raw_itad.get("is_historical_low", False))
                if (
                    not is_hist_low
                    and raw_itad.get("current_price") is not None
                    and raw_itad.get("history_low") is not None
                ):
                    try:
                        is_hist_low = float(raw_itad["current_price"]) <= float(raw_itad["history_low"]) + 1e-6
                    except (TypeError, ValueError):
                        is_hist_low = False
                itad = {
                    "historical_low_price": hist_price,
                    "historical_low_cut": hist_cut,
                    "historical_low_date": str(raw_itad.get("historical_low_date") or "").strip(),
                    "current_low_price": curr_low_price,
                    "current_low_shop": curr_low_shop,
                    "is_historical_low": is_hist_low,
                    "url": str(raw_itad.get("url") or raw_itad.get("deal_url") or "").strip(),
                }

        raw_regions = data.get("region_prices") or []
        region_prices: list[dict[str, Any]] = []
        numeric_prices: list[tuple[int, float]] = []
        has_explicit_lowest = False
        for rp in raw_regions:
            if not isinstance(rp, dict):
                continue
            code = str(rp.get("region") or rp.get("cc") or "").strip().upper()
            label = str(
                rp.get("region_label") or rp.get("label") or _REGION_LABELS.get(code, code or "未知地区")
            )
            pf = str(
                rp.get("price_formatted") or rp.get("original_formatted") or "-"
            ).strip()
            cny = str(
                rp.get("cny_price") or rp.get("converted_formatted") or rp.get("converted_price") or ""
            ).strip()
            # Avoid showing raw float if converted_formatted wasn't given
            if isinstance(rp.get("converted_price"), (int, float)) and not rp.get("cny_price") and not rp.get("converted_formatted"):
                cny = f"{float(rp['converted_price']):.2f}"
            disc = (
                _to_int(
                    rp.get("discount_percent")
                    if rp.get("discount_percent") is not None
                    else rp.get("cut"),
                    0,
                )
                or 0
            )
            is_low = bool(rp.get("is_lowest", False))
            if is_low:
                has_explicit_lowest = True
            idx_pos = len(region_prices)
            if isinstance(rp.get("converted_price"), (int, float)) and not rp.get("is_free"):
                numeric_prices.append((idx_pos, float(rp["converted_price"])))
            region_prices.append(
                {
                    "region": code,
                    "region_label": label,
                    "price_formatted": pf,
                    "cny_price": cny,
                    "discount_percent": disc,
                    "is_lowest": is_low,
                }
            )
        if not has_explicit_lowest and len(numeric_prices) >= 2:
            min_price = min(p for _, p in numeric_prices)
            for idx_pos, val in numeric_prices:
                if abs(val - min_price) < 1e-6:
                    region_prices[idx_pos]["is_lowest"] = True
                    break

        store_url = str(data.get("store_url") or "").strip()
        if not store_url and appid:
            store_url = f"https://store.steampowered.com/app/{appid}/"

        ctx = {
            "page_title": f"{name} - Steam 商店详情",
            "card_width": 760,
            "appid": appid,
            "name": name,
            "name_en": name_en,
            "header_url": header_url,
            "release_date": str(data.get("release_date") or "").strip(),
            "developers_text": developers_text,
            "publishers_text": publishers_text,
            "review_summary": review_summary,
            "review_percent": review_percent,
            "review_count_text": review_count_text,
            "review_badge_class": review_badge_class,
            "tags": tags,
            "short_description": str(data.get("short_description") or "").strip(),
            "is_free": is_free,
            "discount_percent": discount_percent,
            "price_formatted": price_formatted,
            "original_price_formatted": original_price_formatted,
            "region": region,
            "itad": itad,
            "region_prices": region_prices,
            "store_url": store_url,
        }

        lines = [f"🛒 《{name}》" + (f" ({name_en})" if name_en and name_en != name else "")]
        if is_free:
            lines.append("💰 售价：免费开玩")
        elif price_formatted:
            disc = f" (-{discount_percent}%, 原价 {original_price_formatted})" if discount_percent > 0 and original_price_formatted else (
                f" (-{discount_percent}%)" if discount_percent > 0 else ""
            )
            lines.append(f"💰 售价：{price_formatted}{disc}")
        if review_summary:
            rev = f"⭐ 评测：{review_summary}"
            if review_percent is not None:
                rev += f" ({review_percent}%)"
            if review_count_text:
                rev += f" · {review_count_text} 篇"
            lines.append(rev)
        if itad:
            itad_line = f"📉 史低：{itad['historical_low_price']}"
            if itad["historical_low_cut"]:
                itad_line += f" (-{itad['historical_low_cut']}%)"
            if itad["is_historical_low"]:
                itad_line += " [平/新史低]"
            lines.append(itad_line)
        if region_prices:
            rp_str = " | ".join(
                f"{rp['region_label']}: {rp['price_formatted']}"
                + (f" ({rp['cny_price']})" if rp["cny_price"] else "")
                for rp in region_prices
            )
            lines.append(f"🌍 跨区比价：{rp_str}")
        if store_url:
            lines.append(f"🔗 {store_url}")
        return ctx, "\n".join(lines)

    def _prepare_openbox(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        steam_id = str(
            data.get("steam_id") or data.get("steamid") or summary.get("steamid") or ""
        ).strip()
        player_name = str(
            data.get("player_name")
            or data.get("persona_name")
            or data.get("name")
            or summary.get("personaname")
            or summary.get("name")
            or steam_id
            or "未知玩家"
        )
        avatar_url = sanitize_render_image_url(
            data.get("avatar_url") or summary.get("avatarfull") or summary.get("avatar")
        )
        avatar_frame_url = sanitize_avatar_frame_url(data.get("avatar_frame_url"))
        profile_url = str(data.get("profile_url") or summary.get("profileurl") or "").strip()
        steam_level = _to_int(data.get("steam_level"))

        account_age = str(data.get("account_age") or data.get("timecreated_text") or "").strip()
        if not account_age:
            tc = _to_int(data.get("timecreated") or summary.get("timecreated"))
            if tc and tc > 0:
                created_dt = datetime.fromtimestamp(tc)
                years = max(0.0, (time.time() - tc) / (86400.0 * 365.25))
                account_age = f"创建于 {created_dt.strftime('%Y-%m-%d')} ({years:.1f} 年)"

        current_game = str(
            data.get("current_game") or summary.get("gameextrainfo") or ""
        ).strip()
        pstate = _to_int(data.get("personastate") if data.get("personastate") is not None else summary.get("personastate"))
        if current_game:
            status_bucket = "ingame"
            status_badge_class = "badge-ingame"
            default_status = "游戏中"
        elif pstate is not None and pstate > 0:
            status_bucket = "away" if pstate in (2, 3, 4) else "online"
            status_badge_class = "badge-blue"
            default_status = _PERSONA_STATE_TEXT.get(pstate, "在线")
        else:
            status_bucket = "offline"
            status_badge_class = "badge-slate"
            default_status = "离线"
        status_text = str(data.get("status_text") or "").strip() or default_status

        visibility_text = str(data.get("visibility_text") or "").strip()
        if not visibility_text:
            vis = _to_int(
                data.get("communityvisibilitystate")
                if data.get("communityvisibilitystate") is not None
                else summary.get("communityvisibilitystate")
            )
            if vis == 3:
                visibility_text = "公开资料"
            elif vis == 1:
                visibility_text = "私密资料"
            elif vis == 2:
                visibility_text = "仅好友可见"

        raw_bans = data.get("bans") if isinstance(data.get("bans"), dict) else {}
        bans = {
            "vac_banned": bool(raw_bans.get("vac_banned", raw_bans.get("VACBanned", False))),
            "number_of_vac_bans": _to_int(
                raw_bans.get("number_of_vac_bans", raw_bans.get("NumberOfVACBans")), 0
            )
            or 0,
            "number_of_game_bans": _to_int(
                raw_bans.get("number_of_game_bans", raw_bans.get("NumberOfGameBans")), 0
            )
            or 0,
            "community_banned": bool(
                raw_bans.get("community_banned", raw_bans.get("CommunityBanned", False))
            ),
            "economy_ban": str(
                raw_bans.get("economy_ban", raw_bans.get("EconomyBan", "none")) or "none"
            ),
            "days_since_last_ban": _to_int(
                raw_bans.get("days_since_last_ban", raw_bans.get("DaysSinceLastBan")), 0
            )
            or 0,
        }

        owned_games_count = _to_int(data.get("owned_games_count"))
        owned_games_text = f"{owned_games_count:,} 款" if owned_games_count is not None else "已隐藏"

        total_hours = data.get("total_playtime_hours")
        total_playtime_text = (
            f"{_to_float(total_hours):,.1f} 小时" if total_hours is not None else "已隐藏"
        )

        recent_games: list[dict[str, Any]] = []
        for g in (data.get("recent_games") or [])[:8]:
            if not isinstance(g, dict):
                continue
            aid = str(g.get("appid") or g.get("game_id") or "").strip()
            gname = str(g.get("name") or (f"App {aid}" if aid else "未知游戏"))
            if g.get("playtime_2weeks_hours") is not None:
                r_hrs = _to_float(g.get("playtime_2weeks_hours"))
            else:
                r_hrs = _to_float(g.get("playtime_2weeks")) / 60.0
            if g.get("playtime_forever_hours") is not None:
                f_hrs = _to_float(g.get("playtime_forever_hours"))
            elif g.get("playtime_forever") is not None:
                f_hrs = _to_float(g.get("playtime_forever")) / 60.0
            else:
                f_hrs = None
            cover = sanitize_render_image_url(g.get("cover_url")) or steam_header_url(aid)
            recent_games.append(
                {
                    "appid": aid,
                    "name": gname,
                    "cover_url": cover,
                    "recent_hours_text": f"{r_hrs:.1f}h",
                    "forever_hours_text": f"{f_hrs:.1f}h" if f_hrs is not None else "",
                }
            )

        raw_top = data.get("top_games") or []
        top_parsed: list[dict[str, Any]] = []
        for g in raw_top[:10]:
            if not isinstance(g, dict):
                continue
            aid = str(g.get("appid") or g.get("game_id") or "").strip()
            gname = str(g.get("name") or (f"App {aid}" if aid else "未知游戏"))
            if g.get("playtime_forever_hours") is not None:
                f_hrs = _to_float(g.get("playtime_forever_hours"))
            elif g.get("playtime_hours") is not None:
                f_hrs = _to_float(g.get("playtime_hours"))
            elif g.get("playtime_minutes") is not None:
                f_hrs = _to_float(g.get("playtime_minutes")) / 60.0
            else:
                f_hrs = _to_float(g.get("playtime_forever")) / 60.0
            cover = sanitize_render_image_url(g.get("cover_url")) or steam_header_url(aid)
            top_parsed.append(
                {
                    "appid": aid,
                    "name": gname,
                    "cover_url": cover,
                    "forever_hours": max(0.0, f_hrs),
                    "forever_hours_text": f"{max(0.0, f_hrs):,.1f} 小时",
                }
            )
        max_top_hrs = max((g["forever_hours"] for g in top_parsed), default=0.0)
        for g in top_parsed:
            g["bar_pct"] = (
                round(min(100.0, max(2.0, g["forever_hours"] / max_top_hrs * 100.0)), 1)
                if max_top_hrs > 0
                else 0.0
            )

        privacy_note = str(data.get("privacy_note") or "").strip()

        ctx = {
            "page_title": f"{player_name} - Steam 玩家档案",
            "card_width": 760,
            "steam_id": steam_id,
            "player_name": player_name,
            "player_initial": _initial_char(player_name),
            "avatar_url": avatar_url,
            "avatar_frame_url": avatar_frame_url,
            "profile_url": profile_url,
            "steam_level": steam_level,
            "account_age": account_age,
            "current_game": current_game,
            "status_bucket": status_bucket,
            "status_badge_class": status_badge_class,
            "status_text": status_text,
            "visibility_text": visibility_text,
            "bans": bans,
            "owned_games_text": owned_games_text,
            "total_playtime_text": total_playtime_text,
            "recent_games": recent_games,
            "top_games": top_parsed,
            "privacy_note": privacy_note,
        }

        lines = [f"📦 Steam 玩家档案：{player_name} (SteamID: {steam_id})"]
        meta_bits = [f"状态: {status_text}"]
        if steam_level is not None:
            meta_bits.append(f"Lv.{steam_level}")
        if account_age:
            meta_bits.append(account_age)
        lines.append(" | ".join(meta_bits))
        vac_str = (
            f"VAC封禁×{bans['number_of_vac_bans']}"
            if bans["vac_banned"]
            else "VAC正常"
        )
        gb_str = (
            f"游戏封禁×{bans['number_of_game_bans']}"
            if bans["number_of_game_bans"] > 0
            else "无游戏封禁"
        )
        lines.append(f"🛡️ 信誉：{vac_str} / {gb_str}")
        lines.append(f"🎮 游戏库：{owned_games_text} · 总时长 {total_playtime_text}")
        if privacy_note:
            lines.append(f"⚠️ {privacy_note}")
        if recent_games:
            rec_str = "、".join(f"{g['name']}({g['recent_hours_text']})" for g in recent_games[:5])
            lines.append(f"🕹️ 近两周：{rec_str}")
        if top_parsed:
            top_str = "、".join(f"{g['name']}({g['forever_hours_text']})" for g in top_parsed[:5])
            lines.append(f"🏅 历史最久：{top_str}")
        return ctx, "\n".join(lines)

    # ------------------------------------------------------------------
    # Synchronous HTML helper for unit testing
    # ------------------------------------------------------------------

    def render_template_html(self, template_name: str, data: dict[str, Any]) -> str:
        """Synchronously render a named template with normalized context (for tests/inspection)."""
        preparers: dict[str, Callable[[dict[str, Any]], tuple[dict[str, Any], str]]] = {
            "game_start.html": self._prepare_game_start,
            "game_end.html": self._prepare_game_end,
            "achievement.html": self._prepare_achievement,
            "player_list.html": self._prepare_player_list,
            "rank.html": self._prepare_rank,
            "game_detail.html": self._prepare_game_detail,
            "openbox.html": self._prepare_openbox,
        }
        preparer = preparers.get(template_name)
        if preparer is not None:
            ctx, _ = preparer(data)
            return self._render_html(template_name, ctx)
        return self._render_html(template_name, dict(data))

    # ------------------------------------------------------------------
    # Public async card rendering methods -> (image_ref | None, plain_text)
    # ------------------------------------------------------------------

    async def _render_card(
        self,
        template_name: str,
        preparer: Callable[[dict[str, Any]], tuple[dict[str, Any], str]],
        data: dict[str, Any],
    ) -> tuple[str | None, str]:
        ctx, fallback_text = preparer(data)
        try:
            html = await asyncio.to_thread(self._render_html, template_name, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to render template %s: %s", template_name, exc)
            return None, fallback_text
        image_ref = await self._html_to_image(html, width=int(ctx.get("card_width", 760)))
        return image_ref, fallback_text

    async def render_game_start(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("game_start.html", self._prepare_game_start, data)

    async def render_game_end(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("game_end.html", self._prepare_game_end, data)

    async def render_achievement(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("achievement.html", self._prepare_achievement, data)

    async def render_player_list(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("player_list.html", self._prepare_player_list, data)

    async def render_rank(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("rank.html", self._prepare_rank, data)

    async def render_game_detail(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("game_detail.html", self._prepare_game_detail, data)

    async def render_openbox(self, data: dict[str, Any]) -> tuple[str | None, str]:
        return await self._render_card("openbox.html", self._prepare_openbox, data)
