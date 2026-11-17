"""Controlled bridge to Dynamics Memory (群间记忆) for recording shared activity experiences."""

from __future__ import annotations

import importlib
import logging
import time
from typing import Any

logger = logging.getLogger("astrbot.plugin.astrbot_plugin_dynamics_games.memory_bridge")

FACADE_MODULES: tuple[str, ...] = (
    "astrbot_plugin_dynamics_memory.api.public",
    "astrbot_plugin_livingmemory.api.public",
)
MEMORY_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_dynamics_memory",
    "DynamicsMemory",
    "群间记忆",
    "astrbot_plugin_livingmemory",
    "LivingMemory",
)


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def find_memory_plugin(context: Any) -> Any | None:
    """Resolve active Dynamics Memory plugin instance without raising."""
    for mod_name in FACADE_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, "find_plugin", None)
            if callable(fn):
                inst = fn(context)
                if inst is not None and not getattr(inst, "_terminating", False):
                    return inst
        except Exception:
            continue

    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return None
    try:
        raw = getter()
        entries = list(raw.items()) if isinstance(raw, dict) else [("", r) for r in (raw or ())]
        for key, row in entries:
            if _value(row, "activated", True) is False:
                continue
            names = {
                str(x)
                for x in (
                    key,
                    _value(row, "name"),
                    _value(row, "root_dir_name"),
                    _value(row, "display_name"),
                )
                if x
            }
            if names & set(MEMORY_ALIASES):
                owner = (
                    _value(row, "star_cls")
                    or _value(row, "star")
                    or _value(row, "plugin")
                )
                if owner is not None and not isinstance(owner, type):
                    return owner
    except Exception:
        return None
    return None


def memory_status(context: Any) -> dict[str, Any]:
    plugin = find_memory_plugin(context)
    if plugin is None:
        return {"detected": False, "ready": False, "detail": "not_installed_or_disabled"}
    caps_fn = getattr(plugin, "get_dynamics_capabilities", None)
    if callable(caps_fn):
        try:
            caps = caps_fn()
            if isinstance(caps, dict):
                return {
                    "detected": True,
                    "ready": bool(caps.get("ready")),
                    "provider_id": caps.get("provider_id", "astrbot_plugin_dynamics_memory"),
                    "has_activity_api": callable(
                        getattr(plugin, "record_activity_experience", None)
                    ),
                }
        except Exception:
            pass
    ready = bool(getattr(getattr(plugin, "initializer", None), "is_initialized", False))
    return {"detected": True, "ready": ready, "has_activity_api": False}


async def sync_experience_to_memory(
    context: Any,
    *,
    umo: str,
    activity_id: str,
    category: str,
    category_label: str,
    title: str,
    event_kind: str,
    summary: str,
    participants: list[dict[str, Any]],
    source_refs: list[dict[str, Any]] | None = None,
    occurred_at: float | None = None,
) -> dict[str, Any]:
    """Write a group activity through Dynamics Memory's source-validating API."""
    plugin = find_memory_plugin(context)
    if plugin is None:
        return {"ok": False, "status": "unavailable", "doc_id": 0, "reason": "群间记忆未启用"}

    ts = float(occurred_at or time.time())
    payload = {
        "schema_version": 1,
        "scope": {
            "umo": umo,
            "platform": umo.split(":", 1)[0] if ":" in umo else "unknown",
        },
        "activity_id": str(activity_id),
        "category": str(category),
        "category_label": str(category_label),
        "title": str(title)[:200],
        "event_kind": str(event_kind),
        "summary": str(summary)[:2400],
        "participants": [
            {
                "uid": str(p.get("uid") or "")[:80],
                "name": str(p.get("name") or p.get("uid") or "")[:80],
                "role": str(p.get("role_note") or p.get("role") or "")[:60],
                "contributions": int(p.get("contributions_count", p.get("contribution_count", 0)) or 0),
            }
            for p in participants[:50]
            if p.get("uid")
        ],
        "source_refs": (source_refs or [])[:32],
        "occurred_at": ts,
    }

    record_fn = getattr(plugin, "record_activity_experience", None)
    if not callable(record_fn):
        return {
            "ok": False, "status": "unsupported", "doc_id": 0,
            "reason": "群间记忆版本未开放带来源校验的经历接口",
        }
    try:
        res = await record_fn(payload)
        response = res if isinstance(res, dict) else {}
        doc_id = int(response.get("doc_id") or 0)
        reason = str(response.get("reason") or "")
        if doc_id > 0 and response.get("recorded"):
            return {"ok": True, "status": "synced", "doc_id": doc_id, "reason": ""}
        if doc_id > 0 and reason == "duplicate":
            return {"ok": True, "status": "duplicate", "doc_id": doc_id, "reason": "已存在相同经历记录"}
        # Keep the failure reason on the outcome for inspection and retry.
        return {
            "ok": False, "status": reason or "rejected", "doc_id": 0,
            "reason": reason or "记忆写入未通过校验",
        }
    except Exception as exc:
        logger.warning("群间记忆 record_activity_experience 调用异常: %s", exc)
        return {"ok": False, "status": "error", "doc_id": 0, "reason": str(exc)[:200]}
