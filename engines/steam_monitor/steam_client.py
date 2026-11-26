"""Async Steam Web API, Store, Community, SGDB, and ITAD client."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import re
import ssl
import time
from collections import deque
from collections.abc import Callable
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from astrbot.api import logger

STEAM_ID64_OFFSET = 76561197960265728

MAX_GAME_NAME_CACHE_SIZE = 1024
MAX_COVER_CACHE_SIZE = 1024
MAX_AVATAR_FRAME_CACHE_SIZE = 1024
MAX_ACHIEVEMENT_SCHEMA_CACHE_SIZE = 256
MAX_NO_ACHIEVEMENT_APPS = 1024
MAX_ACHIEVEMENT_RETRY_CACHE = 512

DEFAULT_STEAM_API_BASE = "https://api.steampowered.com"
DEFAULT_STEAM_STORE_BASE = "https://store.steampowered.com"
DEFAULT_SGDB_API_BASE = "https://www.steamgriddb.com/api/v2"
DEFAULT_ITAD_API_BASE = "https://api.isthereanydeal.com"

ALLOWED_API_BASE_DOMAINS: dict[str, tuple[str, ...]] = {
    "steam_api_base": ("steampowered.com",),
    "steam_store_base": ("steampowered.com",),
    "sgdb_api_base": ("steamgriddb.com",),
    "itad_api_base": ("isthereanydeal.com",),
}
ALLOWED_AVATAR_FRAME_DOMAINS: tuple[str, ...] = (
    "steamstatic.com",
    "steamcdn-a.akamaihd.net",
    "steamcommunity.com",
    "steampowered.com",
)
ALLOWED_CA_FILE_EXTENSIONS: frozenset[str] = frozenset({".pem", ".crt", ".cer"})
_LOCAL_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
)
_UNSAFE_URL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\s\"'<>`]")


def _normalize_hostname(host: str | None) -> str:
    return str(host or "").strip().rstrip(".").lower()


def _is_host_in_domains(host: str, domains: tuple[str, ...]) -> bool:
    clean = _normalize_hostname(host)
    if not clean:
        return False
    return any(clean == dom or clean.endswith(f".{dom}") for dom in domains)


def _is_private_or_local_host(host: str) -> bool:
    clean = _normalize_hostname(host)
    if not clean:
        return True
    if clean == "localhost" or clean.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        ip_obj = ipaddress.ip_address(clean.strip("[]"))
    except ValueError:
        # Reject single-label hostnames (e.g. "redis", "metadata") and numeric IP aliases
        if "." not in clean:
            return True
        if clean.isdigit():
            return True
        return False
    mapped = getattr(ip_obj, "ipv4_mapped", None)
    if mapped is not None:
        ip_obj = mapped
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _origin_tuple(url: str) -> tuple[str, str, int]:
    sp = urlsplit(url)
    scheme = (sp.scheme or "").lower()
    host = _normalize_hostname(sp.hostname)
    port = sp.port or (443 if scheme == "https" else 80 if scheme == "http" else 0)
    return (scheme, host, port)


def sanitize_avatar_frame_url(url: str | None) -> str:
    """Strictly validate an external avatar frame URL before passing it to Playwright.

    Only HTTPS URLs on official Steam CDN/community domains without credentials,
    IP literals, private/local hosts, or non-standard ports are accepted.
    """
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048 or _UNSAFE_URL_CHARS_RE.search(raw):
        return ""
    try:
        sp = urlsplit(raw)
    except ValueError:
        return ""
    if (sp.scheme or "").lower() != "https":
        return ""
    if sp.username is not None or sp.password is not None:
        return ""
    try:
        port = sp.port
    except ValueError:
        return ""
    if port not in (None, 443):
        return ""
    host = _normalize_hostname(sp.hostname)
    if not host or _is_private_or_local_host(host):
        return ""
    # Reject any IP literal outright for avatar frames
    try:
        ipaddress.ip_address(host.strip("[]"))
        return ""
    except ValueError:
        pass
    if not _is_host_in_domains(host, ALLOWED_AVATAR_FRAME_DOMAINS):
        return ""
    return urlunsplit(("https", sp.netloc, sp.path or "/", sp.query, ""))


def sanitize_render_image_url(url: str | None) -> str:
    """Validate general external image URLs (avatars/covers) before Playwright rendering."""
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048 or _UNSAFE_URL_CHARS_RE.search(raw):
        return ""
    try:
        sp = urlsplit(raw)
    except ValueError:
        return ""
    scheme = (sp.scheme or "").lower()
    if scheme != "https":
        return ""
    if sp.username is not None or sp.password is not None:
        return ""
    try:
        port = sp.port
    except ValueError:
        return ""
    if port not in (None, 443):
        return ""
    host = _normalize_hostname(sp.hostname)
    if not host or _is_private_or_local_host(host):
        return ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return ""
    except ValueError:
        pass
    return urlunsplit(("https", sp.netloc, sp.path or "/", sp.query, ""))


def validate_api_base_url(key: str, raw_url: str) -> str:
    """Validate an *_api_base / steam_store_base override against HTTPS + domain whitelist."""
    val = str(raw_url or "").strip()
    if not val:
        return ""
    if len(val) > 512 or _UNSAFE_URL_CHARS_RE.search(val):
        raise ValueError(f"{key} 包含非法字符或过长")
    try:
        sp = urlsplit(val)
    except ValueError as exc:
        raise ValueError(f"{key} 不是合法的 URL") from exc
    if (sp.scheme or "").lower() != "https":
        raise ValueError(f"{key} 仅允许 https:// 协议")
    if sp.username is not None or sp.password is not None:
        raise ValueError(f"{key} 不允许包含凭据")
    if sp.query or sp.fragment:
        raise ValueError(f"{key} 不允许包含查询参数或片段")
    try:
        port = sp.port
    except ValueError as exc:
        raise ValueError(f"{key} 端口无效") from exc
    if port not in (None, 443):
        raise ValueError(f"{key} 仅允许标准 443 端口")
    host = _normalize_hostname(sp.hostname)
    if not host or _is_private_or_local_host(host):
        raise ValueError(f"{key} 禁止指向本地或内网地址")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise ValueError(f"{key} 禁止使用裸 IP 地址")
    except ValueError as exc:
        if "禁止使用裸 IP" in str(exc):
            raise
    allowed = ALLOWED_API_BASE_DOMAINS.get(key)
    if allowed and not _is_host_in_domains(host, allowed):
        raise ValueError(
            f"{key} 域名不在白名单内（允许：{', '.join(allowed)}）"
        )
    return urlunsplit(("https", sp.netloc, (sp.path or "").rstrip("/"), "", ""))

STEAM_CDN_APP_ASSETS = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps"
STEAM_COMMUNITY_ASSETS = "https://shared.fastly.steamstatic.com/community_assets/images"
STEAM_ACHIEVEMENT_ICON_BASE = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps"

STEAM_STORE_COOKIES: dict[str, str] = {
    "birthtime": "0",
    "lastagecheckage": "1-0-1970",
    "mature_content": "1",
    "wants_mature_content": "1",
}

STEAM_STORE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

STORE_REGION_FALLBACKS: tuple[str, ...] = ("HK", "TW", "JP", "US")

_STEAM_ID3_RE = re.compile(r"^\[U:1:(\d+)\]$", re.IGNORECASE)
_STEAM_ID2_RE = re.compile(r"^STEAM_[01]:([01]):(\d+)$", re.IGNORECASE)


def store_region_candidates(preferred: str = "CN") -> list[str]:
    """Return preferred store region followed by unlocked fallback regions."""
    pref = str(preferred or "CN").strip().upper() or "CN"
    ordered: list[str] = []
    for code in (pref, *STORE_REGION_FALLBACKS):
        if code and code not in ordered:
            ordered.append(code)
    return ordered


def _parse_steam_search_html(html: str, limit: int) -> list[dict[str, Any]]:
    """Parse Steam store search HTML cards into normalized item dicts."""
    if not isinstance(html, str) or limit <= 0:
        return []
    results: list[dict[str, Any]] = []
    seen_appids: set[str] = set()
    card_pattern = re.compile(
        r'<a\b(?=[^>]*\bhref=["\'][^"\']*/app/(\d+)(?:/|["\']))[^>]*>([\s\S]*?)</a>',
        re.IGNORECASE,
    )
    element_pattern = re.compile(
        r"<(?P<tag>[a-z][a-z0-9]*)\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</(?P=tag)>",
        re.IGNORECASE,
    )
    image_pattern = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
    for card in card_pattern.finditer(html):
        appid = card.group(1)
        if appid in seen_appids:
            continue
        title_match = None
        for element in element_pattern.finditer(card.group(2)):
            class_match = re.search(
                r'\bclass=["\']([^"\']*)["\']', element.group("attrs"), re.IGNORECASE
            )
            if class_match and re.search(r"(?:^|\s)title(?:\s|$)", class_match.group(1), re.IGNORECASE):
                title_match = element
                break
        if not title_match:
            continue
        title = re.sub(r"<[^>]+>", " ", title_match.group("body"))
        title = re.sub(r"\s+", " ", unescape(title)).strip()
        if not title:
            continue
        image_match = image_pattern.search(card.group(2))
        tiny_image = unescape(image_match.group(1)) if image_match else ""
        results.append(
            {
                "appid": appid,
                "id": appid,
                "name": title,
                "type": "app",
                "tiny_image": tiny_image,
                "price": None,
            }
        )
        seen_appids.add(appid)
        if len(results) >= limit:
            break
    return results


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _is_steam_shop(shop: Any) -> bool:
    if not isinstance(shop, dict):
        return False
    if shop.get("id") == 61:
        return True
    return str(shop.get("name") or "").strip().lower() == "steam"


class SteamClient:
    """Unified async client for Steam Web API, Steam Store, Community, SGDB, and ITAD."""

    def __init__(
        self,
        config_getter: Callable[[], dict[str, Any]],
        data_dir: Path,
        store: Any = None,
    ) -> None:
        self._config_getter = config_getter
        self.data_dir = Path(data_dir)
        self.store = store

        self._ssl_cache: tuple[str, ssl.SSLContext | None] = ("", None)
        self._game_name_cache: dict[str, tuple[str, str, float]] = {}
        self._cover_cache: dict[tuple[str, bool], tuple[str, float]] = {}
        self._avatar_frame_cache: dict[str, tuple[str | None, float]] = {}
        self._achievement_schema_cache: dict[tuple[str, str], tuple[dict[str, dict[str, Any]], float]] = {}
        self._no_achievement_apps: set[str] = set()
        self._no_achievement_order: deque[str] = deque()
        self._achievement_retry_after: dict[tuple[str, str], float] = {}

        self._http_client: httpx.AsyncClient | None = None
        self._store_http_client: httpx.AsyncClient | None = None
        self._http_pool_sig: tuple[int, str | None, str] | None = None

    @staticmethod
    def _cache_set(cache: dict[Any, Any], key: Any, value: Any, max_size: int) -> None:
        if key in cache:
            cache.pop(key, None)
        cache[key] = value
        limit = max(1, int(max_size))
        while len(cache) > limit:
            cache.pop(next(iter(cache)), None)

    def _mark_no_achievement_app(self, appid: str) -> None:
        gid = str(appid or "").strip()
        if not gid or gid in self._no_achievement_apps:
            return
        while len(self._no_achievement_apps) >= MAX_NO_ACHIEVEMENT_APPS and self._no_achievement_order:
            oldest = self._no_achievement_order.popleft()
            self._no_achievement_apps.discard(oldest)
        self._no_achievement_apps.add(gid)
        self._no_achievement_order.append(gid)

    # ------------------------------------------------------------------
    # Dynamic configuration helpers
    # ------------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        try:
            cfg = self._config_getter()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    @property
    def api_key(self) -> str:
        return str(self._cfg().get("steam_api_key") or "").strip()

    @property
    def sgdb_api_key(self) -> str:
        return str(self._cfg().get("sgdb_api_key") or "").strip()

    @property
    def itad_api_key(self) -> str:
        return str(self._cfg().get("itad_api_key") or "").strip()

    @property
    def proxy_url(self) -> str | None:
        cfg = self._cfg()
        if not cfg.get("enable_proxy"):
            return None
        url = str(cfg.get("proxy_url") or "").strip()
        return url or None

    @property
    def steam_api_base(self) -> str:
        return (str(self._cfg().get("steam_api_base") or "").strip() or DEFAULT_STEAM_API_BASE).rstrip("/")

    @property
    def steam_store_base(self) -> str:
        return (str(self._cfg().get("steam_store_base") or "").strip() or DEFAULT_STEAM_STORE_BASE).rstrip("/")

    @property
    def sgdb_api_base(self) -> str:
        return (str(self._cfg().get("sgdb_api_base") or "").strip() or DEFAULT_SGDB_API_BASE).rstrip("/")

    @property
    def itad_api_base(self) -> str:
        return (str(self._cfg().get("itad_api_base") or "").strip() or DEFAULT_ITAD_API_BASE).rstrip("/")

    @property
    def retry_times(self) -> int:
        try:
            val = int(self._cfg().get("retry_times", 3))
        except (TypeError, ValueError):
            val = 3
        return max(1, min(val, 10))

    @property
    def cache_avatar_hours(self) -> float:
        try:
            return max(0.0, float(self._cfg().get("cache_avatar_hours", 24)))
        except (TypeError, ValueError):
            return 24.0

    @property
    def cache_cover_hours(self) -> float:
        try:
            return max(0.0, float(self._cfg().get("cache_cover_hours", 168)))
        except (TypeError, ValueError):
            return 168.0

    @property
    def cache_avatar_frame_hours(self) -> float:
        cfg = self._cfg()
        raw = cfg.get("cache_avatar_frame_hours", cfg.get("cache_avatar_hours", 168))
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 168.0

    def _get_ssl_context(self) -> ssl.SSLContext | None:
        """Build/reuse a custom SSLContext when ssl_ca_file is set. Never disables TLS verification."""
        raw_path = str(self._cfg().get("ssl_ca_file") or "").strip()
        if not raw_path:
            if self._ssl_cache[0]:
                self._ssl_cache = ("", None)
            return None
        if "\x00" in raw_path or ".." in Path(raw_path).parts:
            logger.warning("[SteamClient] 自定义 ssl_ca_file 路径非法，回退系统 CA 证书链")
            self._ssl_cache = ("", None)
            return None
        resolved = os.path.abspath(os.path.expanduser(raw_path))
        if Path(resolved).suffix.lower() not in ALLOWED_CA_FILE_EXTENSIONS:
            logger.warning("[SteamClient] 自定义 ssl_ca_file 扩展名不受支持，回退系统 CA 证书链")
            self._ssl_cache = (resolved, None)
            return None
        if resolved == self._ssl_cache[0]:
            return self._ssl_cache[1]
        if not os.path.isfile(resolved):
            logger.warning("[SteamClient] 自定义 ssl_ca_file 不存在，回退系统 CA 证书链: %s", resolved)
            self._ssl_cache = (resolved, None)
            return None
        try:
            ctx = ssl.create_default_context(cafile=resolved)
            self._ssl_cache = (resolved, ctx)
            return ctx
        except Exception as exc:
            logger.warning("[SteamClient] 加载自定义 ssl_ca_file 失败，回退系统 CA: %s (%s)", resolved, exc)
            self._ssl_cache = (resolved, None)
            return None

    async def _request_same_origin(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        max_redirects: int = 3,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send an HTTP request without ever following cross-origin redirects.

        Protects API keys in query params or Authorization headers from leaking
        if an upstream endpoint redirects to another host or downgrades scheme.
        """
        req_kwargs = dict(kwargs)
        req_kwargs["follow_redirects"] = False
        current_url = url
        current_method = method.upper()
        initial_origin = _origin_tuple(current_url)

        for hop in range(max_redirects + 1):
            if current_method == "GET":
                resp = await client.get(current_url, **req_kwargs)
            elif current_method == "POST":
                resp = await client.post(current_url, **req_kwargs)
            else:
                resp = await client.request(current_method, current_url, **req_kwargs)

            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp

            location = str(resp.headers.get("location") or "").strip()
            if not location or hop >= max_redirects:
                return resp

            next_url = urljoin(current_url, location)
            if _origin_tuple(next_url) != initial_origin:
                logger.warning(
                    "[SteamClient] 已阻断携带敏感凭据的跨源重定向: %s -> %s",
                    initial_origin,
                    _origin_tuple(next_url),
                )
                return resp

            current_url = next_url
            # Query params already resolved into Location; avoid duplicating params on redirect
            req_kwargs.pop("params", None)
            if resp.status_code in (301, 302, 303) and current_method == "POST":
                current_method = "GET"
                req_kwargs.pop("json", None)
                req_kwargs.pop("data", None)
                req_kwargs.pop("content", None)

        return resp

    def _httpx_kwargs(
        self,
        *,
        timeout: float = 15.0,
        store: bool = False,
        follow_redirects: bool = False,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Construct kwargs for httpx.AsyncClient with proxy and strict TLS verification."""
        ssl_ctx = self._get_ssl_context()
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "verify": ssl_ctx if ssl_ctx is not None else True,
            "follow_redirects": follow_redirects,
            "limits": httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0,
            ),
        }
        proxy = self.proxy_url
        if proxy:
            kwargs["proxy"] = proxy
        merged_headers: dict[str, str] = {}
        if store:
            kwargs["cookies"] = dict(STEAM_STORE_COOKIES)
            merged_headers.update(STEAM_STORE_HEADERS)
        if headers:
            merged_headers.update(headers)
        if merged_headers:
            kwargs["headers"] = merged_headers
        return kwargs

    def _current_pool_signature(self) -> tuple[int, str | None, str]:
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        raw_ca = str(self._cfg().get("ssl_ca_file") or "").strip()
        resolved_ca = os.path.abspath(os.path.expanduser(raw_ca)) if raw_ca else ""
        return (loop_id, self.proxy_url, resolved_ca)

    async def _close_client_safely(self, client: httpx.AsyncClient | None) -> None:
        if client is None:
            return
        try:
            if not getattr(client, "is_closed", False):
                await client.aclose()
        except Exception as exc:
            logger.debug("[SteamClient] 关闭 httpx 连接池异常（已忽略）: %s", exc)

    async def _get_http_client(self, *, store: bool = False) -> httpx.AsyncClient:
        """Return a shared pooled httpx.AsyncClient (rebuilt automatically on proxy/CA/loop change)."""
        sig = self._current_pool_signature()
        if self._http_pool_sig is not None and self._http_pool_sig != sig:
            old_api, old_store = self._http_client, self._store_http_client
            self._http_client = None
            self._store_http_client = None
            # Only await close on the same loop; if loop changed, avoid cross-loop close errors.
            if self._http_pool_sig[0] == sig[0]:
                await self._close_client_safely(old_api)
                await self._close_client_safely(old_store)
            self._http_pool_sig = None

        self._http_pool_sig = sig
        if store:
            if self._store_http_client is None or getattr(self._store_http_client, "is_closed", False):
                self._store_http_client = httpx.AsyncClient(
                    **self._httpx_kwargs(timeout=15.0, store=True)
                )
            return self._store_http_client

        if self._http_client is None or getattr(self._http_client, "is_closed", False):
            self._http_client = httpx.AsyncClient(
                **self._httpx_kwargs(timeout=15.0, store=False)
            )
        return self._http_client

    async def aclose(self) -> None:
        """Close all shared pooled httpx.AsyncClient instances."""
        api_client, store_client = self._http_client, self._store_http_client
        self._http_client = None
        self._store_http_client = None
        self._http_pool_sig = None
        await self._close_client_safely(api_client)
        await self._close_client_safely(store_client)

    def clear_caches(self) -> dict[str, int]:
        """Clear all in-memory API caches and return cleared entry counts."""
        counts = {
            "game_names": len(self._game_name_cache),
            "covers": len(self._cover_cache),
            "avatar_frames": len(self._avatar_frame_cache),
            "achievement_schemas": len(self._achievement_schema_cache),
            "no_achievement_apps": len(self._no_achievement_apps),
            "achievement_cooldowns": len(self._achievement_retry_after),
        }
        self._game_name_cache.clear()
        self._cover_cache.clear()
        self._avatar_frame_cache.clear()
        self._achievement_schema_cache.clear()
        self._no_achievement_apps.clear()
        self._no_achievement_order.clear()
        self._achievement_retry_after.clear()
        return counts

    # ------------------------------------------------------------------
    # SteamID resolution
    # ------------------------------------------------------------------

    async def resolve_steam_input(self, raw: str) -> str | None:
        """Resolve SteamID64, profile URL, vanity URL, SteamID2/3, or 8-10 digit friend code to 17-digit SteamID64."""
        if not raw or not isinstance(raw, str):
            return None
        s = raw.strip()
        if not s:
            return None

        # 1) Direct 17-digit SteamID64
        if s.isdigit() and len(s) == 17:
            return s

        # 2) SteamID3 [U:1:<accountid>]
        m3 = _STEAM_ID3_RE.match(s)
        if m3:
            try:
                sid64 = str(int(m3.group(1)) + STEAM_ID64_OFFSET)
                if len(sid64) == 17:
                    return sid64
            except ValueError:
                pass

        # 3) SteamID2 STEAM_0:Y:Z
        m2 = _STEAM_ID2_RE.match(s)
        if m2:
            try:
                y_bit = int(m2.group(1))
                z_num = int(m2.group(2))
                sid64 = str(z_num * 2 + y_bit + STEAM_ID64_OFFSET)
                if len(sid64) == 17:
                    return sid64
            except ValueError:
                pass

        # 4) Community URLs
        lowered = s.lower()
        if "steamcommunity.com" in lowered or "s.team/p/" in lowered:
            path = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
            segments = [seg for seg in path.split("/") if seg]
            if len(segments) >= 2:
                last = segments[-1]
                prev = segments[-2].lower()
                if prev == "profiles" and last.isdigit() and len(last) == 17:
                    return last
                if prev == "id" and last:
                    return await self._resolve_vanity_url(last)
                if "s.team" in lowered and last.isdigit() and len(last) == 17:
                    return last
            return None

        # 5) 8-10 digit Steam friend code (accountid + 76561197960265728)
        if s.isdigit() and 8 <= len(s) <= 10:
            try:
                sid64 = str(int(s) + STEAM_ID64_OFFSET)
                if len(sid64) == 17:
                    return sid64
            except ValueError:
                pass

        return None

    async def _resolve_vanity_url(self, vanity: str) -> str | None:
        api_key = self.api_key
        if not api_key or not vanity:
            return None
        url = f"{self.steam_api_base}/ISteamUser/ResolveVanityURL/v1/"
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(
                url,
                params={"key": api_key, "vanityurl": vanity},
                timeout=15.0,
            )
            if resp.status_code != 200:
                logger.warning("[SteamClient] ResolveVanityURL HTTP %s (vanity=%s)", resp.status_code, vanity)
                return None
            data = resp.json()
            resp_data = data.get("response") if isinstance(data, dict) else None
            if isinstance(resp_data, dict) and resp_data.get("success") == 1:
                sid = str(resp_data.get("steamid") or "").strip()
                if sid.isdigit() and len(sid) == 17:
                    return sid
        except Exception as exc:
            logger.warning("[SteamClient] ResolveVanityURL 失败 (vanity=%s): %s", vanity, exc)
        return None

    # ------------------------------------------------------------------
    # Player status polling (CRITICAL batch failure tracking)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_player_status(player: dict[str, Any]) -> dict[str, Any]:
        sid = str(player.get("steamid") or "").strip()
        raw_gid = player.get("gameid")
        gameid = str(raw_gid).strip() if raw_gid is not None and str(raw_gid).strip() else None
        raw_extra = player.get("gameextrainfo")
        gameextrainfo = str(raw_extra).strip() if raw_extra else None
        try:
            personastate = int(player.get("personastate", 0) or 0)
        except (TypeError, ValueError):
            personastate = 0
        try:
            visibility = int(player.get("communityvisibilitystate", 1) or 1)
        except (TypeError, ValueError):
            visibility = 1
        return {
            "steamid": sid,
            "name": str(player.get("personaname") or sid),
            "gameid": gameid,
            "gameextrainfo": gameextrainfo,
            "personastate": personastate,
            "lastlogoff": player.get("lastlogoff"),
            "avatarfull": str(player.get("avatarfull") or ""),
            "avatar": str(player.get("avatar") or ""),
            "profileurl": str(player.get("profileurl") or f"https://steamcommunity.com/profiles/{sid}/"),
            "communityvisibilitystate": visibility,
        }

    async def fetch_player_statuses_batch(
        self,
        steam_ids: list[str],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Batch fetch player summaries in <=100-ID chunks with exponential backoff.

        Returns `(statuses_map, failed_sids)`.
        CRITICAL: Any batch failure or malformed/non-dict response places ALL sids in
        that batch into `failed_sids` so callers freeze state instead of misclassifying
        as a game exit. Any sid missing from a batch's `players` list is also marked
        in `failed_sids`.
        """
        deduped: list[str] = []
        seen: set[str] = set()
        for raw_sid in steam_ids or []:
            sid = str(raw_sid or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                deduped.append(sid)

        if not deduped:
            return {}, set()

        api_key = self.api_key
        if not api_key:
            logger.warning("[SteamClient] 未配置 steam_api_key，跳过本轮批量状态查询 (%d 个玩家)", len(deduped))
            return {}, set(deduped)

        statuses_map: dict[str, dict[str, Any]] = {}
        failed_sids: set[str] = set()
        retries = self.retry_times
        url = f"{self.steam_api_base}/ISteamUser/GetPlayerSummaries/v2/"
        batch_size = 100

        for idx in range(0, len(deduped), batch_size):
            batch = deduped[idx : idx + batch_size]
            batch_set = set(batch)
            params = {"key": api_key, "steamids": ",".join(batch)}
            delay = 1.0
            batch_ok = False

            for attempt in range(retries):
                try:
                    client = await self._get_http_client(store=False)
                    resp = await client.get(url, params=params, timeout=15.0)
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError(f"非字典响应: {type(data).__name__}")
                    resp_data = data.get("response")
                    if not isinstance(resp_data, dict):
                        raise ValueError(f"response 字段异常: {type(resp_data).__name__}")
                    players = resp_data.get("players")
                    if not isinstance(players, list):
                        raise ValueError(f"players 字段异常: {type(players).__name__}")

                    returned_sids: set[str] = set()
                    for player in players:
                        if not isinstance(player, dict):
                            continue
                        sid = str(player.get("steamid") or "").strip()
                        if sid and sid in batch_set:
                            statuses_map[sid] = self._normalize_player_status(player)
                            returned_sids.add(sid)

                    missing = batch_set - returned_sids
                    if missing:
                        logger.warning(
                            "[SteamClient] 批量查询响应缺失 %d 个 SteamID（将冻结状态）: %s",
                            len(missing),
                            sorted(missing)[:5],
                        )
                        failed_sids.update(missing)
                    batch_ok = True
                    break
                except Exception as exc:
                    logger.warning(
                        "[SteamClient] 批量状态查询失败 (批次 %d 人, 第 %d/%d 次): %s",
                        len(batch),
                        attempt + 1,
                        retries,
                        exc,
                    )
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        delay *= 2.0

            if not batch_ok:
                logger.error(
                    "[SteamClient] 批量状态查询重试耗尽，冻结本批 %d 个玩家状态",
                    len(batch),
                )
                failed_sids.update(batch_set)

        return statuses_map, failed_sids

    async def fetch_player_summary(self, steam_id: str) -> dict[str, Any] | None:
        """Fetch raw player summary dict for a single SteamID64."""
        sid = str(steam_id or "").strip()
        api_key = self.api_key
        if not sid or not api_key:
            return None
        url = f"{self.steam_api_base}/ISteamUser/GetPlayerSummaries/v2/"
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(
                url, params={"key": api_key, "steamids": sid}, timeout=15.0
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return None
            resp_data = data.get("response")
            if not isinstance(resp_data, dict):
                return None
            players = resp_data.get("players")
            if isinstance(players, list) and players and isinstance(players[0], dict):
                return dict(players[0])
        except Exception as exc:
            logger.warning("[SteamClient] 获取玩家摘要失败 (sid=%s): %s", sid, exc)
        return None

    # ------------------------------------------------------------------
    # Game metadata, online count, covers, avatar frames
    # ------------------------------------------------------------------

    @staticmethod
    def default_game_cover_url(appid: str, *, vertical: bool = False) -> str:
        """Return the official Steam CDN cover URL for an appid."""
        gid = str(appid or "").strip()
        if not gid:
            return ""
        filename = "library_600x900.jpg" if vertical else "header.jpg"
        return f"{STEAM_CDN_APP_ASSETS}/{gid}/{filename}"

    def _read_store_game_cache(self, gid: str) -> tuple[str, str] | None:
        if self.store is None:
            return None
        getter = getattr(self.store, "get_game_cache", None)
        if not callable(getter):
            return None
        try:
            row = getter(gid)
        except Exception as exc:
            logger.debug("[SteamClient] 读取 store.get_game_cache 失败 (appid=%s): %s", gid, exc)
            return None
        if not row:
            return None
        if isinstance(row, dict):
            updated_at = _safe_float(row.get("updated_at")) or 0.0
            ttl_sec = max(24.0, self.cache_cover_hours) * 3600.0
            if updated_at > 0 and (time.time() - updated_at) > ttl_sec:
                return None
            zh = str(row.get("name_zh") or "").strip()
            en = str(row.get("name_en") or "").strip()
            if zh or en:
                return (zh or en, en or zh)
        return None

    def _write_store_game_cache(
        self,
        gid: str,
        name_zh: str,
        name_en: str,
        header_url: str = "",
    ) -> None:
        if self.store is None:
            return
        upserter = getattr(self.store, "upsert_game_cache", None)
        if not callable(upserter):
            return
        try:
            sig = inspect.signature(upserter)
            params = sig.parameters
            kwargs: dict[str, Any] = {}
            if "name_zh" in params:
                kwargs["name_zh"] = name_zh
            if "name_en" in params:
                kwargs["name_en"] = name_en
            if "header_url" in params:
                kwargs["header_url"] = header_url or self.default_game_cover_url(gid)
            if "game_id" in params:
                kwargs["game_id"] = gid
                upserter(**kwargs)
            elif "appid" in params:
                kwargs["appid"] = gid
                upserter(**kwargs)
            else:
                upserter(gid, name_zh, name_en, header_url or self.default_game_cover_url(gid))
        except Exception as exc:
            logger.debug("[SteamClient] 写入 store.upsert_game_cache 失败 (appid=%s): %s", gid, exc)

    async def get_game_names(self, appid: str, fallback: str = "") -> tuple[str, str]:
        """Return `(zh_name, en_name)` for a Steam appid using memory cache, SQLite cache, and Steam Store API."""
        gid = str(appid or "").strip()
        fb = str(fallback or "").strip() or "未知游戏"
        if not gid:
            return (fb, fb)

        now = time.time()
        cached = self._game_name_cache.get(gid)
        if cached and (now - cached[2]) < 7 * 86400:
            return (cached[0], cached[1])

        store_hit = await asyncio.to_thread(self._read_store_game_cache, gid)
        if store_hit:
            self._cache_set(
                self._game_name_cache,
                gid,
                (store_hit[0], store_hit[1], now),
                MAX_GAME_NAME_CACHE_SIZE,
            )
            return store_hit

        if not gid.isdigit():
            return (fb, fb)

        name_zh = ""
        name_en = ""
        header_url = ""
        try:
            client = await self._get_http_client(store=True)
            zh_data = await self._request_appdetails(
                client, gid, language="schinese", country="CN", timeout=12.0
            )
            if not zh_data:
                zh_data = await self._request_appdetails(
                    client, gid, language="schinese", country="HK", timeout=12.0
                )
            if zh_data:
                name_zh = str(zh_data.get("name") or "").strip()
                header_url = str(zh_data.get("header_image") or "").strip()

            en_data = await self._request_appdetails(
                client, gid, language="english", country="US", timeout=12.0
            )
            if en_data:
                name_en = str(en_data.get("name") or "").strip()
                if not header_url:
                    header_url = str(en_data.get("header_image") or "").strip()
        except Exception as exc:
            logger.warning("[SteamClient] 获取游戏名称失败 (appid=%s): %s", gid, exc)

        if name_zh or name_en:
            final_zh = name_zh or name_en
            final_en = name_en or name_zh
            self._cache_set(
                self._game_name_cache,
                gid,
                (final_zh, final_en, now),
                MAX_GAME_NAME_CACHE_SIZE,
            )
            await asyncio.to_thread(
                self._write_store_game_cache, gid, final_zh, final_en, header_url
            )
            return (final_zh, final_en)

        return (fb, fb)

    async def get_game_online_count(self, appid: str) -> int | None:
        """Fetch current concurrent player count for a Steam appid."""
        gid = str(appid or "").strip()
        if not gid or not gid.isdigit():
            return None
        url = f"{self.steam_api_base}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(url, params={"appid": gid}, timeout=10.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            resp_data = data.get("response") if isinstance(data, dict) else None
            if isinstance(resp_data, dict) and resp_data.get("result") == 1:
                count = resp_data.get("player_count")
                if isinstance(count, int):
                    return count
        except Exception as exc:
            logger.debug("[SteamClient] 获取游戏在线人数失败 (appid=%s): %s", gid, exc)
        return None

    async def get_game_cover_url(self, appid: str, *, vertical: bool = False) -> str:
        """Return cover image URL: defaults to Steam CDN, optionally enriched via SGDB if sgdb_api_key is set."""
        gid = str(appid or "").strip()
        if not gid:
            return ""

        default_url = self.default_game_cover_url(gid, vertical=vertical)
        cache_key = (gid, bool(vertical))
        ttl = self.cache_cover_hours * 3600.0
        now = time.time()
        cached = self._cover_cache.get(cache_key)
        if cached and (ttl == 0 or (now - cached[1]) < ttl):
            return cached[0]

        sgdb_key = self.sgdb_api_key
        if sgdb_key and gid.isdigit():
            dimensions = "600x900" if vertical else "460x215,920x430"
            url = f"{self.sgdb_api_base}/grids/steam/{gid}"
            try:
                client = await self._get_http_client(store=False)
                resp = await client.get(
                    url,
                    params={"dimensions": dimensions},
                    headers={"Authorization": f"Bearer {sgdb_key}"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    items = payload.get("data") if isinstance(payload, dict) else None
                    if isinstance(items, list) and items:
                        first = items[0]
                        if isinstance(first, dict):
                            sgdb_url = str(first.get("url") or first.get("thumb") or "").strip()
                            if sgdb_url:
                                self._cache_set(
                                    self._cover_cache,
                                    cache_key,
                                    (sgdb_url, now),
                                    MAX_COVER_CACHE_SIZE,
                                )
                                return sgdb_url
            except Exception as exc:
                logger.debug("[SteamClient] SGDB 封面查询失败，回退 Steam CDN (appid=%s): %s", gid, exc)

        self._cache_set(
            self._cover_cache,
            cache_key,
            (default_url, now),
            MAX_COVER_CACHE_SIZE,
        )
        return default_url

    async def fetch_avatar_frame_url(self, steam_id: str) -> str | None:
        """Fetch and cache the player's Steam animated/static avatar frame URL."""
        sid = str(steam_id or "").strip()
        if not sid:
            return None

        ttl = self.cache_avatar_frame_hours * 3600.0
        now = time.time()
        cached = self._avatar_frame_cache.get(sid)
        if cached and (ttl == 0 or (now - cached[1]) < ttl):
            return cached[0]

        # 1) Try official IPlayerService/GetAvatarFrame/v1 when API key is available
        api_key = self.api_key
        if api_key:
            api_url = f"{self.steam_api_base}/IPlayerService/GetAvatarFrame/v1/"
            try:
                client = await self._get_http_client(store=False)
                resp = await client.get(
                    api_url,
                    params={"key": api_key, "steamid": sid},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    frame = ((payload.get("response") or {}).get("avatar_frame") or {}) if isinstance(payload, dict) else {}
                    if isinstance(frame, dict):
                        img = str(frame.get("image_large") or frame.get("image_small") or "").strip()
                        if img:
                            if not img.startswith(("http://", "https://")):
                                img = f"{STEAM_COMMUNITY_ASSETS}/{img.lstrip('/')}"
                            safe_img = sanitize_avatar_frame_url(img)
                            if safe_img:
                                self._cache_set(
                                    self._avatar_frame_cache,
                                    sid,
                                    (safe_img, now),
                                    MAX_AVATAR_FRAME_CACHE_SIZE,
                                )
                                return safe_img
                            logger.warning(
                                "[SteamClient] 拒绝非法头像框 URL (API, sid=%s): %r", sid, img
                            )
                        # API responded validly with empty frame object -> user has no frame
                        if "response" in payload:
                            self._cache_set(
                                self._avatar_frame_cache,
                                sid,
                                (None, now),
                                MAX_AVATAR_FRAME_CACHE_SIZE,
                            )
                            return None
            except Exception as exc:
                logger.debug("[SteamClient] GetAvatarFrame API 失败 (sid=%s): %s", sid, exc)

        # 2) Fallback: parse public Steam community profile HTML
        profile_url = f"https://steamcommunity.com/profiles/{sid}/?l=english"
        try:
            client = await self._get_http_client(store=False)
            resp = await self._request_same_origin(
                client, "GET", profile_url, timeout=10.0
            )
            if resp.status_code == 200:
                html = resp.text
                div_match = re.search(
                    r'<div class="profile_avatar_frame">(.*?)</div>', html, re.DOTALL | re.IGNORECASE
                )
                if div_match:
                    sources = re.findall(r'(?:srcset|src)="([^"]+)"', div_match.group(1), re.IGNORECASE)
                    if sources:
                        raw_result = sources[-1].split(",")[0].strip().split(" ")[0].strip()
                        result = sanitize_avatar_frame_url(raw_result)
                        if result:
                            self._cache_set(
                                self._avatar_frame_cache,
                                sid,
                                (result, now),
                                MAX_AVATAR_FRAME_CACHE_SIZE,
                            )
                            return result
                        if raw_result:
                            logger.warning(
                                "[SteamClient] 拒绝非法头像框 URL (HTML, sid=%s): %r", sid, raw_result
                            )
                g_match = re.search(r"g_rgProfileData\s*=\s*(\{.+?\});", html, re.DOTALL)
                if g_match:
                    try:
                        profile = json.loads(g_match.group(1).replace(r"\/", "/"))
                        af = profile.get("avatar_frame") if isinstance(profile, dict) else None
                        if isinstance(af, dict) and af.get("url"):
                            raw_result = str(af["url"]).strip()
                            result = sanitize_avatar_frame_url(raw_result)
                            if result:
                                self._cache_set(
                                    self._avatar_frame_cache,
                                    sid,
                                    (result, now),
                                    MAX_AVATAR_FRAME_CACHE_SIZE,
                                )
                                return result
                            if raw_result:
                                logger.warning(
                                    "[SteamClient] 拒绝非法头像框 URL (profileData, sid=%s): %r",
                                    sid,
                                    raw_result,
                                )
                    except Exception:
                        pass
                self._cache_set(
                    self._avatar_frame_cache,
                    sid,
                    (None, now),
                    MAX_AVATAR_FRAME_CACHE_SIZE,
                )
                return None
        except Exception as exc:
            logger.debug("[SteamClient] 社区主页解析头像框失败 (sid=%s): %s", sid, exc)

        return None

    # ------------------------------------------------------------------
    # Steam Store APIs (appdetails, prices, reviews, search)
    # ------------------------------------------------------------------

    async def _request_appdetails(
        self,
        client: httpx.AsyncClient,
        appid: str,
        *,
        language: str | None = None,
        country: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        params: dict[str, str] = {"appids": str(appid)}
        if language:
            params["l"] = language
        if country:
            params["cc"] = str(country).lower()
        url = f"{self.steam_store_base}/api/appdetails"
        resp = await client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        item = payload.get(str(appid), {}) if isinstance(payload, dict) else {}
        if not isinstance(item, dict) or not item.get("success"):
            return None
        data = item.get("data")
        return data if isinstance(data, dict) else None

    async def fetch_store_appdetails(
        self,
        appid: str,
        country: str = "CN",
        lang: str = "schinese",
    ) -> dict[str, Any] | None:
        """Fetch Steam store appdetails with age-gate cookies and region/language fallback."""
        gid = str(appid or "").strip()
        if not gid.isdigit():
            return None

        preferred = str(country or "CN").strip().upper() or "CN"
        languages: list[str] = []
        if lang:
            languages.append(lang)
        if lang not in ("english", "en"):
            languages.append("english")

        try:
            client = await self._get_http_client(store=True)
            for try_lang in languages:
                for cc in store_region_candidates(preferred):
                    try:
                        data = await self._request_appdetails(
                            client, gid, language=try_lang, country=cc, timeout=15.0
                        )
                    except Exception as exc:
                        logger.warning(
                            "[SteamClient] 获取 %s 区商店详情失败 (appid=%s, lang=%s): %s",
                            cc,
                            gid,
                            try_lang,
                            exc,
                        )
                        continue
                    if data:
                        result = dict(data)
                        result["_store_region"] = cc
                        result["_store_language"] = try_lang
                        return result
        except Exception as exc:
            logger.warning("[SteamClient] 获取商店详情异常 (appid=%s): %s", gid, exc)
        return None

    async def fetch_region_price(
        self,
        appid: str,
        country: str = "CN",
        *,
        allow_fallback: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch Steam store price for a specific country code (optionally falling back if locked)."""
        gid = str(appid or "").strip()
        if not gid.isdigit():
            return None
        preferred = str(country or "CN").strip().upper() or "CN"
        candidates = store_region_candidates(preferred) if allow_fallback else [preferred]
        try:
            client = await self._get_http_client(store=True)
            for cc in candidates:
                try:
                    data = await self._request_appdetails(
                        client, gid, country=cc, timeout=15.0
                    )
                except Exception as exc:
                    logger.debug("[SteamClient] 获取 %s 区价格失败 (appid=%s): %s", cc, gid, exc)
                    continue
                if not data:
                    continue
                if data.get("is_free"):
                    return {
                        "region": cc,
                        "currency": None,
                        "current_price": 0.0,
                        "current_regular": 0.0,
                        "cut": 0,
                        "formatted": "免费游玩",
                        "is_free": True,
                    }
                po = data.get("price_overview")
                if not isinstance(po, dict) or not po:
                    continue
                final_val = (po.get("final") or 0) / 100.0
                initial_val = (po.get("initial") or po.get("final") or 0) / 100.0
                return {
                    "region": cc,
                    "currency": str(po.get("currency") or "").upper() or None,
                    "current_price": round(final_val, 2),
                    "current_regular": round(initial_val, 2),
                    "cut": int(po.get("discount_percent") or 0),
                    "formatted": str(po.get("final_formatted") or ""),
                    "is_free": False,
                }
        except Exception as exc:
            logger.warning("[SteamClient] 获取商店区域价格异常 (appid=%s, cc=%s): %s", gid, preferred, exc)
        return None

    async def _fetch_review_summary(self, appid: str, language: str = "all") -> dict[str, Any] | None:
        gid = str(appid or "").strip()
        if not gid.isdigit():
            return None
        url = f"{self.steam_store_base}/appreviews/{gid}"
        params = {"json": 1, "filter": "summary", "language": language or "all"}
        try:
            client = await self._get_http_client(store=True)
            resp = await client.get(url, params=params, timeout=12.0)
            resp.raise_for_status()
            payload = resp.json()
            summary = payload.get("query_summary") or payload.get("querySummary") or {}
            total = int(summary.get("total_reviews") or summary.get("totalReviews") or 0)
            positive = int(summary.get("total_positive") or summary.get("totalPositive") or 0)
            if total <= 0:
                return {"text": "暂无评价", "percent": None, "total": 0}
            percent = round(positive * 100 / total)
            if percent >= 95:
                label = "好评如潮"
            elif percent >= 80:
                label = "特别好评"
            elif percent >= 70:
                label = "多半好评"
            elif percent >= 40:
                label = "褒贬不一"
            elif percent >= 20:
                label = "多半差评"
            else:
                label = "差评"
            return {"text": label, "percent": percent, "total": total}
        except Exception as exc:
            logger.debug("[SteamClient] 获取评价摘要失败 (appid=%s, lang=%s): %s", gid, language, exc)
            return None

    async def fetch_game_reviews(self, appid: str) -> dict[str, dict[str, Any] | None]:
        """Fetch both global ('all') and Simplified Chinese ('schinese') review summaries."""
        all_rev, zh_rev = await asyncio.gather(
            self._fetch_review_summary(appid, "all"),
            self._fetch_review_summary(appid, "schinese"),
        )
        return {"all": all_rev, "schinese": zh_rev}

    async def search_steam_store(
        self,
        query: str,
        country: str = "CN",
        lang: str = "schinese",
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search Steam Store via storesearch API with HTML search fallback."""
        term = str(query or "").strip()
        if not term:
            return []
        cc = (str(country or "CN").strip().lower() or "cn")
        language = str(lang or "schinese").strip() or "schinese"

        # 1) Primary: storesearch JSON API
        try:
            client = await self._get_http_client(store=True)
            resp = await self._request_same_origin(
                client,
                "GET",
                f"{self.steam_store_base}/api/storesearch/",
                params={"term": term, "l": language, "cc": cc},
                timeout=15.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            raw_items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(raw_items, list) and raw_items:
                results: list[dict[str, Any]] = []
                for item in raw_items[:limit]:
                    if not isinstance(item, dict):
                        continue
                    appid = str(item.get("id") or "").strip()
                    name = str(item.get("name") or "").strip()
                    if not appid or not name:
                        continue
                    results.append(
                        {
                            "appid": appid,
                            "id": appid,
                            "name": name,
                            "type": str(item.get("type") or "app").lower(),
                            "tiny_image": str(item.get("tiny_image") or ""),
                            "price": item.get("price") if isinstance(item.get("price"), dict) else None,
                        }
                    )
                if results:
                    return results
        except Exception as exc:
            logger.warning("[SteamClient] Steam storesearch 失败 (query=%s): %s", term, exc)

        # 2) Fallback: store HTML search endpoint
        try:
            client = await self._get_http_client(store=True)
            resp = await self._request_same_origin(
                client,
                "GET",
                f"{self.steam_store_base}/search/results/",
                params={"term": term, "l": language, "cc": cc, "count": limit, "json": 1},
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            html = payload.get("results_html", "") if isinstance(payload, dict) else ""
            parsed = _parse_steam_search_html(html, limit)
            if parsed:
                return parsed
        except Exception as exc:
            logger.debug("[SteamClient] Steam HTML 搜索兜底失败 (query=%s): %s", term, exc)

        return []

    # ------------------------------------------------------------------
    # OpenBox player profile (graceful partial degradation)
    # ------------------------------------------------------------------

    async def _fetch_steam_level(self, steam_id: str) -> int | None:
        api_key = self.api_key
        if not api_key:
            return None
        url = f"{self.steam_api_base}/IPlayerService/GetSteamLevel/v1/"
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(
                url, params={"key": api_key, "steamid": steam_id}, timeout=12.0
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            level = ((data.get("response") or {}).get("player_level")) if isinstance(data, dict) else None
            return int(level) if level is not None else None
        except Exception as exc:
            logger.debug("[SteamClient] 获取 Steam 等级失败 (sid=%s): %s", steam_id, exc)
            return None

    async def _fetch_player_bans(self, steam_id: str) -> dict[str, Any] | None:
        api_key = self.api_key
        if not api_key:
            return None
        url = f"{self.steam_api_base}/ISteamUser/GetPlayerBans/v1/"
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(
                url, params={"key": api_key, "steamids": steam_id}, timeout=12.0
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            players = data.get("players") if isinstance(data, dict) else None
            if isinstance(players, list) and players and isinstance(players[0], dict):
                b = players[0]
                return {
                    "community_banned": bool(b.get("CommunityBanned", False)),
                    "vac_banned": bool(b.get("VACBanned", False)),
                    "number_of_vac_bans": int(b.get("NumberOfVACBans", 0) or 0),
                    "days_since_last_ban": int(b.get("DaysSinceLastBan", 0) or 0),
                    "number_of_game_bans": int(b.get("NumberOfGameBans", 0) or 0),
                    "economy_ban": str(b.get("EconomyBan") or "none"),
                }
        except Exception as exc:
            logger.debug("[SteamClient] 获取封禁状态失败 (sid=%s): %s", steam_id, exc)
        return None

    async def _fetch_owned_games(self, steam_id: str) -> tuple[int | None, list[dict[str, Any]]]:
        api_key = self.api_key
        if not api_key:
            return None, []
        url = f"{self.steam_api_base}/IPlayerService/GetOwnedGames/v1/"
        params = {
            "key": api_key,
            "steamid": steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
        }
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(url, params=params, timeout=15.0)
            if resp.status_code != 200:
                return None, []
            data = resp.json()
            resp_data = data.get("response") if isinstance(data, dict) else None
            if not isinstance(resp_data, dict) or not resp_data:
                return None, []
            game_count = resp_data.get("game_count")
            games = resp_data.get("games")
            if game_count is None and not isinstance(games, list):
                return None, []
            return int(game_count or len(games or [])), (games if isinstance(games, list) else [])
        except Exception as exc:
            logger.debug("[SteamClient] 获取拥有游戏列表失败 (sid=%s): %s", steam_id, exc)
            return None, []

    async def _fetch_recent_games(self, steam_id: str, count: int = 5) -> list[dict[str, Any]] | None:
        api_key = self.api_key
        if not api_key:
            return None
        url = f"{self.steam_api_base}/IPlayerService/GetRecentlyPlayedGames/v1/"
        try:
            client = await self._get_http_client(store=False)
            resp = await client.get(
                url,
                params={"key": api_key, "steamid": steam_id, "count": count},
                timeout=12.0,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            resp_data = data.get("response") if isinstance(data, dict) else None
            if not isinstance(resp_data, dict) or not resp_data:
                return None
            games = resp_data.get("games")
            if not isinstance(games, list):
                return [] if "total_count" in resp_data else None
            return games
        except Exception as exc:
            logger.debug("[SteamClient] 获取近期游玩失败 (sid=%s): %s", steam_id, exc)
            return None

    async def fetch_openbox_profile(self, steam_id: str) -> dict[str, Any]:
        """Aggregate comprehensive player profile with graceful partial degradation for private profiles."""
        sid = str(steam_id or "").strip()
        summary_res, level_res, bans_res, owned_res, recent_res, frame_res = await asyncio.gather(
            self.fetch_player_summary(sid),
            self._fetch_steam_level(sid),
            self._fetch_player_bans(sid),
            self._fetch_owned_games(sid),
            self._fetch_recent_games(sid, count=5),
            self.fetch_avatar_frame_url(sid),
            return_exceptions=True,
        )

        summary = summary_res if isinstance(summary_res, dict) else None
        steam_level = level_res if isinstance(level_res, int) else None
        bans = bans_res if isinstance(bans_res, dict) else None
        avatar_frame_url = frame_res if isinstance(frame_res, str) else None

        owned_games_count: int | None = None
        total_playtime_hours: float | None = None
        top_games: list[dict[str, Any]] = []
        if isinstance(owned_res, tuple) and len(owned_res) == 2:
            count_val, raw_owned = owned_res
            owned_games_count = count_val
            if count_val is not None:
                total_mins = sum(
                    int(g.get("playtime_forever") or 0)
                    for g in raw_owned
                    if isinstance(g, dict)
                )
                total_playtime_hours = round(total_mins / 60.0, 1)
                sorted_games = sorted(
                    (g for g in raw_owned if isinstance(g, dict) and int(g.get("playtime_forever") or 0) > 0),
                    key=lambda g: int(g.get("playtime_forever") or 0),
                    reverse=True,
                )
                for g in sorted_games[:6]:
                    appid = str(g.get("appid") or "")
                    mins = int(g.get("playtime_forever") or 0)
                    top_games.append(
                        {
                            "appid": appid,
                            "name": str(g.get("name") or f"App {appid}"),
                            "playtime_minutes": mins,
                            "playtime_hours": round(mins / 60.0, 1),
                            "cover_url": self.default_game_cover_url(appid),
                        }
                    )

        recent_games: list[dict[str, Any]] = []
        recent_hidden = False
        if isinstance(recent_res, list):
            for g in recent_res[:5]:
                if not isinstance(g, dict):
                    continue
                appid = str(g.get("appid") or "")
                mins_2w = int(g.get("playtime_2weeks") or 0)
                mins_forever = int(g.get("playtime_forever") or 0)
                recent_games.append(
                    {
                        "appid": appid,
                        "name": str(g.get("name") or f"App {appid}"),
                        "playtime_2weeks_minutes": mins_2w,
                        "playtime_2weeks_hours": round(mins_2w / 60.0, 1),
                        "playtime_forever_hours": round(mins_forever / 60.0, 1),
                        "cover_url": self.default_game_cover_url(appid),
                    }
                )
        else:
            recent_hidden = True

        privacy_notes: list[str] = []
        if summary is None:
            privacy_notes.append("未能获取到该玩家的 Steam 基础资料（可能 SteamID 无效或 API 暂不可用）。")
        else:
            vis = int(summary.get("communityvisibilitystate", 1) or 1)
            if vis != 3:
                privacy_notes.append("该玩家个人资料设为私密或仅好友可见，部分信息无法获取。")
            elif owned_games_count is None and recent_hidden:
                privacy_notes.append("该玩家隐藏了游戏详情，游戏库与近期游玩记录不可见。")
            elif owned_games_count is None:
                privacy_notes.append("该玩家隐藏了游戏库详情，拥有游戏数与总时长不可见。")

        return {
            "steam_id": sid,
            "summary": summary,
            "steam_level": steam_level,
            "bans": bans,
            "owned_games_count": owned_games_count,
            "total_playtime_hours": total_playtime_hours,
            "recent_games": recent_games,
            "top_games": top_games,
            "avatar_frame_url": avatar_frame_url,
            "privacy_note": " ".join(privacy_notes),
        }

    # ------------------------------------------------------------------
    # Achievement APIs
    # ------------------------------------------------------------------

    def is_no_achievement_app(self, appid: str) -> bool:
        return str(appid or "").strip() in self._no_achievement_apps

    async def fetch_player_achievements(self, steam_id: str, appid: str) -> set[str] | None:
        """Fetch set of unlocked achievement apinames for `(steam_id, appid)`.

        Returns:
            `set[str]` of unlocked apinames on success, or `None` on API error / private / no stats.
        """
        sid = str(steam_id or "").strip()
        gid = str(appid or "").strip()
        api_key = self.api_key
        if not sid or not gid or not gid.isdigit() or not api_key:
            return None
        if gid in self._no_achievement_apps:
            return None

        now = time.monotonic()
        if self._achievement_retry_after:
            self._achievement_retry_after = {
                k: v for k, v in self._achievement_retry_after.items() if v > now
            }
        retry_key = (sid, gid)
        if retry_key in self._achievement_retry_after:
            return None

        url = f"{self.steam_api_base}/ISteamUserStats/GetPlayerAchievements/v1/"
        params = {"key": api_key, "steamid": sid, "appid": gid, "l": "schinese"}
        max_attempts = min(self.retry_times, 3)
        failure = "unavailable"

        for attempt in range(max_attempts):
            try:
                client = await self._get_http_client(store=False)
                resp = await client.get(url, params=params, timeout=15.0)
                status = resp.status_code
                if status in (200, 400):
                    payload = resp.json()
                    stats = payload.get("playerstats") if isinstance(payload, dict) else None
                    if not isinstance(stats, dict):
                        stats = {}
                    if status == 200 and stats.get("success"):
                        achs = stats.get("achievements") or []
                        return {
                            str(a["apiname"])
                            for a in achs
                            if isinstance(a, dict) and a.get("apiname") and int(a.get("achieved") or 0) == 1
                        }
                    err_msg = str(stats.get("error") or "").lower()
                    if "no stats" in err_msg:
                        self._mark_no_achievement_app(gid)
                        logger.info("[SteamClient] appid=%s 无成就统计，加入本次运行免查列表", gid)
                        return None
                    failure = f"HTTP {status} ({stats.get('error') or 'success=false'})"
                    break
                if status in (401, 403):
                    return None
                failure = f"HTTP {status}"
                if status < 500:
                    break
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)

        self._cache_set(
            self._achievement_retry_after,
            retry_key,
            time.monotonic() + 300.0,
            MAX_ACHIEVEMENT_RETRY_CACHE,
        )
        logger.debug("[SteamClient] 获取玩家成就失败 (sid=%s, appid=%s): %s", sid, gid, failure)
        return None

    async def fetch_achievement_schema(
        self,
        appid: str,
        lang: str = "schinese",
    ) -> dict[str, dict[str, Any]]:
        """Fetch achievement schema (display_name, description, icon, global_percent) keyed by apiname."""
        gid = str(appid or "").strip()
        if not gid or not gid.isdigit() or gid in self._no_achievement_apps:
            return {}

        language = str(lang or "schinese").strip() or "schinese"
        cache_key = (gid, language)
        now = time.time()
        cached = self._achievement_schema_cache.get(cache_key)
        if cached and (now - cached[1]) < 86400.0:
            return cached[0]

        api_key = self.api_key
        if not api_key:
            return {}

        lang_candidates: list[str] = [language]
        for fallback_lang in ("schinese", "english"):
            if fallback_lang not in lang_candidates:
                lang_candidates.append(fallback_lang)

        def _to_icon_url(val: Any) -> str:
            s = str(val or "").strip()
            if not s:
                return ""
            if s.startswith(("http://", "https://")):
                return s
            return f"{STEAM_ACHIEVEMENT_ICON_BASE}/{gid}/{quote(s, safe='')}.jpg"

        percents: dict[str, float | None] = {}
        try:
            pct_url = f"{self.steam_api_base}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
            client = await self._get_http_client(store=False)
            resp_pct = await client.get(pct_url, params={"gameid": gid}, timeout=12.0)
            if resp_pct.status_code == 200:
                pct_data = resp_pct.json()
                pct_list = ((pct_data.get("achievementpercentages") or {}).get("achievements") or [])
                for item in pct_list:
                    if isinstance(item, dict) and item.get("name"):
                        percents[str(item["name"])] = _safe_float(item.get("percent"))
        except Exception as exc:
            logger.debug("[SteamClient] 获取成就全球解锁率失败 (appid=%s): %s", gid, exc)

        result: dict[str, dict[str, Any]] = {}
        schema_url = f"{self.steam_api_base}/ISteamUserStats/GetSchemaForGame/v2/"
        try:
            client = await self._get_http_client(store=False)
            for try_lang in lang_candidates:
                resp = await client.get(
                    schema_url,
                    params={"appid": gid, "key": api_key, "l": try_lang},
                    timeout=15.0,
                )
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                game_obj = payload.get("game") if isinstance(payload, dict) else None
                avail = game_obj.get("availableGameStats") if isinstance(game_obj, dict) else None
                achs = avail.get("achievements") if isinstance(avail, dict) else None
                if not isinstance(achs, list) or not achs:
                    continue
                parsed: dict[str, dict[str, Any]] = {}
                for ach in achs:
                    if not isinstance(ach, dict) or not ach.get("name"):
                        continue
                    apiname = str(ach["name"])
                    parsed[apiname] = {
                        "apiname": apiname,
                        "display_name": str(ach.get("displayName") or apiname),
                        "description": str(ach.get("description") or ""),
                        "icon": _to_icon_url(ach.get("icon")),
                        "icon_gray": _to_icon_url(ach.get("icongray")),
                        "global_percent": percents.get(apiname),
                    }
                if parsed:
                    result = parsed
                    if any(v["description"] for v in parsed.values()):
                        break
        except Exception as exc:
            logger.warning("[SteamClient] 获取成就 schema 失败 (appid=%s): %s", gid, exc)

        if result:
            self._cache_set(
                self._achievement_schema_cache,
                cache_key,
                (result, now),
                MAX_ACHIEVEMENT_SCHEMA_CACHE_SIZE,
            )
        return result

    # ------------------------------------------------------------------
    # Optional ITAD API helpers (used by PriceService when itad_api_key is set)
    # ------------------------------------------------------------------

    async def itad_lookup_appid(self, appid: str) -> dict[str, Any] | None:
        """Look up an ITAD game record by Steam appid."""
        key = self.itad_api_key
        gid = str(appid or "").strip()
        if not key or not gid:
            return None
        try:
            client = await self._get_http_client(store=False)
            resp = await self._request_same_origin(
                client,
                "GET",
                f"{self.itad_api_base}/games/lookup/v1",
                params={"appid": gid, "key": key},
                timeout=15.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            game = payload.get("game") if isinstance(payload, dict) else None
            if isinstance(game, dict) and game.get("id"):
                return {
                    "id": str(game["id"]),
                    "title": str(game.get("title") or ""),
                    "slug": str(game.get("slug") or ""),
                    "appid": gid,
                }
        except Exception as exc:
            logger.warning("[SteamClient] ITAD lookup 失败 (appid=%s): %s", gid, exc)
        return None

    async def itad_search_games(self, title: str, *, limit: int = 6) -> list[dict[str, Any]]:
        """Search ITAD catalog by title."""
        key = self.itad_api_key
        term = str(title or "").strip()
        if not key or not term:
            return []
        try:
            client = await self._get_http_client(store=False)
            resp = await self._request_same_origin(
                client,
                "GET",
                f"{self.itad_api_base}/games/search/v1",
                params={"title": term, "results": limit, "key": key},
                timeout=15.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                return []
            results: list[dict[str, Any]] = []
            for item in payload[:limit]:
                if not isinstance(item, dict):
                    continue
                game_id = str(item.get("id") or item.get("gameId") or "").strip()
                game_title = str(item.get("title") or item.get("name") or "").strip()
                if not game_id or not game_title:
                    continue
                assets = item.get("assets") if isinstance(item.get("assets"), dict) else {}
                image = str(assets.get("boxart") or assets.get("banner600") or assets.get("banner") or "")
                results.append(
                    {
                        "id": game_id,
                        "title": game_title,
                        "slug": str(item.get("slug") or ""),
                        "url": str(item.get("url") or ""),
                        "image": image,
                    }
                )
            return results
        except Exception as exc:
            logger.warning("[SteamClient] ITAD 搜索失败 (title=%s): %s", term, exc)
            return []

    async def itad_get_price_summary(self, game_id: str, country: str = "CN") -> dict[str, Any] | None:
        """Fetch ITAD v3 current deals + all-time historical low for an ITAD game_id."""
        key = self.itad_api_key
        if not key or not game_id:
            return None
        cc = str(country or "CN").strip().upper() or "CN"
        try:
            client = await self._get_http_client(store=False)
            resp = await self._request_same_origin(
                client,
                "POST",
                f"{self.itad_api_base}/games/prices/v3",
                json=[game_id],
                params={"country": cc, "key": key},
                timeout=20.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("[SteamClient] ITAD 查价失败 (game_id=%s, cc=%s): %s", game_id, cc, exc)
            return None

        entry: dict[str, Any] = {}
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("id") == game_id:
                    entry = item
                    break
        if not entry:
            return None

        current_price: float | None = None
        current_regular: float | None = None
        currency: str | None = None
        cut: int | None = None
        deal_url: str = ""
        steam_low: float | None = None
        steam_low_cut: int | None = None
        cdk_shop: str | None = None
        cdk_amount: float | None = None
        cdk_currency: str | None = None
        cdk_cut: int | None = None
        cdk_url: str = ""
        fallback_deal: tuple[float, float, str | None, int | None, str] | None = None

        for deal in entry.get("deals") or []:
            if not isinstance(deal, dict):
                continue
            price_obj = deal.get("price") or {}
            regular_obj = deal.get("regular") or {}
            amount = _safe_float(price_obj.get("amount"))
            if amount is None:
                continue
            regular_amt = _safe_float(regular_obj.get("amount"))
            if regular_amt is None:
                regular_amt = amount
            shop = deal.get("shop") or {}
            deal_cut = int(deal.get("cut") or 0)
            url_str = str(deal.get("url") or "")
            curr = str(price_obj.get("currency") or "").upper() or None

            if fallback_deal is None:
                fallback_deal = (amount, regular_amt, curr, deal_cut, url_str)

            if _is_steam_shop(shop):
                current_price = amount
                current_regular = regular_amt
                currency = curr
                cut = deal_cut
                deal_url = url_str
                store_low = _safe_float((deal.get("storeLow") or {}).get("amount"))
                if store_low is not None:
                    steam_low = store_low
                    if regular_amt and regular_amt > 0:
                        steam_low_cut = int(round((1.0 - store_low / regular_amt) * 100))
                    else:
                        steam_low_cut = deal_cut if store_low < amount else 0
            elif cdk_amount is None or amount < cdk_amount:
                cdk_amount = amount
                cdk_shop = str(shop.get("name") or "") or None
                cdk_currency = curr
                cdk_cut = deal_cut
                cdk_url = url_str

        if current_price is None and fallback_deal is not None:
            current_price, current_regular, currency, cut, deal_url = fallback_deal

        history_low: float | None = None
        history_low_cut: int | None = None
        low_obj = entry.get("historyLow")
        if isinstance(low_obj, dict):
            low_all = low_obj.get("all") or {}
            history_low = _safe_float(low_all.get("amount"))
            if not currency and low_all.get("currency"):
                currency = str(low_all.get("currency")).upper()
            if history_low is not None and current_regular and current_regular > 0:
                history_low_cut = int(round((1.0 - history_low / current_regular) * 100))

        return {
            "game_id": game_id,
            "country": cc,
            "current_price": current_price,
            "current_regular": current_regular,
            "currency": currency,
            "cut": cut,
            "deal_url": deal_url or cdk_url,
            "history_low": history_low,
            "history_low_cut": history_low_cut,
            "steam_low": steam_low,
            "steam_low_cut": steam_low_cut,
            "cdk_shop": cdk_shop,
            "cdk_amount": cdk_amount,
            "cdk_currency": cdk_currency,
            "cdk_cut": cdk_cut,
            "cdk_url": cdk_url,
        }
