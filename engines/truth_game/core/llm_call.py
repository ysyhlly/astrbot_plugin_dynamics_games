"""Unified LLM call helper for the truth-game plugin (T7).

The 4-step strategy is a **reimplementation** of the contract proven by
``astrbot_plugin_group_chronicle/core/llm_call.py`` (the behavioral spec only —
no code is copied). In exact order, per sequence:

1. Configured provider id (when non-empty):
   ``context.llm_generate(chat_provider_id=id, prompt=..., system_prompt=...)``,
   and if that returns ``None`` → ``context.get_provider_by_id(id).text_chat(...)``
   (NO ``contexts`` argument — the spec's source does not pass it).
2. Session provider: ``context.get_current_chat_provider_id(umo)`` → truthy →
   ``llm_generate(chat_provider_id=that_id, ...)``.
3. Fallback: ``context.get_using_provider(umo).text_chat(...)``.
4. All fail → ``LLMUnavailableError``.

Retry policy (pinned): the whole sequence is retried **once** only on FAST
failures (immediate exception / ``None`` return). ``asyncio.TimeoutError`` is
NEVER retried — a provider that hung a full timeout will not recover instantly,
and retrying would double the lock-hold worst case for nothing; on timeout we
raise ``LLMUnavailableError`` immediately. Worst case per generation:
2 sequences × 4 timed steps × ``llm_timeout_seconds``.

Design constraints:
- No astrbot imports at module top level — every context/provider method is
  reached through ``getattr`` guards, so this module (and its tests) run
  standalone without an AstrBot runtime, and older AstrBot versions degrade
  gracefully.
- Each LLM generation step runs under ``asyncio.wait_for`` so a hung provider
  cannot block a game room forever.
- Completion extraction: ``resp is None`` → failure; otherwise
  ``getattr(resp, "completion_text", None) or str(resp)``; an empty or
  whitespace-only result is a failure.
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

try:
    from ....series_panels import (
        is_provider_budget_busy_error,
        resolve_series_provider,
        series_llm_call,
    )
except Exception:  # pragma: no cover - standalone fallback
    def is_provider_budget_busy_error(_exc: BaseException | None) -> bool:
        return False

    def resolve_series_provider(
        _context: Any,
        explicit_provider: str = "",
        fallback_provider: str = "",
        *,
        role: str = "reply",
    ) -> str:
        _ = role
        return str(explicit_provider or "").strip() or str(fallback_provider or "").strip()

    async def series_llm_call(
        _context: Any,
        _provider_id: str,
        _plugin_id: str,
        _purpose: str,
        invoke: Any,
        *,
        acquire_timeout: float | None = 15.0,
    ) -> Any:
        _ = acquire_timeout
        return await invoke()

#: Whole-sequence retry budget: first attempt + one fast-failure retry.
_SEQUENCE_ATTEMPTS = 2


class LLMUnavailableError(Exception):
    """No usable LLM: every call path failed (or the provider timed out)."""


class _AllPathsFailed(Exception):
    """Internal signal: one full sequence ran but every path failed FAST.

    Distinct from ``LLMUnavailableError`` so the retry loop can distinguish
    "fast failure — retry once" from "timeout — never retry".
    """


async def call_llm(
    context: Any,
    umo: str,
    prompt: str,
    system_prompt: str | None = None,
    provider_id: str | None = None,
    llm_timeout_seconds: float = 60.0,
) -> str:
    """Generate text via the configured → session → fallback provider chain.

    Args:
        context: AstrBot plugin context (accessed via getattr only).
        umo: unified_msg_origin session identifier.
        prompt: the user-side prompt.
        system_prompt: optional system prompt.
        provider_id: optional configured provider id; empty/None means "not
            configured", in which case the session/default paths are used.
        llm_timeout_seconds: per-step timeout for every LLM generation call.

    Returns:
        The generated text (non-empty string).

    Raises:
        LLMUnavailableError: every path failed (after at most one fast-failure
            retry), or a step timed out (no retry in that case).
    """
    configured = resolve_series_provider(context, provider_id or "", role="reply") or None
    last_failure: Exception | None = None

    for attempt in range(1, _SEQUENCE_ATTEMPTS + 1):
        try:
            text = await _run_sequence(
                context, umo, prompt, system_prompt, configured, llm_timeout_seconds
            )
        except asyncio.TimeoutError:
            # A provider that hung a full timeout will not recover instantly.
            # Never retry on timeout: bounded worst case (2×4×timeout) relies on
            # fast failures being the ONLY retried case.
            logger.error(
                f"[llm_call] LLM 步骤超时({llm_timeout_seconds}s), 放弃且不重试 "
                f"(attempt {attempt}/{_SEQUENCE_ATTEMPTS})"
            )
            raise LLMUnavailableError(
                f"LLM 调用超时({llm_timeout_seconds}s), 未配置可用的 LLM"
            ) from None
        except _AllPathsFailed as exc:
            last_failure = exc
            if attempt < _SEQUENCE_ATTEMPTS:
                logger.warning(
                    f"[llm_call] 所有路径快速失败 (第 {attempt}/{_SEQUENCE_ATTEMPTS} 次), "
                    f"重试整个序列: {exc}"
                )
                continue
            break
        except Exception as exc:
            if is_provider_budget_busy_error(exc):
                logger.warning(
                    f"[llm_call] 共享模型队列繁忙，放弃重试并降级: {exc}"
                )
                raise LLMUnavailableError("共享模型队列繁忙，已降级处理") from exc
            raise
        # Non-empty text returned — success.
        return text

    raise LLMUnavailableError("未配置可用的 LLM") from last_failure


async def _run_sequence(
    context: Any,
    umo: str,
    prompt: str,
    system_prompt: str | None,
    configured_id: str | None,
    llm_timeout_seconds: float,
) -> str:
    """Run the 4-step strategy exactly once.

    Returns the first non-empty text; raises ``asyncio.TimeoutError`` if any
    step hangs past the timeout; raises ``_AllPathsFailed`` when every path
    failed fast (immediate exception or empty/None result).
    """
    # -- Step 1: plugin-configured provider -----------------------------
    if configured_id:
        text = await _step_llm_generate(
            context, configured_id, prompt, system_prompt, llm_timeout_seconds
        )
        if text is not None:
            logger.info(f"[llm_call] 成功: 配置 Provider (llm_generate) '{configured_id}'")
            return text
        provider = _provider_by_id(context, configured_id)
        text = await _step_text_chat(
            provider,
            prompt,
            system_prompt,
            llm_timeout_seconds,
            context=context,
            provider_id=configured_id,
        )
        if text is not None:
            logger.info(f"[llm_call] 成功: 配置 Provider (text_chat) '{configured_id}'")
            return text

    # -- Step 2: current session provider (new API) ---------------------
    session_id = await _resolve_session_provider_id(context, umo)
    if session_id:
        text = await _step_llm_generate(
            context, session_id, prompt, system_prompt, llm_timeout_seconds
        )
        if text is not None:
            logger.info(f"[llm_call] 成功: 会话 Provider '{session_id}'")
            return text

    # -- Step 3: provider currently in use (old API) --------------------
    provider = _using_provider(context, umo)
    text = await _step_text_chat(
        provider,
        prompt,
        system_prompt,
        llm_timeout_seconds,
        context=context,
        provider_id=_provider_label(provider),
    )
    if text is not None:
        logger.info("[llm_call] 成功: 回退 Provider (get_using_provider)")
        return text

    # -- Step 4: everything failed --------------------------------------
    raise _AllPathsFailed("配置/会话/默认 LLM Provider 均不可用")


# ---------------------------------------------------------------------------
# Per-step helpers
# ---------------------------------------------------------------------------


def _provider_label(provider: Any) -> str:
    if provider is None:
        return "default"
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            meta = meta()
        except Exception:
            meta = None
    pid = (
        (getattr(meta, "id", None) if meta is not None else None)
        or getattr(provider, "provider_id", None)
        or getattr(provider, "id", None)
    )
    return str(pid or "").strip() or "default"


async def _step_llm_generate(
    context: Any,
    provider_id: str,
    prompt: str,
    system_prompt: str | None,
    llm_timeout_seconds: float,
) -> str | None:
    """New unified API: ``context.llm_generate(...)`` under wait_for.

    Returns None on any fast failure (missing method / exception / empty
    result); re-raises ``asyncio.TimeoutError`` and ``ProviderBudgetBusyError``
    so they reach the no-retry path in ``call_llm``.
    """
    llm_generate = getattr(context, "llm_generate", None)
    if not callable(llm_generate):
        return None
    try:
        resp = await series_llm_call(
            context,
            provider_id,
            "astrbot_plugin_dynamics_games",
            "games_truth_game",
            lambda: asyncio.wait_for(
                llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                ),
                timeout=llm_timeout_seconds,
            ),
            acquire_timeout=min(15.0, float(llm_timeout_seconds)),
        )
    except asyncio.TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001 - degraded providers raise freely
        if is_provider_budget_busy_error(exc):
            raise
        logger.warning(f"[llm_call] llm_generate 失败: {exc}")
        return None
    return _extract_text(resp)


async def _step_text_chat(
    provider: Any,
    prompt: str,
    system_prompt: str | None,
    llm_timeout_seconds: float,
    *,
    context: Any = None,
    provider_id: str = "",
) -> str | None:
    """Old API: ``provider.text_chat(prompt=..., system_prompt=...)`` under wait_for.

    Returns None on any fast failure; re-raises ``asyncio.TimeoutError`` and
    ``ProviderBudgetBusyError``.
    """
    if provider is None:
        return None
    text_chat = getattr(provider, "text_chat", None)
    if not callable(text_chat):
        return None
    try:
        resp = await series_llm_call(
            context,
            provider_id or _provider_label(provider),
            "astrbot_plugin_dynamics_games",
            "games_truth_game",
            lambda: asyncio.wait_for(
                text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=llm_timeout_seconds,
            ),
            acquire_timeout=min(15.0, float(llm_timeout_seconds)),
        )
    except asyncio.TimeoutError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_provider_budget_busy_error(exc):
            raise
        logger.warning(f"[llm_call] text_chat 失败: {exc}")
        return None
    return _extract_text(resp)


async def _resolve_session_provider_id(context: Any, umo: str) -> str | None:
    """Resolve the current session's provider id via the new API.

    Not an LLM step — no wait_for (the 4-step worst-case bound counts only the
    4 generation calls). Any failure returns None and the sequence falls
    through to the fallback path.
    """
    get_pid = getattr(context, "get_current_chat_provider_id", None)
    if not callable(get_pid):
        return None
    try:
        provider_id = await get_pid(umo=umo)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[llm_call] 获取会话 Provider ID 失败: {exc}")
        return None
    return (provider_id or "").strip() or None


def _provider_by_id(context: Any, provider_id: str) -> Any:
    """Synchronous lookup: ``context.get_provider_by_id(id)`` (getattr-guarded)."""
    getter = getattr(context, "get_provider_by_id", None)
    if not callable(getter):
        return None
    try:
        return getter(provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[llm_call] get_provider_by_id('{provider_id}') 失败: {exc}")
        return None


def _using_provider(context: Any, umo: str) -> Any:
    """Synchronous lookup: ``context.get_using_provider(umo)`` (getattr-guarded).

    Tries the keyword form used by current AstrBot, then the positional form
    for older versions (TypeError fallback).
    """
    getter = getattr(context, "get_using_provider", None)
    if not callable(getter):
        return None
    try:
        return getter(umo=umo)
    except TypeError:
        try:
            return getter(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[llm_call] get_using_provider 失败: {exc}")
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[llm_call] get_using_provider 失败: {exc}")
        return None


def _extract_text(resp: Any) -> str | None:
    """Completion extraction: None/empty/whitespace → None (failure)."""
    if resp is None:
        return None
    text = getattr(resp, "completion_text", None) or resp
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return None
    return text


__all__ = ["LLMUnavailableError", "call_llm"]
