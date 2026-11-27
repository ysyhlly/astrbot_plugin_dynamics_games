"""Zero-coupling bridge for Dynamics Event Bus, 3D World keepsakes & Horizons co-read callbacks."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from astrbot.api import logger

from .. import series_panels

DEFAULT_BUS_TIMEOUT = 5.0
DEFAULT_KEEPSAKE_TIMEOUT = 8.0

CHAT_DYNAMICS_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_chat_dynamics",
    "ChatDynamics",
    "群间对话",
)
WORLD_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_dynamics_world",
    "DynamicsWorld",
    "群间世界",
)
HORIZONS_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_dynamics_horizons",
    "DynamicsHorizons",
    "群间见闻",
)


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _find_plugin(context: Any, aliases: tuple[str, ...]) -> Any | None:
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return None
    wanted = {a.lower() for a in aliases}
    try:
        raw = getter()
        entries = (
            list(raw.items())
            if isinstance(raw, dict)
            else [("", r) for r in (raw or ())]
        )
        for key, row in entries:
            if _value(row, "activated", True) is False:
                continue
            names = {
                str(x).strip().lower()
                for x in (
                    key,
                    _value(row, "name"),
                    _value(row, "root_dir_name"),
                    _value(row, "display_name"),
                )
                if x
            }
            if names & wanted:
                owner = (
                    _value(row, "star_cls")
                    or _value(row, "star")
                    or _value(row, "plugin")
                )
                if owner is not None and not isinstance(owner, type):
                    if not getattr(owner, "_terminating", False) and not getattr(
                        owner, "_closed", False
                    ):
                        return owner
    except Exception:
        return None
    return None


async def _call_maybe_async(
    fn: Any, *args: Any, timeout: float = DEFAULT_BUS_TIMEOUT, **kwargs: Any
) -> Any:
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await asyncio.wait_for(res, timeout=timeout)
    return res


async def emit_dynamics_event(
    context: Any,
    event_type: str,
    payload: dict[str, Any],
    *,
    timeout: float = DEFAULT_BUS_TIMEOUT,
) -> dict[str, Any]:
    """Emit a standard Dynamics Event Bus event to Chat Dynamics without hard coupling."""
    etype = str(event_type or "").strip()
    if not etype:
        return {"ok": False, "status": "invalid_event_type", "delivered": 0}
    plugin = _find_plugin(context, CHAT_DYNAMICS_ALIASES)
    if plugin is None:
        return {"ok": False, "status": "unavailable", "delivered": 0}
    emit_fn = getattr(plugin, "emit_dynamics_event", None)
    if not callable(emit_fn):
        return {"ok": False, "status": "unsupported", "delivered": 0}
    try:
        delivered = await _call_maybe_async(
            emit_fn, etype, dict(payload or {}), timeout=timeout
        )
        cnt = int(delivered) if isinstance(delivered, int) else 1
        return {"ok": True, "status": "emitted", "delivered": cnt}
    except (asyncio.TimeoutError, TimeoutError):
        logger.debug("emit_dynamics_event(%s) 超时", etype)
        return {"ok": False, "status": "timeout", "delivered": 0}
    except Exception as exc:
        logger.debug("emit_dynamics_event(%s) 异常: %s", etype, exc)
        return {"ok": False, "status": "error", "delivered": 0, "error": str(exc)[:160]}


async def emit_game_state_changed(
    context: Any,
    *,
    umo: str,
    active: bool,
    engine: str,
    activity_id: str = "",
    title: str = "",
    phase: str = "",
) -> dict[str, Any]:
    """Helper emitting the standard ``game_state_changed`` Dynamics Event Bus event."""
    clean_umo = str(umo or "").strip()
    if not clean_umo:
        return {"ok": False, "status": "skipped", "delivered": 0}
    return await emit_dynamics_event(
        context,
        "game_state_changed",
        {
            "umo": clean_umo,
            "active": bool(active),
            "engine": str(engine or ""),
            "activity_id": str(activity_id or ""),
            "title": str(title or "")[:120],
            "phase": str(phase or ""),
            "source_plugin": "astrbot_plugin_dynamics_games",
            "emitted_at": time.time(),
        },
    )


async def emit_milestone_achieved(
    context: Any,
    *,
    umo: str,
    title: str,
    summary: str,
    participants: list[dict[str, Any]] | list[str],
    image_path: str = "",
    deep_link: str = "",
    created_at: float | None = None,
) -> dict[str, Any]:
    """Helper emitting the standard ``milestone_achieved`` Dynamics Event Bus event."""
    clean_umo = str(umo or "").strip()
    if not clean_umo or not str(title or "").strip():
        return {"ok": False, "status": "skipped", "delivered": 0}
    norm_people: list[str] = []
    for item in (participants or [])[:30]:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("uid") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in norm_people:
            norm_people.append(name)
    return await emit_dynamics_event(
        context,
        "milestone_achieved",
        {
            "umo": clean_umo,
            "source_plugin": "astrbot_plugin_dynamics_games",
            "title": str(title)[:160],
            "summary": str(summary or "")[:800],
            "participants": norm_people,
            "image_path": str(image_path or "").strip(),
            "deep_link": str(deep_link or "").strip(),
            "created_at": float(created_at or time.time()),
        },
    )


async def register_world_keepsake(
    context: Any,
    *,
    umo: str,
    title: str,
    summary: str,
    sender_name: str = "",
    image_path: str = "",
    deep_link: str = "",
    kind: str = "memory_postcard",
    persona_id: str = "",
    timeout: float = DEFAULT_KEEPSAKE_TIMEOUT,
) -> dict[str, Any]:
    """Shelf a completed activity/telephone poster onto Dynamics World's 3D cabin keepsake wall."""
    plugin = _find_plugin(context, WORLD_ALIASES)
    if plugin is None:
        return {"ok": False, "status": "unavailable"}
    reg_fn = getattr(plugin, "register_external_keepsake", None)
    if not callable(reg_fn):
        return {"ok": False, "status": "unsupported"}

    resolved_persona = str(persona_id or "").strip()
    if not resolved_persona:
        try:
            shared = series_panels.get_series_shared_config(context) or {}
            resolved_persona = str(shared.get("persona_id") or "").strip()
        except Exception:
            resolved_persona = ""

    try:
        res = await _call_maybe_async(
            reg_fn,
            persona_id=resolved_persona,
            umo=str(umo or "").strip(),
            title=str(title or "")[:120],
            summary=str(summary or "")[:600],
            sender_name=str(sender_name or "群友们")[:80],
            source_plugin="astrbot_plugin_dynamics_games",
            deep_link=str(deep_link or "").strip(),
            image_path=str(image_path or "").strip(),
            kind=str(kind or "memory_postcard"),
            timeout=timeout,
        )
        if isinstance(res, dict):
            return {
                "ok": bool(res.get("ok", True)),
                "status": "registered",
                "response": res,
            }
        return {"ok": True, "status": "registered", "response": {}}
    except (asyncio.TimeoutError, TimeoutError):
        logger.debug("dynamics_world.register_external_keepsake 超时")
        return {"ok": False, "status": "timeout"}
    except Exception as exc:
        logger.debug("dynamics_world.register_external_keepsake 异常: %s", exc)
        return {"ok": False, "status": "error", "error": str(exc)[:160]}


def is_horizons_origin_activity(
    activity: dict[str, Any], *, meta: dict[str, Any] | None = None
) -> bool:
    """Detect whether a reading activity was launched from Dynamics Horizons bookshelf."""
    if str(activity.get("category") or "") != "reading":
        return False
    if meta and str(meta.get("source_plugin") or "").lower().find("horizons") >= 0:
        return True
    creator_uid = str(activity.get("creator_uid") or "").lower()
    desc = str(activity.get("description") or "").lower()
    creator_name = str(activity.get("creator_name") or "").lower()
    return (
        "horizons" in creator_uid
        or "horizons" in desc
        or "见闻书架" in creator_name
        or "群间见闻" in desc
    )


async def notify_horizons_coread_completed(
    context: Any,
    *,
    umo: str,
    activity: dict[str, Any],
    outcome: dict[str, Any],
    participants: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
    timeout: float = DEFAULT_BUS_TIMEOUT,
) -> dict[str, Any]:
    """Notify Dynamics Horizons when a bookshelf-originated co-reading activity finishes."""
    if not is_horizons_origin_activity(activity, meta=meta):
        return {"ok": False, "status": "not_applicable"}
    plugin = _find_plugin(context, HORIZONS_ALIASES)
    if plugin is None:
        return {"ok": False, "status": "unavailable"}

    joined_names = [
        str(p.get("name") or p.get("uid") or "")
        for p in participants
        if p.get("status", "joined") == "joined"
    ]
    payload = {
        "umo": str(umo or "").strip(),
        "activity_id": str(activity.get("id") or ""),
        "title": str(activity.get("title") or ""),
        "summary": str(outcome.get("summary") or "")[:600],
        "participants": joined_names[:30],
        "poster_path": str(outcome.get("poster_path") or ""),
        "completed_at": float(activity.get("completed_at") or time.time()),
        "meta": dict(meta or {}),
    }

    for method_name in (
        "mark_book_coread",
        "on_coread_completed",
        "record_coread_completed",
        "mark_bookshelf_coread",
    ):
        fn = getattr(plugin, method_name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            if len(params) == 1 and params[0].kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                res = await _call_maybe_async(fn, payload, timeout=timeout)
            else:
                res = await _call_maybe_async(
                    fn,
                    umo=payload["umo"],
                    title=payload["title"],
                    activity_id=payload["activity_id"],
                    summary=payload["summary"],
                    participants=payload["participants"],
                    meta=payload["meta"],
                    timeout=timeout,
                )
            return {
                "ok": True,
                "status": "notified",
                "method": method_name,
                "response": res if isinstance(res, dict) else {},
            }
        except Exception as exc:
            logger.debug("通知 dynamics_horizons.%s 失败: %s", method_name, exc)
            return {"ok": False, "status": "error", "method": method_name, "error": str(exc)[:160]}

    return {"ok": False, "status": "unsupported"}
