"""Steam store game detail, price lookup, ITAD enrichment, and interactive candidate selection."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from html import unescape
from typing import Any

from astrbot.api import logger

from .steam_client import SteamClient, store_region_candidates

DEFAULT_PENDING_TTL_SECONDS = 180.0
MAX_PENDING_SELECTIONS = 256

# Cross-checked FX rates to CNY
CURRENCY_RATES: dict[str, float] = {
    "CNY": 1.0,
    "USD": 6.7254,
    "EUR": 7.8167,
    "JPY": 0.0438,
    "KRW": 0.005,
    "RUB": 0.078,
    "UAH": 0.1506,
    "TRY": 0.1392,
    "GBP": 9.1017,
    "PLN": 1.8211,
    "BRL": 1.3186,
    "INR": 0.0708,
    "HKD": 0.8576,
    "TWD": 0.2142,
    "SGD": 5.18,
}

CURRENCY_REGION: dict[str, str] = {
    "CNY": "CN",
    "JPY": "JP",
    "USD": "US",
    "EUR": "DE",
    "GBP": "GB",
    "KRW": "KR",
    "RUB": "RU",
    "UAH": "UA",
    "TRY": "TR",
    "PLN": "PL",
    "BRL": "BR",
    "INR": "IN",
    "HKD": "HK",
    "TWD": "TW",
    "SGD": "SG",
}

CURRENCY_SYMBOL: dict[str, str] = {
    "CNY": "¥",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "KRW": "₩",
    "RUB": "₽",
    "UAH": "₴",
    "TRY": "₺",
    "PLN": "zł",
    "BRL": "R$",
    "INR": "₹",
    "HKD": "HK$",
    "TWD": "NT$",
    "SGD": "S$",
}

COUNTRY_LABEL: dict[str, str] = {
    "CN": "国区",
    "RU": "俄区",
    "UA": "乌区",
    "US": "美区",
    "JP": "日区",
    "KR": "韩区",
    "TR": "土区",
    "GB": "英区",
    "DE": "德区",
    "PL": "波区",
    "HK": "港区",
    "TW": "台区",
    "MO": "澳区",
    "SG": "新区",
    "BR": "巴区",
    "IN": "印区",
}

_APPID_URL_RE = re.compile(
    r"(?:(?:store\.)?steampowered\.com/app/|steam://(?:store|run)/)(\d+)",
    re.IGNORECASE,
)


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def convert_currency(
    amount: float | None,
    from_currency: str | None,
    to_currency: str | None,
) -> float | None:
    """Convert monetary amount using CNY pivot table; returns original amount if rate unknown."""
    if amount is None:
        return None
    frm = str(from_currency or "").strip().upper()
    to = str(to_currency or "").strip().upper()
    if not frm or not to or frm == to:
        return round(float(amount), 2)
    f_rate = CURRENCY_RATES.get(frm)
    t_rate = CURRENCY_RATES.get(to)
    if not f_rate or not t_rate:
        return round(float(amount), 2)
    return round(float(amount) * f_rate / t_rate, 2)


def format_money(amount: float | None, currency: str | None = "CNY") -> str:
    """Format a price with currency symbol."""
    if amount is None:
        return ""
    curr = str(currency or "CNY").strip().upper() or "CNY"
    sym = CURRENCY_SYMBOL.get(curr, f"{curr} ")
    val = float(amount)
    if abs(val - round(val)) < 1e-6:
        return f"{sym}{int(round(val))}"
    return f"{sym}{val:.2f}"


def _clean_html_text(value: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", str(value or ""), flags=re.I | re.S)
    text = re.sub(r"<img[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _normalize_title(s: str) -> str:
    if not s:
        return ""
    repl = {
        "\x99": "™",
        "\x9c": "œ",
        "\x91": "‘",
        "\x92": "’",
        "\x93": "“",
        "\x94": "”",
        "\x85": "…",
        "\x96": "–",
        "\x97": "—",
    }
    s = "".join(repl.get(ch, ch) for ch in str(s))
    try:
        return unicodedata.normalize("NFKC", s).casefold().strip()
    except Exception:
        return str(s).casefold().strip()


def _query_tokens(query: str) -> list[str]:
    tokens = re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+", str(query or "").casefold())
    return [t for t in tokens if len(t) >= 2 or t.isdigit()]


def _token_in_title(token: str, haystack: str) -> bool:
    if re.fullmatch(r"[0-9a-z]+", token):
        return re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", haystack) is not None
    return token in haystack


def _title_matches_query(title: str, query: str) -> bool:
    tokens = _query_tokens(query)
    haystack = str(title or "").casefold()
    if not haystack:
        return False
    if not tokens:
        return True
    if haystack.strip() == str(query or "").casefold().strip():
        return True
    matched = sum(1 for t in tokens if _token_in_title(t, haystack))
    if len(tokens) == 1:
        return matched == 1
    return matched >= max(2, (len(tokens) + 1) // 2)


def _item_type_rank(item_type: str) -> int:
    return {
        "game": 0,
        "app": 0,
        "software": 1,
        "dlc": 2,
        "bundle": 3,
        "music": 4,
        "video": 5,
    }.get(str(item_type or "").lower(), 1)


def _rank_candidates(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q_fold = str(query or "").casefold().strip()

    def _sort_key(pair: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
        idx, item = pair
        title = str(item.get("title") or item.get("name") or "").casefold().strip()
        exact = 0 if title == q_fold else 1
        t_rank = _item_type_rank(str(item.get("type") or ""))
        extra_tokens = 0 if exact == 0 else max(0, len(_query_tokens(title)) - len(_query_tokens(query)))
        return (exact, t_rank, extra_tokens, idx)

    ranked = sorted(enumerate(items), key=_sort_key)
    return [item for _, item in ranked]


class PriceService:
    """Steam game detail, multi-region price comparison, optional ITAD historical low, and candidate selection."""

    def __init__(
        self,
        steam_client: SteamClient,
        config_getter: Callable[[], dict[str, Any]],
        llm_translate_fn: Callable[[str], Awaitable[str]] | None = None,
        *,
        pending_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS,
    ) -> None:
        self._client = steam_client
        self._config_getter = config_getter
        self._llm_translate_fn = llm_translate_fn
        self._pending_ttl_seconds = max(0.01, float(pending_ttl_seconds))
        self._pending: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _cfg(self) -> dict[str, Any]:
        try:
            cfg = self._config_getter()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _price_settings(self) -> tuple[str, str, list[str]]:
        cfg = self._cfg()
        currency = str(cfg.get("price_currency") or "CNY").strip().upper() or "CNY"
        region = str(cfg.get("price_region") or "").strip().upper()
        if not region:
            region = CURRENCY_REGION.get(currency, "CN")

        raw_compare = cfg.get("price_compare_regions", "UA,US,JP")
        if isinstance(raw_compare, (list, tuple)):
            parts = [str(x).strip().upper() for x in raw_compare]
        else:
            parts = [p.strip().upper() for p in re.split(r"[,，\s]+", str(raw_compare or ""))]

        compare_regions: list[str] = []
        for code in parts:
            if not code or code == "NONE" or code == region or code in compare_regions:
                continue
            compare_regions.append(code)
        return currency, region, compare_regions

    # ------------------------------------------------------------------
    # Input parsing & pending interactive selection state
    # ------------------------------------------------------------------

    @staticmethod
    def extract_appid(raw: str) -> str | None:
        """Extract Steam appid from a plain numeric ID, store URL, or steam:// URI."""
        s = str(raw or "").strip()
        if not s:
            return None
        if s.isdigit():
            return s
        match = _APPID_URL_RE.search(s)
        return match.group(1) if match else None

    def _purge_expired_pending(self) -> None:
        now = time.monotonic()
        expired = [k for k, (deadline, _) in self._pending.items() if deadline <= now]
        for k in expired:
            self._pending.pop(k, None)

    def has_pending_selection(self, session_key: str) -> bool:
        """Return True if `session_key` has an unexpired interactive candidate list."""
        self._purge_expired_pending()
        key = str(session_key or "").strip()
        entry = self._pending.get(key)
        return bool(entry and entry[1])

    def get_pending_candidates(self, session_key: str) -> list[dict[str, Any]]:
        """Return currently pending candidates for `session_key`, or empty list if none/expired."""
        self._purge_expired_pending()
        key = str(session_key or "").strip()
        entry = self._pending.get(key)
        return list(entry[1]) if entry else []

    def clear_pending(self, session_key: str) -> None:
        """Clear pending candidate selection for `session_key`."""
        key = str(session_key or "").strip()
        self._pending.pop(key, None)

    def select_candidate(self, session_key: str, index: int) -> dict[str, Any] | None:
        """Select a 1-based candidate index for `session_key` and clear pending state if valid."""
        self._purge_expired_pending()
        key = str(session_key or "").strip()
        entry = self._pending.get(key)
        if not entry:
            return None
        _, candidates = entry
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return None
        if 1 <= idx <= len(candidates):
            selected = dict(candidates[idx - 1])
            self._pending.pop(key, None)
            return selected
        return None

    @staticmethod
    def format_candidates_prompt(candidates: list[dict[str, Any]]) -> str:
        """Format a user-friendly numbered candidate list message."""
        lines = ["找到多个匹配游戏，请直接回复序号选择："]
        for idx, item in enumerate(candidates, 1):
            title = item.get("title") or item.get("en_title") or f"App {item.get('appid', '')}"
            appid = item.get("appid")
            suffix = f" (AppID: {appid})" if appid else ""
            lines.append(f"{idx}. {title}{suffix}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Search & candidate resolution
    # ------------------------------------------------------------------

    async def _search_steam_candidates(
        self,
        query: str,
        region: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        fetch_limit = max(limit * 2, 10)
        zh_items, en_items = await asyncio.gather(
            self._client.search_steam_store(query, country=region, lang="schinese", limit=fetch_limit),
            self._client.search_steam_store(query, country=region, lang="english", limit=fetch_limit),
        )
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        en_name_by_appid = {
            str(it.get("appid") or it.get("id") or ""): str(it.get("name") or "")
            for it in en_items
            if isinstance(it, dict)
        }

        for raw_list in (zh_items, en_items):
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                appid = str(item.get("appid") or item.get("id") or "").strip()
                title = str(item.get("name") or "").strip()
                if not appid or not title or appid in seen:
                    continue
                en_title = en_name_by_appid.get(appid, "")
                # Accept if either localized or English title matches query, or non-DLC localized hit
                matched = (
                    _title_matches_query(title, query)
                    or (en_title and _title_matches_query(en_title, query))
                    or (
                        contains_chinese(title)
                        and not contains_chinese(query)
                        and str(item.get("type") or "").lower() not in {"dlc", "bundle", "music", "video"}
                    )
                )
                if not matched:
                    continue
                seen.add(appid)
                merged.append(
                    {
                        "appid": appid,
                        "title": title,
                        "en_title": en_title or title,
                        "itad_id": "",
                        "image": str(item.get("tiny_image") or self._client.default_game_cover_url(appid)),
                        "type": str(item.get("type") or "app"),
                    }
                )

        # If strict token filtering dropped everything, fall back to raw top Steam storesearch hits
        if not merged and (zh_items or en_items):
            for item in (zh_items or en_items)[:limit]:
                appid = str(item.get("appid") or item.get("id") or "").strip()
                title = str(item.get("name") or "").strip()
                if not appid or not title or appid in seen:
                    continue
                seen.add(appid)
                merged.append(
                    {
                        "appid": appid,
                        "title": title,
                        "en_title": en_name_by_appid.get(appid, title),
                        "itad_id": "",
                        "image": str(item.get("tiny_image") or self._client.default_game_cover_url(appid)),
                        "type": str(item.get("type") or "app"),
                    }
                )

        return _rank_candidates(merged, query)[:limit]

    async def search_candidates(
        self,
        query: str,
        session_key: str | None = None,
        *,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Search Steam Store (with optional LLM translation and ITAD fallback).

        If `session_key` is provided and multiple candidates are found, caches them
        with TTL so `select_candidate(session_key, index)` can resolve numeric replies.
        """
        raw = str(query or "").strip()
        if not raw:
            return []

        _, region, _ = self._price_settings()

        # 1) Direct AppID / Steam Store URL
        direct_appid = self.extract_appid(raw)
        if direct_appid:
            zh_name, en_name = await self._client.get_game_names(direct_appid, fallback=f"App {direct_appid}")
            itad_id = ""
            if self._client.itad_api_key:
                itad_info = await self._client.itad_lookup_appid(direct_appid)
                if itad_info:
                    itad_id = itad_info.get("id", "")
            candidate = {
                "appid": direct_appid,
                "title": zh_name,
                "en_title": en_name,
                "itad_id": itad_id,
                "image": self._client.default_game_cover_url(direct_appid),
                "type": "app",
            }
            if session_key:
                self.clear_pending(session_key)
            return [candidate]

        # 2) Steam Store search
        candidates = await self._search_steam_candidates(raw, region, limit)

        # 3) Optional LLM Chinese -> English translation retry
        if not candidates and self._llm_translate_fn is not None and contains_chinese(raw):
            try:
                translated = str(await self._llm_translate_fn(raw) or "").strip()
            except Exception as exc:
                logger.warning("[PriceService] LLM 译名失败 (query=%s): %s", raw, exc)
                translated = ""
            if translated and translated != raw:
                candidates = await self._search_steam_candidates(translated, region, limit)

        # 4) Optional ITAD fallback search if Steam search yielded nothing
        if not candidates and self._client.itad_api_key:
            itad_hits = await self._client.itad_search_games(raw, limit=limit)
            for hit in itad_hits:
                title = hit.get("title", "")
                if not title:
                    continue
                steam_match = await self._client.search_steam_store(title, country=region, lang="english", limit=3)
                appid = str(steam_match[0].get("appid") or "") if steam_match else ""
                candidates.append(
                    {
                        "appid": appid,
                        "title": title,
                        "en_title": title,
                        "itad_id": hit.get("id", ""),
                        "image": hit.get("image") or (self._client.default_game_cover_url(appid) if appid else ""),
                        "type": "app",
                    }
                )

        candidates = candidates[:limit]
        if session_key:
            key = str(session_key).strip()
            if len(candidates) > 1 and key:
                self._purge_expired_pending()
                self._pending.pop(key, None)
                while len(self._pending) >= MAX_PENDING_SELECTIONS:
                    oldest_key = next(iter(self._pending))
                    self._pending.pop(oldest_key, None)
                self._pending[key] = (
                    time.monotonic() + self._pending_ttl_seconds,
                    list(candidates),
                )
            elif key:
                self._pending.pop(key, None)

        return candidates

    # ------------------------------------------------------------------
    # Card DTO builders
    # ------------------------------------------------------------------

    async def _fetch_itad_enrichment(
        self,
        *,
        appid: str,
        en_name: str,
        itad_id: str,
        primary_region: str,
        target_currency: str,
    ) -> dict[str, Any] | None:
        if not self._client.itad_api_key:
            return None

        resolved_id = str(itad_id or "").strip()
        if not resolved_id and appid:
            looked = await self._client.itad_lookup_appid(appid)
            if looked:
                resolved_id = str(looked.get("id") or "")
        if not resolved_id and en_name:
            hits = await self._client.itad_search_games(en_name, limit=3)
            norm = _normalize_title(en_name)
            matched = next((h for h in hits if _normalize_title(h.get("title", "")) == norm), None)
            if matched:
                resolved_id = str(matched.get("id") or "")
        if not resolved_id:
            return None

        summary = await self._client.itad_get_price_summary(resolved_id, country=primary_region)
        if not summary or summary.get("current_price") is None:
            for fb_cc in store_region_candidates(primary_region)[1:]:
                fb_summary = await self._client.itad_get_price_summary(resolved_id, country=fb_cc)
                if fb_summary and fb_summary.get("current_price") is not None:
                    summary = fb_summary
                    break
        if not summary:
            return None

        src_curr = summary.get("currency")
        out_curr = target_currency if (src_curr in CURRENCY_RATES and target_currency in CURRENCY_RATES) else (src_curr or target_currency)

        def _conv(val: Any, from_c: str | None = src_curr) -> float | None:
            if val is None:
                return None
            return convert_currency(float(val), from_c, out_curr)

        current_price = _conv(summary.get("current_price"))
        current_regular = _conv(summary.get("current_regular"))
        history_low = _conv(summary.get("history_low"))
        steam_low = _conv(summary.get("steam_low"))
        cdk_src_curr = summary.get("cdk_currency") or src_curr
        cdk_amount = _conv(summary.get("cdk_amount"), cdk_src_curr)

        return {
            "game_id": resolved_id,
            "country": summary.get("country") or primary_region,
            "currency": out_curr,
            "currency_symbol": CURRENCY_SYMBOL.get(out_curr, out_curr),
            "current_price": current_price,
            "current_price_formatted": format_money(current_price, out_curr),
            "current_regular": current_regular,
            "current_regular_formatted": format_money(current_regular, out_curr),
            "cut": int(summary.get("cut") or 0),
            "history_low": history_low,
            "history_low_cut": summary.get("history_low_cut"),
            "history_low_formatted": format_money(history_low, out_curr),
            "steam_low": steam_low,
            "steam_low_cut": summary.get("steam_low_cut"),
            "steam_low_formatted": format_money(steam_low, out_curr),
            "cdk_shop": summary.get("cdk_shop"),
            "cdk_amount": cdk_amount,
            "cdk_formatted": format_money(cdk_amount, out_curr),
            "cdk_cut": summary.get("cdk_cut"),
            "deal_url": summary.get("deal_url") or "",
        }

    def _assemble_card_dict(
        self,
        *,
        appid: str,
        fallback_title: str,
        detail: dict[str, Any] | None,
        reviews: dict[str, dict[str, Any] | None] | None,
        online_count: int | None,
        primary_region: str,
        target_currency: str,
        region_prices: list[dict[str, Any]],
        itad_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        detail = detail or {}
        name = str(detail.get("name") or fallback_title or (f"App {appid}" if appid else "未知游戏")).strip()
        header_image = str(
            detail.get("header_image")
            or (self._client.default_game_cover_url(appid) if appid else "")
        )
        short_desc = _clean_html_text(detail.get("short_description") or "")
        release_obj = detail.get("release_date") if isinstance(detail.get("release_date"), dict) else {}
        release_date = str(release_obj.get("date") or "未知")
        developers = [str(d) for d in (detail.get("developers") or []) if d]
        publishers = [str(p) for p in (detail.get("publishers") or []) if p]
        genres = [
            str(g.get("description"))
            for g in (detail.get("genres") or [])
            if isinstance(g, dict) and g.get("description")
        ]
        categories = [
            str(c.get("description"))
            for c in (detail.get("categories") or [])
            if isinstance(c, dict) and c.get("description")
        ]
        tags = (genres + [c for c in categories if c not in genres])[:6]

        is_free = bool(detail.get("is_free", False))
        po = detail.get("price_overview") if isinstance(detail.get("price_overview"), dict) else {}

        # Primary current price (prefer Steam primary region price, then appdetails, then ITAD)
        primary_row = next((r for r in region_prices if r.get("region") == primary_region), None)
        price_info: dict[str, Any] = {
            "is_free": is_free,
            "currency": target_currency,
            "currency_symbol": CURRENCY_SYMBOL.get(target_currency, target_currency),
            "current": None,
            "regular": None,
            "cut": 0,
            "formatted": "免费游玩" if is_free else "",
            "regular_formatted": "",
            "region": primary_region,
        }
        if is_free:
            price_info.update({"current": 0.0, "regular": 0.0, "cut": 0, "formatted": "免费游玩"})
        elif primary_row and primary_row.get("converted_price") is not None:
            curr = primary_row.get("converted_currency") or target_currency
            price_info.update(
                {
                    "currency": curr,
                    "currency_symbol": CURRENCY_SYMBOL.get(curr, curr),
                    "current": primary_row["converted_price"],
                    "regular": primary_row.get("converted_regular") or primary_row["converted_price"],
                    "cut": int(primary_row.get("cut") or 0),
                    "formatted": primary_row.get("converted_formatted") or format_money(primary_row["converted_price"], curr),
                    "regular_formatted": format_money(primary_row.get("converted_regular"), curr),
                    "region": primary_region,
                }
            )
        elif po:
            po_curr = str(po.get("currency") or target_currency).upper()
            final_v = round((po.get("final") or 0) / 100.0, 2)
            init_v = round((po.get("initial") or po.get("final") or 0) / 100.0, 2)
            conv_final = convert_currency(final_v, po_curr, target_currency)
            conv_init = convert_currency(init_v, po_curr, target_currency)
            out_curr = target_currency if (po_curr in CURRENCY_RATES and target_currency in CURRENCY_RATES) else po_curr
            price_info.update(
                {
                    "currency": out_curr,
                    "currency_symbol": CURRENCY_SYMBOL.get(out_curr, out_curr),
                    "current": conv_final,
                    "regular": conv_init,
                    "cut": int(po.get("discount_percent") or 0),
                    "formatted": str(po.get("final_formatted") or format_money(conv_final, out_curr)),
                    "regular_formatted": format_money(conv_init, out_curr),
                    "region": str(detail.get("_store_region") or primary_region),
                }
            )
        elif itad_data and itad_data.get("current_price") is not None:
            curr = itad_data.get("currency") or target_currency
            price_info.update(
                {
                    "currency": curr,
                    "currency_symbol": CURRENCY_SYMBOL.get(curr, curr),
                    "current": itad_data["current_price"],
                    "regular": itad_data.get("current_regular") or itad_data["current_price"],
                    "cut": int(itad_data.get("cut") or 0),
                    "formatted": itad_data.get("current_price_formatted") or format_money(itad_data["current_price"], curr),
                    "regular_formatted": itad_data.get("current_regular_formatted") or "",
                    "region": str(itad_data.get("country") or primary_region),
                }
            )

        actual_store_region = str(detail.get("_store_region") or "").upper()
        region_locked = False
        if not is_free and primary_row is None:
            if (actual_store_region and actual_store_region != primary_region) or bool(region_prices):
                region_locked = True
        region_label = COUNTRY_LABEL.get(primary_region, primary_region)
        region_locked_note = f"当前游戏锁{region_label}" if region_locked else ""

        store_url = f"https://store.steampowered.com/app/{appid}/" if appid else ""
        review_all = (reviews or {}).get("all") or {}
        review_zh = (reviews or {}).get("schinese") or {}

        return {
            "appid": appid,
            "name": name,
            "header_image": header_image,
            "short_description": short_desc,
            "release_date": release_date,
            "developers": developers,
            "publishers": publishers,
            "genres": genres,
            "tags": tags,
            "review_all": review_all,
            "review_schinese": review_zh,
            "online_count": online_count,
            "is_free": is_free,
            "price": price_info,
            "itad": itad_data,
            "region_prices": region_prices,
            "store_url": store_url,
            "region_locked": region_locked,
            "region_locked_note": region_locked_note,
            "currency": target_currency,
            "currency_symbol": CURRENCY_SYMBOL.get(target_currency, target_currency),
            "primary_region": primary_region,
            "primary_region_label": region_label,
        }

    async def build_game_detail_card(self, appid: str) -> dict[str, Any] | None:
        """Build a Steam Store game detail card DTO for `/steam game <appid>`."""
        gid = self.extract_appid(appid)
        if not gid:
            return None

        target_currency, primary_region, _ = self._price_settings()
        detail, reviews, online_count = await asyncio.gather(
            self._client.fetch_store_appdetails(gid, country=primary_region, lang="schinese"),
            self._client.fetch_game_reviews(gid),
            self._client.get_game_online_count(gid),
        )
        if not detail:
            return None

        return self._assemble_card_dict(
            appid=gid,
            fallback_title=str(detail.get("name") or f"App {gid}"),
            detail=detail,
            reviews=reviews,
            online_count=online_count,
            primary_region=primary_region,
            target_currency=target_currency,
            region_prices=[],
            itad_data=None,
        )

    async def build_price_card(
        self,
        candidate_or_query: dict[str, Any] | str,
        *,
        include_itad: bool = True,
    ) -> dict[str, Any] | None:
        """Build a full price card DTO (Steam Store + multi-region comparison + optional ITAD historical low)."""
        candidate: dict[str, Any] | None = None
        if isinstance(candidate_or_query, dict):
            candidate = dict(candidate_or_query)
        else:
            raw = str(candidate_or_query or "").strip()
            if not raw:
                return None
            candidates = await self.search_candidates(raw, session_key=None, limit=1)
            if not candidates:
                return None
            candidate = candidates[0]

        appid = str(candidate.get("appid") or "").strip()
        title = str(candidate.get("title") or candidate.get("en_title") or "").strip()
        en_title = str(candidate.get("en_title") or title).strip()
        itad_id = str(candidate.get("itad_id") or "").strip()

        target_currency, primary_region, compare_regions = self._price_settings()
        region_codes = [primary_region, *compare_regions]

        detail_task = (
            self._client.fetch_store_appdetails(appid, country=primary_region, lang="schinese")
            if appid
            else asyncio.sleep(0, result=None)
        )
        reviews_task = (
            self._client.fetch_game_reviews(appid)
            if appid
            else asyncio.sleep(0, result={"all": None, "schinese": None})
        )
        online_task = (
            self._client.get_game_online_count(appid)
            if appid
            else asyncio.sleep(0, result=None)
        )
        region_tasks = [
            self._client.fetch_region_price(appid, code, allow_fallback=False)
            for code in region_codes
        ] if appid else []
        itad_task = (
            self._fetch_itad_enrichment(
                appid=appid,
                en_name=en_title,
                itad_id=itad_id,
                primary_region=primary_region,
                target_currency=target_currency,
            )
            if include_itad and self._client.itad_api_key
            else asyncio.sleep(0, result=None)
        )

        gathered = await asyncio.gather(
            detail_task,
            reviews_task,
            online_task,
            itad_task,
            *region_tasks,
            return_exceptions=True,
        )

        detail = gathered[0] if isinstance(gathered[0], dict) else None
        reviews = gathered[1] if isinstance(gathered[1], dict) else None
        online_count = gathered[2] if isinstance(gathered[2], int) else None
        itad_data = gathered[3] if isinstance(gathered[3], dict) else None
        raw_region_results = gathered[4:]

        if detail is None and itad_data is None:
            return None

        region_prices: list[dict[str, Any]] = []
        for code, res in zip(region_codes, raw_region_results):
            if not isinstance(res, dict):
                continue
            orig_curr = res.get("currency")
            orig_price = res.get("current_price")
            orig_regular = res.get("current_regular")
            is_free_r = bool(res.get("is_free", False))
            if is_free_r:
                region_prices.append(
                    {
                        "region": code,
                        "label": COUNTRY_LABEL.get(code, code),
                        "is_free": True,
                        "currency": orig_curr,
                        "original_price": 0.0,
                        "original_formatted": "免费游玩",
                        "converted_price": 0.0,
                        "converted_regular": 0.0,
                        "converted_currency": target_currency,
                        "converted_formatted": "免费游玩",
                        "cut": 0,
                    }
                )
                continue
            conv_curr = target_currency if (orig_curr in CURRENCY_RATES and target_currency in CURRENCY_RATES) else (orig_curr or target_currency)
            conv_price = convert_currency(orig_price, orig_curr, conv_curr)
            conv_reg = convert_currency(orig_regular, orig_curr, conv_curr)
            region_prices.append(
                {
                    "region": code,
                    "label": COUNTRY_LABEL.get(code, code),
                    "is_free": False,
                    "currency": orig_curr,
                    "original_price": orig_price,
                    "original_formatted": res.get("formatted") or format_money(orig_price, orig_curr),
                    "converted_price": conv_price,
                    "converted_regular": conv_reg,
                    "converted_currency": conv_curr,
                    "converted_formatted": format_money(conv_price, conv_curr),
                    "cut": int(res.get("cut") or 0),
                }
            )

        return self._assemble_card_dict(
            appid=appid,
            fallback_title=title or en_title,
            detail=detail,
            reviews=reviews,
            online_count=online_count,
            primary_region=primary_region,
            target_currency=target_currency,
            region_prices=region_prices,
            itad_data=itad_data,
        )
