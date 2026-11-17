"""Persona resolution for the truth-game plugin (T7).

Reimplementation of the persona contract proven by
``astrbot_plugin_group_chronicle/core/persona.py`` (behavioral spec only):

- ``off``            -> ``None`` (caller uses its own neutral prompt).
- ``custom_prompt``  -> the configured ``custom_persona_prompt`` text
  (empty/whitespace -> ``None``).
- ``core_persona``   -> ``context.persona_manager.get_persona(persona_id)``,
  extracting the ``.system_prompt`` attribute (getattr); if that yields
  nothing, fall back to ``get_default_persona_v3(umo=umo)`` and extract
  ``default.get("prompt")`` (a dict ``.get`` — NOT ``.system_prompt`` on the
  default result).

Design constraints:
- No astrbot imports. EVERY context/persona-manager access goes through
  ``getattr`` guards and try/except, so older AstrBot versions (which may lack
  ``persona_manager`` entirely) degrade gracefully.
- Resolution NEVER raises: any failure returns ``None``.
- This module only feeds the plugin's OWN generation calls — it must NOT be
  injected into any global LLM hook (the ``on_llm_request`` anti-pattern).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PERSONA_MODE_OFF = "off"
PERSONA_MODE_CORE = "core_persona"
PERSONA_MODE_CUSTOM = "custom_prompt"


def _series_defaults(context: Any) -> dict[str, Any]:
    try:
        from ....series_panels import get_series_defaults
    except ImportError:
        return {}
    return get_series_defaults(context)


async def resolve_system_prompt(
    context: Any, config: dict, umo: str | None = None
) -> str | None:
    """Resolve the system prompt for one generation; None = neutral handling.

    Args:
        context: AstrBot plugin context (may lack ``persona_manager`` on old
            versions — handled gracefully).
        config: plugin config, dict-like or attribute-accessible object.
        umo: unified_msg_origin, used only when loading the default persona.

    Returns:
        The system prompt text, or None when persona is off / custom prompt is
        empty / resolution failed. Never raises.
    """
    mode = _cfg_get(config, "persona_mode", PERSONA_MODE_OFF)

    if mode == PERSONA_MODE_OFF:
        return None

    if mode == PERSONA_MODE_CUSTOM:
        return _non_empty(_cfg_get(config, "custom_persona_prompt", ""))

    if mode != PERSONA_MODE_CORE:
        logger.warning(f"[persona] 未知的人格模式: {mode!r}, 按中立提示词处理")
        return None

    # core_persona: persona manager is an AstrBot core feature — old versions
    # may not expose it; degrade to None rather than crash.
    persona_manager = getattr(context, "persona_manager", None)
    if persona_manager is None:
        logger.warning("[persona] 未检测到 persona_manager, 按中立提示词处理")
        return None

    persona_id = str(_cfg_get(config, "persona_id", "") or "").strip()
    if not persona_id:
        persona_id = str(_series_defaults(context).get("persona_id", "") or "").strip()
    if not persona_id:
        logger.warning("[persona] 未配置有效的 persona_id, 按中立提示词处理")
        return None

    prompt = await _load_named_persona(persona_manager, persona_id)
    if not prompt:
        prompt = await _load_default_persona(persona_manager, umo)
    return _non_empty(prompt)


async def _load_named_persona(persona_manager: Any, persona_id: str) -> str | None:
    """Try the configured core persona; any failure -> None (never raises)."""
    get_persona = getattr(persona_manager, "get_persona", None)
    if not callable(get_persona):
        return None
    try:
        persona = await get_persona(persona_id)
    except Exception as exc:  # noqa: BLE001 - resolve must never raise
        logger.warning(f"[persona] 获取核心人格 '{persona_id}' 失败: {exc}")
        return None
    if persona is None:
        return None
    # The result may be a persona object or malformed data — attribute access
    # via getattr only.
    return getattr(persona, "system_prompt", None)


async def _load_default_persona(
    persona_manager: Any, umo: str | None
) -> str | None:
    """Fall back to AstrBot's default persona prompt; any failure -> None."""
    get_default = getattr(persona_manager, "get_default_persona_v3", None)
    if not callable(get_default):
        return None
    try:
        default = await get_default(umo=umo)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[persona] 获取默认人格失败: {exc}")
        return None
    if default is None:
        return None
    # The default persona result is a dict-like — extract via .get("prompt"),
    # NOT .system_prompt (contract pinned by the behavioral spec).
    get = getattr(default, "get", None)
    if not callable(get):
        logger.warning("[persona] 默认人格结果不是 dict 形态, 无法提取 prompt")
        return None
    try:
        return get("prompt")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[persona] 默认人格结果格式异常: {exc}")
        return None


def _cfg_get(config: Any, key: str, default: Any) -> Any:
    """Dict-like or attribute-based config read; never raises."""
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:  # noqa: BLE001
            return default
    return getattr(config, key, default)


def _non_empty(prompt: Any) -> str | None:
    """Non-string / empty / whitespace-only -> None, else the string itself."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return prompt


__all__ = [
    "PERSONA_MODE_OFF",
    "PERSONA_MODE_CORE",
    "PERSONA_MODE_CUSTOM",
    "resolve_system_prompt",
]
