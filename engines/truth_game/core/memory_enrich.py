"""Memory enrichment for truth-game questions (T9).

Recalls LivingMemory memories about the *askee* and returns them as plain
strings. The caller (question generation / game flow, T8/T10) decides where the
strings land in the USER prompt — this module never injects memory data into
any system prompt.

Guarantees:

- Fully importable without AstrBot / LivingMemory: the only LivingMemory
  symbol it knows is the soft bridge in ``lm_bridge``.
- Never raises: every failure (LM absent, not ready, engine missing, search
  raising, malformed results) silently yields ``[]``.
- Never reads LivingMemory DB files directly.
"""

from __future__ import annotations

from typing import Any

from .lm_bridge import detect_lm, ensure_ready, get_engine

#: Default recall size when ``memory_k`` cannot be coerced to a positive int.
_DEFAULT_MEMORY_K = 5


def _memory_text(result: Any) -> str:
    """Extract the plain text of a single search result.

    Accepts dicts with a ``content`` / ``text`` key and objects exposing a
    ``content`` attribute (LivingMemory's HybridResult). Malformed entries
    yield "" and are skipped by the caller.
    """
    try:
        if isinstance(result, dict):
            raw = result.get("content") or result.get("text") or ""
        else:
            raw = getattr(result, "content", None) or ""
        return str(raw).strip()
    except Exception:
        return ""


def _collect_texts(results: Any) -> list[str]:
    """Dedupe non-empty result texts, preserving first-seen order.

    Tolerates ``None``, non-iterables and mixed result shapes.
    """
    if not results:
        return []
    try:
        iterator = iter(results)
    except TypeError:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in iterator:
        text = _memory_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


async def enrich_for_askee(
    context: Any,
    umo: str,
    askee_id: str,
    askee_name: str,
    memory_k: int = 5,
    enabled: bool = True,
) -> list[str]:
    """Recall LivingMemory memories about the askee.

    Args:
        context: AstrBot ``Context`` (or a stand-in exposing
            ``get_registered_star`` / ``get_all_stars``).
        umo: ``unified_msg_origin`` of the current group session.
        askee_id: player id of the person being asked.
        askee_name: display name of the person being asked.
        memory_k: number of memories to recall (clamped to >= 1).
        enabled: master switch; ``False`` short-circuits to ``[]`` (the
            caller passes the config's ``enable_livingmemory`` here).

    Returns:
        Plain, deduplicated memory strings. ``[]`` on every failure path.

    IMPORTANT — ``session_id=umo`` is SESSION-SCOPING, NOT user identity:
    LivingMemory stores memories per unified_msg_origin, so passing ``umo``
    narrows the search to THIS GROUP's memory index (exactly the reference
    pattern in group_chronicle's evidence search). This is NOT the
    UMO-as-user-identity bug from the command layer: UMO is identical for
    every member of a group, so it can never identify an individual player —
    the per-person focus comes from the query text (``关于 {askee_name}``).
    """
    if not enabled:
        return []
    try:
        plugin = detect_lm(context)
        if plugin is None:
            return []
        if not await ensure_ready(plugin):
            return []
        engine = get_engine(plugin)
        search = getattr(engine, "search_memories", None) if engine is not None else None
        if not callable(search):
            return []

        if (askee_name or "").strip():
            query = f"关于 {askee_name.strip()} 的记忆"
        else:
            query = f"关于 {askee_id} 的记忆"
        try:
            k = max(1, int(memory_k))
        except (TypeError, ValueError):
            k = _DEFAULT_MEMORY_K

        results = await search(query=query, k=k, session_id=umo)
        return _collect_texts(results)
    except Exception:
        return []


__all__ = ["enrich_for_askee"]
