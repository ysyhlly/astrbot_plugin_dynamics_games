"""Idempotent, read-only migration from legacy `steam_status_monitor_V3` data and config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from astrbot.api import logger

from .state_machine import split_interval_by_natural_day
from .store import SteamMonitorStore

MIGRATION_KV_KEY = "legacy_migrated_v1"
MIGRATION_SUMMARY_KV_KEY = "legacy_migrated_v1_summary"

LEGACY_CONFIG_FIELDS: tuple[str, ...] = (
    "steam_api_key",
    "itad_api_key",
    "sgdb_api_key",
    "enable_proxy",
    "proxy_url",
    "steam_api_base",
    "steam_store_base",
    "sgdb_api_base",
    "itad_api_base",
    "game_filter_mode",
    "game_filter_ids",
    "rank_push_hour",
    "rank_push_minute",
    "price_currency",
    "price_region",
    "price_compare_regions",
    "ssl_ca_file",
)

_INT_CONFIG_FIELDS = frozenset({"rank_push_hour", "rank_push_minute"})
_BOOL_CONFIG_FIELDS = frozenset({"enable_proxy"})


def _read_json_readonly(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning("Failed to read legacy JSON file %s: %s", path, exc)
        return None


def _discover_legacy_data_dir(store: SteamMonitorStore) -> Path | None:
    """Locate legacy `steam_status_monitor` or `plugin_data/steam_status_monitor_V3` without modifying anything."""
    candidates: list[Path] = []
    # Live layout: <root>/data/steam_status_monitor (used by steam_status_monitor_V3)
    # Typical layout: <root>/data/plugin_data/astrbot_plugin_dynamics_games/steam_monitor.db
    for parent in store.path.resolve().parents:
        if parent.name == "plugin_data":
            candidates.append(parent / "steam_status_monitor_V3")
            candidates.append(parent.parent / "steam_status_monitor")
            break
        if parent.name == "data":
            candidates.append(parent / "steam_status_monitor")
            candidates.append(parent / "plugin_data" / "steam_status_monitor_V3")
            break
    candidates.append(store.path.resolve().parent.parent / "steam_status_monitor_V3")
    candidates.append(Path.cwd() / "data" / "steam_status_monitor")
    candidates.append(Path.cwd() / "data" / "plugin_data" / "steam_status_monitor_V3")

    seen: set[Path] = set()
    for cand in candidates:
        resolved = cand.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            return resolved
    return None


def _discover_legacy_config_path(
    store: SteamMonitorStore, legacy_data_dir: Path | None
) -> Path | None:
    """Locate legacy AstrBot plugin config JSON for `steam_status_monitor_V3`."""
    roots: list[Path] = []
    for base in (legacy_data_dir, store.path.resolve().parent):
        if base is None:
            continue
        for parent in base.resolve().parents:
            if parent.name == "data":
                roots.append(parent / "config")
                break
    roots.append(Path.cwd() / "data" / "config")

    filenames = (
        "steam_status_monitor_V3_config.json",
        "astrbot_plugin_steam_status_monitor_V3_config.json",
    )
    seen: set[Path] = set()
    for cfg_dir in roots:
        for fname in filenames:
            cand = (cfg_dir / fname).resolve()
            if cand in seen:
                continue
            seen.add(cand)
            if cand.is_file():
                return cand
    return None


def _is_value_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _extract_raw_user_section(config_section: Any) -> Mapping[str, Any] | None:
    parent = getattr(config_section, "parent", None)
    key = getattr(config_section, "key", None)
    if isinstance(parent, Mapping) and isinstance(key, str):
        raw = parent.get(key)
        return raw if isinstance(raw, Mapping) else {}
    return None


def _normalize_legacy_config_value(field: str, raw: Any) -> tuple[bool, Any]:
    if raw is None:
        return False, None
    if field in _BOOL_CONFIG_FIELDS:
        if isinstance(raw, bool):
            return True, raw
        if isinstance(raw, (int, float)):
            return True, bool(raw)
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True, True
            if low in ("false", "0", "no", "off"):
                return True, False
        return False, None
    if field in _INT_CONFIG_FIELDS:
        if isinstance(raw, bool):
            return False, None
        try:
            return True, int(raw)
        except (TypeError, ValueError):
            return False, None
    if isinstance(raw, str):
        clean = raw.strip()
        return (True, clean) if clean else (False, None)
    return (True, raw) if not _is_value_empty(raw) else (False, None)


def _backfill_config_from_legacy(
    config_section: Any,
    legacy_cfg: Mapping[str, Any],
) -> list[str]:
    if config_section is None or not isinstance(legacy_cfg, Mapping):
        return []

    raw_user_section = _extract_raw_user_section(config_section)
    had_proxy_url = not _is_value_empty(config_section.get("proxy_url"))
    backfilled: list[str] = []

    for field in LEGACY_CONFIG_FIELDS:
        if field not in legacy_cfg:
            continue
        ok, legacy_val = _normalize_legacy_config_value(field, legacy_cfg.get(field))
        if not ok:
            continue

        in_section = field in config_section
        current_val = config_section.get(field)

        if raw_user_section is not None:
            # ConfigSection backed by parent config: distinguish explicit user values from defaults
            user_set = field in raw_user_section and not _is_value_empty(
                raw_user_section.get(field)
            )
            if user_set:
                continue
            should_fill = (field not in raw_user_section) or _is_value_empty(current_val)
            if field in _BOOL_CONFIG_FIELDS and not should_fill:
                if current_val is False and legacy_val is True and not had_proxy_url:
                    should_fill = True
        else:
            # Plain dict-like mapping: only backfill missing/empty entries
            if field in _BOOL_CONFIG_FIELDS:
                should_fill = (not in_section) or (current_val is None) or (
                    current_val is False and legacy_val is True and not had_proxy_url
                )
            elif field in _INT_CONFIG_FIELDS:
                should_fill = (
                    (not in_section)
                    or (current_val is None)
                    or (isinstance(current_val, str) and not current_val.strip())
                )
            else:
                should_fill = (not in_section) or _is_value_empty(current_val)

        if should_fill:
            config_section[field] = legacy_val
            backfilled.append(field)

    if backfilled and hasattr(config_section, "save_config"):
        config_section.save_config()
    return backfilled


def _extract_game_name(raw: Any, fallback: str) -> str:
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    text = str(raw or "").strip()
    return text or fallback


def migrate_from_legacy_plugin(
    store: SteamMonitorStore,
    config_section: Any,
    *,
    legacy_data_dir: Path | None = None,
    legacy_config_path: Path | None = None,
) -> dict[str, Any]:
    """Perform one-time idempotent read-only migration from `steam_status_monitor_V3`.

    Never mutates or deletes any legacy file on disk.
    """
    if store.get_kv(MIGRATION_KV_KEY) == "1":
        return {
            "migrated": False,
            "already_migrated": True,
            "counts": {},
            "backfilled_config_fields": [],
        }

    data_dir = (
        Path(legacy_data_dir)
        if legacy_data_dir is not None
        else _discover_legacy_data_dir(store)
    )
    cfg_path = (
        Path(legacy_config_path)
        if legacy_config_path is not None
        else _discover_legacy_config_path(store, data_dir)
    )

    counts: dict[str, int] = {
        "group_players": 0,
        "bindings": 0,
        "push_routes": 0,
        "notify_sessions": 0,
        "rank_push_groups": 0,
        "group_switches": 0,
        "play_sessions": 0,
        "legacy_daily_rows": 0,
    }
    backfilled_fields: list[str] = []

    if data_dir is not None and data_dir.is_dir():
        # 1. Group monitored SteamIDs (support both legacy steam_groups.json and group_steam_ids.json)
        for group_file in ("steam_groups.json", "group_steam_ids.json"):
            raw_groups = _read_json_readonly(data_dir / group_file)
            if isinstance(raw_groups, Mapping):
                for gid, sids in raw_groups.items():
                    clean_gid = str(gid).strip()
                    if not clean_gid or not isinstance(sids, (list, tuple, set)):
                        continue
                    for sid in sids:
                        clean_sid = str(sid).strip()
                        if clean_sid and store.add_group_player(clean_gid, clean_sid):
                            counts["group_players"] += 1

        # 2. QQ <-> Steam bindings (bind_data.json)
        raw_binds = _read_json_readonly(data_dir / "bind_data.json")
        if isinstance(raw_binds, Mapping):
            for key, info in raw_binds.items():
                if not isinstance(info, Mapping):
                    continue
                sid = str(info.get("sid") or info.get("steam_id") or "").strip()
                if not sid:
                    continue
                key_str = str(key).strip()
                qq_id = "" if key_str.startswith("__remark:") else key_str
                nick_raw = info.get("nickname")
                nick = "" if nick_raw in (None, "", "*") else str(nick_raw).strip()
                store.upsert_binding(
                    sid,
                    qq_id=qq_id if qq_id else None,
                    nickname=nick if nick else None,
                )
                counts["bindings"] += 1

        # 3. Cross-group push routes (push_groups.json)
        raw_push = _read_json_readonly(data_dir / "push_groups.json")
        if isinstance(raw_push, Mapping):
            for sid, targets in raw_push.items():
                clean_sid = str(sid).strip()
                if not clean_sid or not isinstance(targets, (list, tuple, set)):
                    continue
                for target_gid in targets:
                    clean_gid = str(target_gid).strip()
                    if clean_gid and store.add_push_route(clean_sid, clean_gid):
                        counts["push_routes"] += 1

        # 4. Notify sessions / UMOs (notify_sessions.json)
        raw_notify = _read_json_readonly(data_dir / "notify_sessions.json")
        if isinstance(raw_notify, Mapping):
            for gid, umo in raw_notify.items():
                clean_gid = str(gid).strip()
                clean_umo = str(umo or "").strip()
                if clean_gid and clean_umo:
                    store.set_notify_session(clean_gid, clean_umo)
                    counts["notify_sessions"] += 1

        # 5. Daily rank push groups (rank_push_groups.json)
        raw_rank = _read_json_readonly(data_dir / "rank_push_groups.json")
        rank_groups: list[str] = []
        rank_all = False
        if isinstance(raw_rank, Mapping):
            raw_list = raw_rank.get("groups", [])
            if isinstance(raw_list, (list, tuple, set)):
                rank_groups = [str(g).strip() for g in raw_list if str(g).strip()]
            rank_all = bool(raw_rank.get("all", False))
        elif isinstance(raw_rank, (list, tuple, set)):
            rank_groups = [str(g).strip() for g in raw_rank if str(g).strip()]
        for gid in rank_groups:
            store.upsert_group_settings(
                gid,
                rank_push_enabled=True,
                rank_push_global=rank_all,
            )
            counts["rank_push_groups"] += 1

        # 6. Per-group monitor / achievement switches (group_switches.json)
        raw_switches = _read_json_readonly(data_dir / "group_switches.json")
        if isinstance(raw_switches, Mapping):
            monitor_map = raw_switches.get("monitor")
            ach_map = raw_switches.get("achievement")
            touched_groups: set[str] = set()
            if isinstance(monitor_map, Mapping):
                for gid, enabled in monitor_map.items():
                    clean_gid = str(gid).strip()
                    if clean_gid:
                        store.upsert_group_settings(
                            clean_gid, monitor_enabled=bool(enabled)
                        )
                        touched_groups.add(clean_gid)
            if isinstance(ach_map, Mapping):
                for gid, enabled in ach_map.items():
                    clean_gid = str(gid).strip()
                    if clean_gid:
                        store.upsert_group_settings(
                            clean_gid, achievement_enabled=bool(enabled)
                        )
                        touched_groups.add(clean_gid)
            counts["group_switches"] = len(touched_groups)

        # 7. Historical session records (session_records.json)
        # Track imported seconds per (day_key, sid, game_id) to avoid double-counting play_records.json
        session_covered: dict[tuple[str, str, str], float] = {}
        raw_sessions = _read_json_readonly(data_dir / "session_records.json")
        session_entries: list[tuple[str, Mapping[str, Any]]] = []
        if isinstance(raw_sessions, Mapping):
            for sid, items in raw_sessions.items():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, Mapping):
                            session_entries.append((str(sid).strip(), item))
        elif isinstance(raw_sessions, list):
            for item in raw_sessions:
                if isinstance(item, Mapping):
                    sid = str(item.get("steam_id") or item.get("sid") or "").strip()
                    if sid:
                        session_entries.append((sid, item))

        for sid, item in session_entries:
            gid = str(item.get("gameid") or item.get("game_id") or "").strip()
            if not sid or not gid:
                continue
            try:
                start_ts = float(item.get("start_time") or item.get("started_at") or 0.0)
                end_ts = float(item.get("end_time") or item.get("ended_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if start_ts <= 0.0 or end_ts <= start_ts:
                continue
            dur_sec = end_ts - start_ts
            gname = _extract_game_name(
                item.get("game_name") or item.get("name"), gid
            )
            row_id = store.insert_play_session(
                sid,
                gid,
                gname,
                start_ts,
                end_ts,
                duration_sec=dur_sec,
                source="legacy_session",
            )
            if row_id > 0:
                counts["play_sessions"] += 1
            for day_k, seg_sec in split_interval_by_natural_day(start_ts, end_ts):
                key_tuple = (day_k, sid, gid)
                session_covered[key_tuple] = session_covered.get(key_tuple, 0.0) + seg_sec

        # 8. Historical daily summaries (play_records.json)
        raw_play = _read_json_readonly(data_dir / "play_records.json")
        if isinstance(raw_play, Mapping):
            for day_key, players in raw_play.items():
                clean_day = str(day_key).strip().replace("/", "-")
                if not clean_day or not isinstance(players, Mapping):
                    continue
                for sid, games in players.items():
                    clean_sid = str(sid).strip()
                    if not clean_sid or not isinstance(games, Mapping):
                        continue
                    for gid, info in games.items():
                        clean_gid = str(gid).strip()
                        if not clean_gid or not isinstance(info, Mapping):
                            continue
                        raw_sec = info.get("seconds")
                        if raw_sec is not None:
                            try:
                                total_sec = float(raw_sec)
                            except (TypeError, ValueError):
                                continue
                        else:
                            try:
                                total_sec = float(info.get("minutes") or 0.0) * 60.0
                            except (TypeError, ValueError):
                                continue
                        if total_sec <= 0.0:
                            continue
                        covered = session_covered.get((clean_day, clean_sid, clean_gid), 0.0)
                        remainder = total_sec - covered
                        if remainder <= 0.5:
                            continue
                        gname = _extract_game_name(info.get("name"), clean_gid)
                        store.upsert_legacy_daily_playtime(
                            clean_day,
                            clean_sid,
                            clean_gid,
                            gname,
                            remainder,
                            mode="replace",
                        )
                        counts["legacy_daily_rows"] += 1

    # 9. Config backfill (read-only on legacy config JSON, non-destructive on config_section)
    if cfg_path is not None and cfg_path.is_file():
        raw_cfg = _read_json_readonly(cfg_path)
        if isinstance(raw_cfg, Mapping):
            backfilled_fields = _backfill_config_from_legacy(config_section, raw_cfg)

    summary = {
        "migrated": True,
        "already_migrated": False,
        "legacy_data_dir": str(data_dir) if data_dir is not None else "",
        "legacy_config_path": str(cfg_path) if cfg_path is not None else "",
        "counts": counts,
        "backfilled_config_fields": backfilled_fields,
    }
    store.set_kv(MIGRATION_SUMMARY_KV_KEY, json.dumps(summary, ensure_ascii=False))
    store.set_kv(MIGRATION_KV_KEY, "1")
    return summary
