"""游戏流程接线：抽人 →（人类提问者选择 / 机器人出题）→ 记忆增强 → 出题 → 播报。

与 T5 的 commands.py 一样遵循 AstrBot-free 接缝原则：只导入 ``core/``
子模块与标准库，绝不导入 astrbot 符号。main.py 负责装配（薄接线）：
在插件 ``__init__`` 里执行 ``manager.advance_hook = build_advance_hook(...)``。

``build_advance_hook`` 是**同步**工厂（``__init__`` 不可 await）；返回的 hook
才是 ``async (room, manager) -> str``。工厂同时把
``manager.resolve_asker_bot`` 覆写为绑定了 context/config 的
``resolve_bot_question`` 包装（取消 choice timer + 真实出题），避免
``room.py → flow.py`` 循环导入。

模块结构
--------
* ``build_advance_hook(context, config, manager, rng=None)`` —— 同步工厂。
* hook（持锁调用，绝不自行获锁 / 不做身份重检）：
  - ``pick_pair`` 后若 asker == BOT → enrich + build_question + 记账 +
    ``round_stage="question_out"``，返回题目；
  - 否则（人类提问者）设 ``last_pair`` / ``last_askee_id`` /
    ``round_stage="awaiting_asker_mode"``，**不**动 ask_counts/asked_history，
    返回含超时秒数的中文选择提示（At 渲染由 T4 announce 层负责）。
* ``resolve_bot_question`` —— 选择超时 / 「机器人出题」共用路径：enrich +
  build_question + 记账 + ``question_out``；不 bump advance_count/idle_streak。
* ``submit_human_question`` —— 人类自出题记账（T6 catch-all 调用）。
* ``announce`` / ``announce_round`` / ``announce_choice`` —— 播报（``announce``
  签名不变；choice / question 分支由 stage-aware 辅助函数负责）。
* ``complete_and_announce`` —— 共享收尾：按 ``last_pair``+``round_stage`` 建
  Reply，``question_out`` 时追加 ``maybe_auto_end``。
* ``round_plain_text`` —— idle/timeout 的 ``room.bot.send`` 昵称纯文本。
* ``maybe_auto_end`` —— 题数上限自动结束（签名不变）。

健壮性：hook / resolve 内所有失败都被兜底为题库题目，绝不向调用方抛错。
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from .commands import Reply
from .lottery import BOT_ASKER_ID, Lottery
from .memory_enrich import enrich_for_askee
from .question_gen import build_question
from .room import Room, RoomManager


# ------------------------------------------------------------- config access
def _cfg_get(config: Any, key: str, default: Any) -> Any:
    """Dict-like or attribute-based config read; never raises (mirrors T8)."""
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:  # noqa: BLE001
            return default
    return getattr(config, key, default)


def _cfg_bool(config: Any, key: str, default: bool) -> bool:
    return bool(_cfg_get(config, key, default))


def _cfg_float(config: Any, key: str, default: float) -> float:
    try:
        return float(_cfg_get(config, key, default))
    except (TypeError, ValueError):
        return default


def _cfg_int(config: Any, key: str, default: int) -> int:
    try:
        return max(1, int(_cfg_get(config, key, default)))
    except (TypeError, ValueError):
        return default


def _room_umo(room: Room) -> str:
    """Session origin for LLM/LM calls; falls back to a group-scoped key."""
    return getattr(room, "umo", "") or f"group:{room.group_id}"


def _format_timeout_seconds(timeout: float) -> str:
    """Render timeout for the choice prompt (int when whole, else float)."""
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        t = 60.0
    if t == int(t):
        return str(int(t))
    return str(t)


def _choice_prompt(asker: str, askee: str, timeout_seconds: float) -> str:
    """Plain-text choice prompt; At rendering is T4's job via last_pair+stage."""
    sec = _format_timeout_seconds(timeout_seconds)
    return (
        f"🎲 轮到 @{asker} 提问 @{askee}。"
        f"请回复：1 自己出题 / 2 机器人出题"
        f"（{sec} 秒内未选择将由机器人出题）"
    )


def _bookkeep_question(
    room: Room,
    asker: str,
    askee: str,
    question: str,
) -> None:
    """Shared bookkeeping for a finalized question (bot or human submit)."""
    if askee:
        room.ask_counts[askee] = room.ask_counts.get(askee, 0) + 1
        room.last_askee_id = askee
    room.asked_history.append(question)
    room.last_pair = (asker, askee)
    room.round_stage = "question_out"


def _askee_nickname(room: Room, askee: str = "") -> str:
    """Resolve askee display name from room.players; empty when unknown."""
    pid = str(askee or "")
    if not pid:
        if room.last_pair is not None:
            pid = str(room.last_pair[1] or "")
        elif room.last_askee_id:
            pid = str(room.last_askee_id)
    if not pid:
        return ""
    player = room.players.get(pid)
    if player is None:
        return ""
    nick = getattr(player, "nickname", None)
    return str(nick).strip() if nick else ""


async def _safe_build_question(
    room: Room,
    context: Any,
    config: Any,
    *,
    memory_context: list[str] | None = None,
    askee_name: str | None = None,
) -> str:
    """build_question with double bank fallback; never raises."""
    name = (askee_name if askee_name is not None else _askee_nickname(room)) or None
    try:
        return await build_question(
            context=context,
            umo=_room_umo(room),
            config=config,
            asked_history=room.asked_history,
            memory_context=memory_context,
            askee_name=name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[flow] build_question failed group=%s，回退题库: %s",
            room.group_id,
            exc,
        )
        try:
            return await build_question(
                context=context,
                umo=_room_umo(room),
                config=config,
                asked_history=room.asked_history,
                askee_name=name,
            )
        except Exception:  # noqa: BLE001 - 双保险（理论上不可达）
            return "你最近一次开怀大笑是因为什么？"


async def _enrich_memories(
    room: Room,
    context: Any,
    askee: str,
    askee_name: str,
    *,
    enable_livingmemory: bool,
    memory_k: int,
) -> list[str] | None:
    """Optional LM enrich; failures → None, never raises."""
    if not enable_livingmemory:
        return None
    try:
        memories = await enrich_for_askee(
            context,
            _room_umo(room),
            askee,
            askee_name,
            memory_k=memory_k,
        )
        if memories:
            return list(memories)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[flow] enrich_for_askee failed group=%s: %s", room.group_id, exc
        )
    return None


def _pair_askee(room: Room) -> tuple[str, str]:
    """Resolve (asker, askee) from last_pair with safe fallbacks."""
    if room.last_pair is not None:
        asker, askee = room.last_pair
        return str(asker), str(askee)
    pids = list(room.players)
    askee = (
        room.last_askee_id
        if room.last_askee_id in room.players
        else (pids[0] if pids else "")
    )
    return BOT_ASKER_ID, str(askee) if askee else ""


# --------------------------------------------------------- resolve / submit
async def resolve_bot_question(
    room: Room,
    context: Any = None,
    config: Any = None,
    rng: random.Random | None = None,  # noqa: ARG001 - reserved / partial compat
    bot_ask_frequency_unused: Any = None,  # noqa: ARG001 - partial signature pad
    **_kwargs: Any,
) -> str:
    """Bot-resolve path for choice timeout / 「机器人出题」.

    Contract:

    * Safe to call under ``room._lock`` (does not acquire the lock).
    * Does **not** bump ``advance_count`` (pair already drawn).
    * Does **not** bump ``idle_streak``.
    * enrich (if enable_livingmemory) + ``build_question`` + bookkeeping:
      ``ask_counts[askee]+=1``, ``asked_history.append``, ``last_askee_id``,
      ``round_stage="question_out"`` (keeps existing ``last_pair`` asker).
    * Never raises — bank fallback on every failure.
    * Does **not** cancel the choice timer (caller / bound wrapper does).
    """
    del rng, bot_ask_frequency_unused
    asker, askee = _pair_askee(room)
    enable_lm = _cfg_bool(config, "enable_livingmemory", True)
    memory_k = _cfg_int(config, "memory_k", 5)
    players = {pid: p.nickname for pid, p in room.players.items()}
    askee_name = players.get(askee, askee or "")
    memory_context = await _enrich_memories(
        room,
        context,
        askee,
        askee_name,
        enable_livingmemory=enable_lm,
        memory_k=memory_k,
    )
    question = await _safe_build_question(
        room,
        context,
        config,
        memory_context=memory_context,
        askee_name=askee_name,
    )
    # Keep human asker on last_pair when present; only fill if missing.
    if room.last_pair is not None:
        asker, askee = room.last_pair
    _bookkeep_question(room, asker, askee, question)
    return question


async def submit_human_question(room: Room, text: str) -> str:
    """Bookkeeping-only path for a human-submitted question (T6 catch-all).

    Appends ``text`` to ``asked_history``, bumps ``ask_counts[askee]``,
    sets ``last_askee_id`` and ``round_stage="question_out"``. Does not
    cancel timers (caller owns that). Returns ``text`` unchanged.
    """
    asker, askee = _pair_askee(room)
    if room.last_pair is not None:
        asker, askee = room.last_pair
    q = str(text) if text is not None else ""
    _bookkeep_question(room, asker, askee, q)
    return q


# --------------------------------------------------------------- flow factory
def build_advance_hook(
    context: Any,
    config: Any,
    manager: RoomManager,
    rng: random.Random | None = None,
) -> Callable[[Room, RoomManager], Awaitable[str]]:
    """创建真正的 advance_hook，并接线 ``manager.resolve_asker_bot``。

    Args:
        context: AstrBot 插件上下文（T8/T9 使用，内部 getattr 守卫）。
        config: 插件配置（dict 或 T2 ``Config`` 对象均可）。
        manager: RoomManager。工厂会把 ``manager.resolve_asker_bot`` 覆写为
            绑定 context/config 的 ``resolve_bot_question`` 包装（含 choice
            timer 取消），避免 room.py 导入 flow.py。
        rng: 可注入随机源，供每轮 Lottery 使用；为 ``None`` 时工厂创建新的
            ``random.Random()`` 实例。

    Returns:
        异步 hook ``async (room, manager) -> str``。本工厂本身是同步的。
    """
    lottery_rng = rng if rng is not None else random.Random()
    bot_ask_enabled = _cfg_bool(config, "bot_ask_enabled", True)
    bot_ask_frequency = _cfg_float(config, "bot_ask_frequency", 0.5)
    enable_livingmemory = _cfg_bool(config, "enable_livingmemory", True)
    memory_k = _cfg_int(config, "memory_k", 5)

    async def hook(room: Room, mgr: RoomManager) -> str:
        """单轮推进：抽人 → 人类选择提示 / 机器人出题，返回展示文本。

        调用前提：在 ``_do_advance`` 内、持有 ``room._lock``（hook **不得**
        自行获取锁，也不做身份重检）。任何失败都被兜底，绝不抛错。
        """
        del mgr  # signature alignment; lock/identity owned by RoomManager
        asker: str = BOT_ASKER_ID
        askee: str = ""
        try:
            players = {pid: p.nickname for pid, p in room.players.items()}
            lottery = Lottery(
                players,
                room.ask_counts,
                bot_ask_enabled=bot_ask_enabled,
                bot_ask_frequency=bot_ask_frequency,
                rng=lottery_rng,
            )
            asker, askee = lottery.pick_pair(room.last_askee_id)

            # ---- human asker: defer question, return choice prompt ----
            if asker != BOT_ASKER_ID:
                room.last_pair = (asker, askee)
                room.last_askee_id = askee
                room.round_stage = "awaiting_asker_mode"
                # do NOT touch ask_counts / asked_history
                timeout = _cfg_float(
                    config, "asker_choice_timeout_seconds", 60.0
                )
                return _choice_prompt(asker, askee, timeout)

            # ---- bot asker: immediate question + bookkeeping ----
            askee_name = players.get(askee, askee)
            memory_context = await _enrich_memories(
                room,
                context,
                askee,
                askee_name,
                enable_livingmemory=enable_livingmemory,
                memory_k=memory_k,
            )
            question = await _safe_build_question(
                room,
                context,
                config,
                memory_context=memory_context,
                askee_name=askee_name,
            )
            _bookkeep_question(room, asker, askee, question)
            return question

        except Exception as exc:  # noqa: BLE001 - 兜底：bot path + question_out
            logger.exception(
                "[flow] 抽人/出题失败 group=%s，回退题库: %s",
                room.group_id,
                exc,
            )
            pids = list(room.players)
            askee = (
                room.last_askee_id
                if room.last_askee_id in room.players
                else (pids[0] if pids else "")
            )
            asker = BOT_ASKER_ID
            question = await _safe_build_question(
                room,
                context,
                config,
                askee_name=_askee_nickname(room, askee),
            )
            _bookkeep_question(room, asker, askee, question)
            return question

    async def _bound_resolve_asker_bot(room: Room) -> str:
        """RoomManager.resolve_asker_bot replacement (no room→flow import).

        Cancels the choice timer (T1 contract) then runs resolve_bot_question.
        """
        try:
            text = await resolve_bot_question(
                room, context=context, config=config, rng=lottery_rng
            )
        except Exception as exc:  # noqa: BLE001 - absolute never-raise
            logger.exception(
                "[flow] resolve_asker_bot failed group=%s: %s",
                getattr(room, "group_id", "?"),
                exc,
            )
            text = "你最近一次开怀大笑是因为什么？"
            try:
                asker, askee = _pair_askee(room)
                if room.last_pair is not None:
                    asker, askee = room.last_pair
                _bookkeep_question(room, asker, askee, text)
            except Exception:  # noqa: BLE001
                room.round_stage = "question_out"
        try:
            manager._cancel_choice_timer(getattr(room, "room_key", room.group_id))
        except Exception:  # noqa: BLE001
            logger.exception(
                "[flow] cancel choice timer failed group=%s",
                getattr(room, "group_id", "?"),
            )
        return text

    # Wire without room.py importing flow (avoids the cycle).
    manager.resolve_asker_bot = _bound_resolve_asker_bot  # type: ignore[method-assign]

    return hook


# ------------------------------------------------------------------ announce
_AWAITING_CHOICE_STAGES = frozenset(
    {"awaiting_asker_mode", "awaiting_asker_question"}
)

_SKIP_PREFIX = "⏭️ 已跳过上一题，下一轮：\n"
_MISSING_PAIR_MSG = "当前没有进行中的提问轮次。"


def _player_label(room: Any, pid: str) -> str:
    """Nickname for plain-text paths; bot asker → 「我」; missing → pid."""
    if pid == BOT_ASKER_ID:
        return "我"
    players = getattr(room, "players", None) or {}
    player = players.get(pid) if hasattr(players, "get") else None
    if player is not None:
        nick = getattr(player, "nickname", None)
        if nick:
            return str(nick)
    return str(pid) if pid is not None else ""


def _choice_body_text(stage: str, config: Any = None) -> str:
    """Trailing body after ``🎲 轮到 X 提问 Y`` (no leading period consumed)."""
    if stage == "awaiting_asker_question":
        return "。请直接发送你要问的题目（纯文本）。"
    timeout = _cfg_float(config, "asker_choice_timeout_seconds", 60.0)
    sec = _format_timeout_seconds(timeout)
    return (
        f"。请回复：1 自己出题 / 2 机器人出题"
        f"（{sec} 秒内未选择将由机器人出题）"
    )


async def announce(
    manager: Any,
    room: Any,
    asker_id: str,
    askee_id: str,
    question: str,
    skip_prefix: bool = False,
) -> Reply:
    """构造一轮的播报 Reply：``@asker 问 @askee：<题目>``。

    - 提问者为机器人哨兵（``__bot__``）时显示「我」（硬编码，无配置键）；
    - ``skip_prefix=True`` 时前置「⏭️ 已跳过上一题，下一轮：」；
    - 参数 ``manager``/``room`` 与计划签名对齐（当前实现不使用，保留以便
      后续扩展，例如按房间渲染昵称）。

    纯函数式构造（内部无 await），声明为 async 以对齐 T10 计划签名。
    """
    del manager, room  # 签名保留；当前播报只需要 asker/askee 的 user_id
    if asker_id == BOT_ASKER_ID:
        parts: list[tuple[str, str]] = [("text", "我")]
    else:
        parts = [("at", asker_id)]
    parts.append(("text", " 问 "))
    parts.append(("at", askee_id))
    parts.append(("text", f"：{question}"))
    if skip_prefix:
        parts.insert(0, ("text", _SKIP_PREFIX))
    return Reply(parts=parts)


async def announce_round(
    room: Any,
    text: str,
    *,
    skip_prefix: bool = False,
    manager: Any = None,
) -> Reply:
    """Question-path At-chain from ``room.last_pair`` + ``text``.

    Format matches :func:`announce` (``@asker 问 @askee：<q>``; bot → 「我」).
    ``skip_prefix`` only applies on this path. Missing ``last_pair`` → plain
    fallback (never raises).
    """
    pair = getattr(room, "last_pair", None) if room is not None else None
    if pair is None:
        prefix = _SKIP_PREFIX if skip_prefix else "🎲 下一题：\n"
        return Reply.plain(prefix + (text or ""))
    asker, askee = pair
    return await announce(
        manager, room, str(asker), str(askee), text or "", skip_prefix=skip_prefix
    )


async def announce_choice(room: Any, config: Any = None) -> Reply:
    """Choice/self-question prompt as an At-chain from ``last_pair`` + stage.

    Expected parts (``awaiting_asker_mode``)::

        [("text", "🎲 轮到 "), ("at", asker), ("text", " 提问 "),
         ("at", askee), ("text", "。请回复：1 自己出题 / 2 机器人出题（…）")]

    ``awaiting_asker_question`` swaps the tail for the self-question re-prompt.
    Bot asker is guarded (renders 「我」) though the lottery never reaches
    choice with a bot asker. Missing pair → plain error, no crash.
    """
    pair = getattr(room, "last_pair", None) if room is not None else None
    if pair is None:
        return Reply.plain(_MISSING_PAIR_MSG)
    asker, askee = str(pair[0]), str(pair[1])
    stage = str(getattr(room, "round_stage", "") or "")
    parts: list[tuple[str, str]] = []
    if asker == BOT_ASKER_ID:
        parts.append(("text", "🎲 轮到 我"))
    else:
        parts.append(("text", "🎲 轮到 "))
        parts.append(("at", asker))
    parts.append(("text", " 提问 "))
    parts.append(("at", askee))
    parts.append(("text", _choice_body_text(stage, config)))
    return Reply(parts=parts)


def round_plain_text(room: Any, text: str, config: Any = None) -> str:
    """Nickname plain-text for ``room.bot.send`` (idle / choice-timeout).

    * ``awaiting_asker_mode`` / ``awaiting_asker_question``: choice prompt with
      nicknames (``text`` unused for the template body).
    * otherwise (``question_out`` etc.): ``{asker} 问 {askee}：{text}`` where
      bot asker is 「我」 and humans use ``room.players`` nicknames.
    * missing ``last_pair``: returns ``text`` (or empty) — never raises.

    Choice-timeout callers keep their own prefix, e.g.
    ``⏰ 提问者超时未选择，由机器人出题：`` + this builder.
    """
    pair = getattr(room, "last_pair", None) if room is not None else None
    if pair is None:
        return text or ""
    asker, askee = str(pair[0]), str(pair[1])
    stage = str(getattr(room, "round_stage", "") or "")
    asker_label = _player_label(room, asker)
    askee_label = _player_label(room, askee)
    if stage in _AWAITING_CHOICE_STAGES:
        return (
            f"🎲 轮到 {asker_label} 提问 {askee_label}"
            f"{_choice_body_text(stage, config)}"
        )
    return f"{asker_label} 问 {askee_label}：{text or ''}"


async def complete_and_announce(
    manager: Any,
    group_id: str,
    caller_id: str,
    is_admin: bool,
    text: str,
    *,
    skip_prefix: bool = False,
    config: Any = None,
) -> Reply:
    """Shared post-advance Reply builder for start/next/skip/quote/choice.

    Branches on ``room.round_stage`` + ``room.last_pair``:

    * choice stages → :func:`announce_choice` (``skip_prefix`` ignored)
    * ``question_out`` (or other with pair) → :func:`announce_round` then
      append :func:`maybe_auto_end` summary when non-None
    * missing room/pair → plain fallback (never raises)

    Callers render the returned :class:`Reply` (At-chain or plain).
    """
    room = None
    if manager is not None:
        getter = getattr(manager, "get_room", None)
        room = getter(group_id) if callable(getter) else manager.rooms.get(group_id)
    if room is None:
        # Room may already be gone (e.g. concurrent end); still surface text.
        if text:
            prefix = _SKIP_PREFIX if skip_prefix else ""
            return Reply.plain(prefix + text)
        return Reply.plain("房间已结束。")

    stage = str(getattr(room, "round_stage", "") or "")
    pair = getattr(room, "last_pair", None)

    if pair is None:
        prefix = _SKIP_PREFIX if skip_prefix else "🎲 下一题：\n"
        reply = Reply.plain(prefix + (text or ""))
    elif stage in _AWAITING_CHOICE_STAGES:
        reply = await announce_choice(room, config=config)
    else:
        reply = await announce_round(
            room, text or "", skip_prefix=skip_prefix, manager=manager
        )

    if stage == "question_out":
        auto = await maybe_auto_end(
            manager, group_id, caller_id, is_admin, config, room=room
        )
        if auto is not None:
            reply.parts.append(("text", "\n" + auto.text()))
    return reply


# ------------------------------------------------------------------ auto end
async def maybe_auto_end(
    manager: Any,
    group_id: str,
    caller_id: str,
    is_admin: bool,
    config: Any,
    *,
    room=None,
) -> Reply | None:
    """Settle the same room whose question triggered the round limit."""
    if room is None and manager is not None:
        getter = getattr(manager, "get_room", None)
        room = getter(group_id) if callable(getter) else manager.rooms.get(group_id)
    if room is None:
        return None
    max_q = _cfg_int(config, "max_questions_per_game", 30)
    summary = await manager.end_at_limit(room, max_q)
    return Reply.plain(summary["summary_text"]) if summary else None


__all__ = [
    "build_advance_hook",
    "resolve_bot_question",
    "submit_human_question",
    "announce",
    "announce_round",
    "announce_choice",
    "round_plain_text",
    "complete_and_announce",
    "maybe_auto_end",
    "BOT_ASKER_ID",
]
