"""Controlled bridge to Group Chronicle (群史记) for admitting valuable outcomes."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.models import CATEGORIES, RISK_WORDS, activity_fingerprint

logger = logging.getLogger("astrbot.plugin.astrbot_plugin_dynamics_games.chronicle_bridge")

CHRONICLE_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_group_chronicle",
    "GroupChronicle",
    "群史记",
)


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def find_chronicle_plugin(context: Any) -> Any | None:
    """Resolve active Group Chronicle plugin instance without raising."""
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
            if names & set(CHRONICLE_ALIASES):
                owner = (
                    _value(row, "star_cls")
                    or _value(row, "star")
                    or _value(row, "plugin")
                )
                if owner is not None and not isinstance(owner, type):
                    if not getattr(owner, "_terminating", False):
                        return owner
    except Exception:
        return None
    return None


def chronicle_status(context: Any) -> dict[str, Any]:
    plugin = find_chronicle_plugin(context)
    if plugin is None:
        return {"detected": False, "ready": False, "detail": "not_installed_or_disabled"}
    has_api = callable(getattr(plugin, "record_valuable_outcome", None))
    has_history = getattr(plugin, "history", None) is not None
    return {
        "detected": True,
        "ready": bool(has_history),
        "has_outcome_api": has_api,
    }


async def sync_outcome_to_chronicle(
    context: Any,
    *,
    umo: str,
    activity: dict[str, Any],
    participants: list[dict[str, Any]],
    contributions: list[dict[str, Any]],
    summary: str,
    valuable: bool,
    importance: int = 1,
    admission_mode: str = "auto",
) -> dict[str, Any]:
    """Submit a verified, completed, valuable outcome to Group Chronicle.

    Hard invariant: cancelled, unfinished, or non-valuable activities are NEVER
    submitted to Group Chronicle.
    """
    if not valuable or activity.get("status") != "completed":
        return {
            "ok": False,
            "status": "not_eligible",
            "event_id": 0,
            "reason": "未达群史记收录门槛或非完结成果",
        }

    plugin = find_chronicle_plugin(context)
    if plugin is None:
        return {
            "ok": False,
            "status": "unavailable",
            "event_id": 0,
            "reason": "群史记插件未启用",
        }

    cat_key = str(activity.get("category") or "creation")
    cat = CATEGORIES.get(cat_key, CATEGORIES["creation"])
    chronicle_kind = str(cat.get("chronicle_kind") or "activity")

    joined = [p for p in participants if p.get("status", "joined") == "joined"]
    creator_uid = str(activity.get("creator_uid") or "")
    mapped_participants: list[dict[str, Any]] = []
    for p in joined[:30]:
        uid = str(p.get("uid") or "").strip()
        if not uid:
            continue
        cnt = int(p.get("contributions_count", p.get("contribution_count", 0)) or 0)
        explicit_weight = p.get("role_weight")
        if explicit_weight in (1, 2, 3):
            role = int(explicit_weight)
        elif uid == creator_uid or cnt >= 3:
            role = 3
        elif cnt >= 1:
            role = 2
        else:
            role = 1
        mapped_participants.append(
            {
                "uid": uid,
                "name": str(p.get("name") or uid)[:80],
                "role": role,
            }
        )

    sources = []
    for c in contributions:
        raw_mid = str(c.get("message_id") or "")
        if not raw_mid.isascii() or not raw_mid.isdecimal() or len(raw_mid) > 19:
            continue
        numeric_id = int(raw_mid)
        if not 0 < numeric_id < 2**63:
            continue
        excerpt = str(c.get("content") or "")[:400]
        sources.append(
            {
                "id": numeric_id,
                "uid": str(c.get("uid") or ""),
                "excerpt": excerpt,
                "ts": float(c.get("created_at") or time.time()),
            }
        )
        if len(sources) == 24:
            break

    corpus = " ".join(
        [str(activity.get("title", "")), summary]
        + [str(s.get("excerpt") or "") for s in sources]
    )
    has_risk = any(w in corpus for w in RISK_WORDS)
    outcome_payload = {
        "schema_version": 1,
        "source_plugin": "astrbot_plugin_dynamics_games",
        "activity_id": str(activity["id"]),
        "fingerprint": activity_fingerprint(umo, str(activity["id"]), chronicle_kind),
        "kind": chronicle_kind,
        "category": cat_key,
        "category_label": cat["label"],
        "title": f"【{cat['label']}】{activity['title']}"[:200],
        "detail": str(summary)[:2800],
        "importance": max(1, min(3, int(importance))),
        "positive": not has_risk,
        "risk": has_risk,
        "participants": mapped_participants,
        "sources": sources,
        "source_basis": "message_refs" if sources else "derived_outcome",
        "event_at": float(activity.get("completed_at") or time.time()),
        "admission_preference": "pending" if (admission_mode == "pending" or has_risk) else "approved",
    }

    record_fn = getattr(plugin, "record_valuable_outcome", None)
    if callable(record_fn):
        try:
            res = await record_fn(umo, outcome_payload)
            if isinstance(res, dict) and res.get("admitted"):
                return {
                    "ok": True,
                    "status": str(res.get("status") or "approved"),
                    "event_id": int(res.get("event_id") or 0),
                    "reason": "",
                }
            return {
                "ok": False,
                "status": str((res or {}).get("status") or "rejected"),
                "event_id": 0,
                "reason": str((res or {}).get("reason") or "未通过群史记准入检查"),
            }
        except Exception as exc:
            logger.warning("群史记 record_valuable_outcome 调用失败: %s", exc)
            return {
                "ok": False,
                "status": "error",
                "event_id": 0,
                "reason": str(exc)[:200],
            }

    return {
        "ok": False,
        "status": "unsupported",
        "event_id": 0,
        "reason": "群史记版本未开放成果收录接口",
    }
