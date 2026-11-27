"""Zero-coupling bridge to Visual Media (视觉工坊) for activity posters & relay illustrations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger

DEFAULT_VISUAL_TIMEOUT = 25.0

VISUAL_ALIASES: tuple[str, ...] = (
    "astrbot_plugin_visual_media",
    "VisualMedia",
    "视觉工坊",
    "群间视觉",
)


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def find_visual_plugin(context: Any) -> Any | None:
    """Resolve active Visual Media plugin instance without hard imports or raising."""
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return None
    wanted = {alias.lower() for alias in VISUAL_ALIASES}
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


def visual_status(context: Any) -> dict[str, Any]:
    plugin = find_visual_plugin(context)
    if plugin is None:
        return {
            "detected": False,
            "ready": False,
            "has_artwork_api": False,
            "detail": "not_installed_or_disabled",
        }
    has_api = callable(getattr(plugin, "generate_series_artwork", None))
    caps_fn = getattr(plugin, "get_dynamics_capabilities", None)
    if callable(caps_fn):
        try:
            caps = caps_fn()
            if isinstance(caps, dict):
                return {
                    "detected": True,
                    "ready": bool(caps.get("ready")),
                    "provider_id": caps.get(
                        "provider_id", "astrbot_plugin_visual_media"
                    ),
                    "has_artwork_api": has_api,
                }
        except Exception:
            pass
    return {
        "detected": True,
        "ready": has_api,
        "has_artwork_api": has_api,
    }


async def generate_artwork_via_visual_media(
    context: Any,
    prompt: str,
    *,
    persona_id: str = "",
    umo: str = "",
    style: str = "polaroid",
    reference_character: str = "",
    source_plugin: str = "astrbot_plugin_dynamics_games",
    timeout: float = DEFAULT_VISUAL_TIMEOUT,
) -> dict[str, Any]:
    """Request an illustration from Visual Media; gracefully fall back when unavailable."""
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        return {
            "ok": False,
            "image_path": "",
            "source": "fallback",
            "error": "empty_prompt",
        }

    plugin = find_visual_plugin(context)
    if plugin is None:
        return {
            "ok": False,
            "image_path": "",
            "source": "fallback",
            "error": "visual_media_unavailable",
        }

    gen_fn = getattr(plugin, "generate_series_artwork", None)
    if not callable(gen_fn):
        return {
            "ok": False,
            "image_path": "",
            "source": "fallback",
            "error": "artwork_api_unsupported",
        }

    try:
        res = await asyncio.wait_for(
            gen_fn(
                clean_prompt,
                persona_id=str(persona_id or ""),
                umo=str(umo or ""),
                style=str(style or "polaroid"),
                reference_character=str(reference_character or ""),
                source_plugin=str(source_plugin or "astrbot_plugin_dynamics_games"),
            ),
            timeout=timeout,
        )
        if isinstance(res, dict) and res.get("ok"):
            img_path = str(res.get("image_path") or "").strip()
            if img_path and Path(img_path).is_file():
                return {
                    "ok": True,
                    "image_path": img_path,
                    "source": "visual_media",
                    "error": "",
                }
            return {
                "ok": False,
                "image_path": "",
                "source": "fallback",
                "error": "image_file_missing",
            }
        err = str((res or {}).get("error") or "generation_rejected")
        return {
            "ok": False,
            "image_path": "",
            "source": "fallback",
            "error": err[:200],
        }
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("视觉工坊 generate_series_artwork 调用超时 (%.1fs)", timeout)
        return {
            "ok": False,
            "image_path": "",
            "source": "fallback",
            "error": "timeout",
        }
    except Exception as exc:
        logger.warning("视觉工坊 generate_series_artwork 调用异常: %s", exc)
        return {
            "ok": False,
            "image_path": "",
            "source": "fallback",
            "error": str(exc)[:200],
        }
