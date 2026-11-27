"""Zero-coupling bridge emitting structured entertainment signals to Learning & World."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from astrbot.api import logger

DEFAULT_SIGNAL_TIMEOUT = 5.0

LEARNING_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_dynamics_learning",
    "DynamicsLearning",
    "群间学习",
)
WORLD_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_dynamics_world",
    "DynamicsWorld",
    "群间世界",
)


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _find_plugin_by_aliases(context: Any, aliases: tuple[str, ...]) -> Any | None:
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


def find_learning_plugin(context: Any) -> Any | None:
    return _find_plugin_by_aliases(context, LEARNING_ALIASES)


def find_world_plugin(context: Any) -> Any | None:
    return _find_plugin_by_aliases(context, WORLD_ALIASES)


async def _call_maybe_async(fn: Any, *args: Any, timeout: float = DEFAULT_SIGNAL_TIMEOUT, **kwargs: Any) -> Any:
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await asyncio.wait_for(res, timeout=timeout)
    return res


async def emit_entertainment_signal(
    context: Any,
    *,
    umo: str,
    signal_type: str,
    summary: str,
    participants: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
    mood_hint: str = "cheerful",
    timeout: float = DEFAULT_SIGNAL_TIMEOUT,
) -> dict[str, Any]:
    """Emit structured entertainment signals to Dynamics Learning and Dynamics World.

    Fully zero-coupling and fault-tolerant: missing plugins or unsupported hooks
    degrade silently without affecting gameplay.
    """
    clean_umo = str(umo or "").strip()
    clean_summary = str(summary or "").strip()[:800]
    if not clean_umo or not clean_summary:
        return {"learning": {"ok": False, "status": "skipped"}, "world": {"ok": False, "status": "skipped"}}

    norm_participants = [
        {
            "uid": str(p.get("uid") or "").strip(),
            "name": str(p.get("name") or p.get("uid") or "").strip()[:80],
            "role": str(p.get("role") or p.get("role_note") or "").strip()[:60],
            "weight": float(p.get("weight", p.get("role_weight", 1)) or 1),
        }
        for p in (participants or [])[:30]
        if p.get("uid")
    ]
    payload = {
        "schema_version": 1,
        "source_plugin": "astrbot_plugin_dynamics_games",
        "umo": clean_umo,
        "signal_type": str(signal_type or "game"),
        "summary": clean_summary,
        "mood_hint": str(mood_hint or "cheerful"),
        "participants": norm_participants,
        "metadata": dict(metadata or {}),
        "occurred_at": time.time(),
    }

    learning_res: dict[str, Any] = {"ok": False, "status": "unavailable"}
    learning_plugin = find_learning_plugin(context)
    if learning_plugin is not None:
        try:
            direct_fn = (
                getattr(learning_plugin, "record_entertainment_signal", None)
                or getattr(learning_plugin, "ingest_social_signal", None)
            )
            if callable(direct_fn):
                res = await _call_maybe_async(direct_fn, payload, timeout=timeout)
                learning_res = {
                    "ok": True,
                    "status": "sent",
                    "response": res if isinstance(res, dict) else {},
                }
            else:
                refl_fn = getattr(learning_plugin, "ingest_horizons_reflection", None)
                if callable(refl_fn) and norm_participants:
                    sent_cnt = 0
                    topic = f"群间娱乐:{payload['signal_type']}"
                    for p in norm_participants[:6]:
                        await _call_maybe_async(
                            refl_fn,
                            clean_umo,
                            p["uid"],
                            topic,
                            clean_summary,
                            sentiment="positive" if mood_hint != "tense" else "neutral",
                            timeout=timeout,
                        )
                        sent_cnt += 1
                    learning_res = {
                        "ok": sent_cnt > 0,
                        "status": "sent_reflection",
                        "count": sent_cnt,
                    }
                else:
                    learning_res = {"ok": False, "status": "unsupported"}
        except Exception as exc:
            logger.debug("娱乐信号同步至群间学习失败: %s", exc)
            learning_res = {"ok": False, "status": "error", "error": str(exc)[:160]}

    world_res: dict[str, Any] = {"ok": False, "status": "unavailable"}
    world_plugin = find_world_plugin(context)
    if world_plugin is not None:
        try:
            world_fn = (
                getattr(world_plugin, "record_mood_signal", None)
                or getattr(world_plugin, "ingest_world_signal", None)
                or getattr(world_plugin, "record_companion_event", None)
            )
            if callable(world_fn):
                res = await _call_maybe_async(world_fn, payload, timeout=timeout)
                world_res = {
                    "ok": True,
                    "status": "sent",
                    "response": res if isinstance(res, dict) else {},
                }
            else:
                world_res = {"ok": False, "status": "unsupported"}
        except Exception as exc:
            logger.debug("娱乐信号同步至群间世界失败: %s", exc)
            world_res = {"ok": False, "status": "error", "error": str(exc)[:160]}

    return {"learning": learning_res, "world": world_res}
