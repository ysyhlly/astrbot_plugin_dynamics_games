"""Per-group room state machine for the truth-game plugin (T3 + v2 T1).

This is the concurrency-critical heart of the plugin. It is **pure asyncio +
stdlib**: it imports NO astrbot symbols, so it stays unit-testable without an
AstrBot runtime. The command layer (T5) and the game-flow wiring (T10) drive
this module; question generation is injected later via ``RoomManager.advance_hook``.

Concurrency contract
--------------------
* One ``Room`` per ``group_id``; ``Room.phase`` is ``"waiting" | "playing"``.
* **Lock ownership (single owner):** the per-room ``asyncio.Lock`` is acquired
  ONLY inside ``RoomManager`` mutating methods (``start`` / ``next`` / ``skip`` /
  ``end`` / ``leave`` and the timer auto-advance paths). Command handlers must
  NEVER acquire it — ``asyncio.Lock`` is not reentrant, and a double acquire
  deadlocks the room.
* **Identity recheck (after every ``await``):** ``asyncio.Lock.acquire()`` is an
  await point, so after acquiring the lock — and after any other await inside a
  locked section (the advance hook, sends, timer wakes) — the code MUST recheck
  ``manager.rooms.get(group_id) is self_room`` (OBJECT identity, not key
  presence: a fresh room reuses the key after ``end``) and ``phase == "playing"``
  where applicable. On failure the operation drops silently.
* **Timer lifetime:** ``end()`` / idle auto-end / leave auto-end cancel the
  room's own timer tasks (waiting + idle + choice) BEFORE popping, so a stale
  timer can never wake into a NEW room that reuses the group key. The identity
  recheck is the second guard.
* **Advance contract (inside the lock, human and idle paths alike):**
    1. ``room.advance_count += 1`` — the single monotonic source of truth used
       by the idle/choice timers to detect "someone else advanced while I slept".
    2. ``text = await self.advance_hook(room, self)``  <- await point.
    3. After the await, identity recheck; on failure return
       ``(False, "房间已结束")`` WITHOUT announcing (drop silently).
* **Bot send contract (duck-typed, no astrbot imports):** ``room.bot`` is an
  object exposing ``async send(text: str)`` (the fakes in tests; T10 wires the
  real one). A missing/``None`` bot or a failed send is skipped silently.
* **round_stage (v2):** ``"" | "awaiting_asker_mode" | "awaiting_asker_question"
  | "question_out"``. Choice timer arms while awaiting asker mode/question;
  cancelled on next/skip/end/terminate/leave-void/idle-advance.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from astrbot.api import logger

_AWAITING_ASKER_STAGES = frozenset({"awaiting_asker_mode", "awaiting_asker_question"})


@dataclass
class Player:
    """One participant in a truth-game room."""

    user_id: str
    nickname: str


@dataclass
class Room:
    """Per-group truth-game room state.

    ``_lock`` is the room's internal asyncio.Lock. It is acquired ONLY inside
    ``RoomManager`` mutating methods — never by callers. (It lives on the
    dataclass so the manager and its timers can share it; the leading
    underscore + ``repr=False`` mark it as internal.)
    """

    group_id: str
    host_id: str
    phase: str = "waiting"  # "waiting" | "playing"
    players: dict[str, Player] = field(default_factory=dict)
    asked_history: list[str] = field(default_factory=list)
    ask_counts: dict[str, int] = field(default_factory=dict)
    #: Monotonic; incremented on EVERY human or idle advance. Single source of
    #: truth for the idle-vs-human race detection (see ADVANCE CONTRACT).
    advance_count: int = 0
    #: Consecutive idle auto-advances; reset to 0 by any human 下一题/跳过.
    idle_streak: int = 0
    created_at: float = 0.0  # time.time()
    started_at: float = 0.0  # time.time(); 0 until start()
    last_action_at: float = 0.0  # time.time(); sliding waiting window
    bot: Any = None  # duck-typed: object with `async send(text)` (see module doc)
    #: ``(asker_id, askee_id)`` of the most recent advance. Set by the T10 flow
    #: hook inside the locked advance; the command layer reads it AFTER
    #: ``next()``/``skip()`` returns to build the ``@asker 问 @askee`` announce.
    #: ``None`` until the first advance.
    last_pair: Optional[tuple[str, str]] = None
    #: Player asked in the most recent advance — the lottery's ``prev_askee``
    #: input, so the same player is not asked twice in a row.
    last_askee_id: Optional[str] = None
    #: ``unified_msg_origin`` captured at create (session scoping for the
    #: LLM/LM calls in T10's flow hook). Empty until ``cmd_create`` stores it;
    #: the flow falls back to ``"group:{group_id}"`` when absent.
    umo: str = ""
    #: Round sub-state (v2): ``""`` | ``awaiting_asker_mode`` |
    #: ``awaiting_asker_question`` | ``question_out``.
    round_stage: str = ""
    room_id: str = field(default_factory=lambda: uuid4().hex)
    participants: dict[str, str] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class RoomManager:
    """Owns all rooms and the per-room FSM.

    ``config`` is a plain dict (never imports ``core.config`` — T2 may run in
    parallel). Keys read here, with defaults:

    - ``max_players`` (20), ``min_players`` (2), ``bot_ask_enabled`` (True)
    - ``waiting_timeout_seconds`` (600), ``round_idle_seconds`` (300),
      ``max_idle_rounds`` (3), ``max_questions_per_game`` (30, reserved for T10)
    - ``asker_choice_timeout_seconds`` (60) — human-asker choice window (v2)
    - ``_waiting_check_interval`` (1.0) — internal knob, tests shorten it
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        self._config: dict = dict(config or {})
        # Public alias for command layer / main (complete_and_announce max_questions).
        self.config: dict = self._config
        self.rooms: dict[str, Room] = {}
        self._waiting_tasks: dict[str, asyncio.Task] = {}
        self._idle_tasks: dict[str, asyncio.Task] = {}
        self._choice_tasks: dict[str, asyncio.Task] = {}
        self.outcome_callback: Optional[Callable[[dict], Awaitable[None]]] = None
        self._pending_outcomes: dict[str, dict] = {}
        self._outcome_lock = asyncio.Lock()
        #: Settable question-generation injection point (T10 replaces it).
        #: Signature: ``async (room, manager) -> str``.
        self.advance_hook: Callable[[Room, "RoomManager"], Awaitable[str]] = (
            self._advance_impl
        )
        #: Internal test seam: the round-idle timer calls this (if set) after
        #: capturing ``advance_count`` and BEFORE acquiring the lock. Tests use
        #: it to deterministically interleave the idle timer with a human
        #: advance (Event gates, not sleeps). Not part of the public API.
        self._idle_wake_hook: Optional[Callable[[], Awaitable[None]]] = None
        #: Internal test seam for the choice timer (same pattern as idle).
        #: Called after sleep + capture of ``(advance_count, round_stage)`` and
        #: BEFORE acquiring the lock.
        self._choice_wake_hook: Optional[Callable[[], Awaitable[None]]] = None

    # ------------------------------------------------------------------ config
    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return max(1, int(self._config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return max(0.01, float(self._config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _max_players(self) -> int:
        return self._cfg_int("max_players", 20)

    def _min_players(self) -> int:
        return self._cfg_int("min_players", 2)

    def _bot_ask_enabled(self) -> bool:
        return bool(self._config.get("bot_ask_enabled", True))

    def _waiting_timeout(self) -> float:
        return self._cfg_float("waiting_timeout_seconds", 600.0)

    def _round_idle(self) -> float:
        return self._cfg_float("round_idle_seconds", 300.0)

    def _max_idle_rounds(self) -> int:
        return self._cfg_int("max_idle_rounds", 3)

    def _waiting_check_interval(self) -> float:
        return self._cfg_float("_waiting_check_interval", 1.0)

    def _asker_choice_timeout(self) -> float:
        # T2 may add the config key in parallel; default 60 when absent.
        return self._cfg_float("asker_choice_timeout_seconds", 60.0)

    # ------------------------------------------------------------ sync methods
    def create(
        self, group_id: str, host_id: str, host_name: str, bot: Any = None
    ) -> tuple[bool, str]:
        """Open a room with the host auto-joined as a player.

        FULLY SYNCHRONOUS (no awaits) and therefore atomic in the event loop —
        it needs no lock and cannot interleave. Rejects (with a friendly reason)
        when a room with ``phase in ("waiting", "playing")`` already exists for
        the group. Arms the waiting timer on success.

        Returns ``(True, "")`` or ``(False, reason)``.
        """
        if not group_id:
            return (False, "缺少群信息，无法创建房间。")
        existing = self.rooms.get(group_id)
        if existing is not None:
            if existing.phase == "settled":
                return (False, "上局已结束，成果仍在保存，请稍后开局。")
            return (False, "本群已经有真心话房间了，请直接发送 报名真心话 加入。")
        now = time.time()
        room = Room(
            group_id=group_id,
            host_id=host_id,
            created_at=now,
            last_action_at=now,
            bot=bot,
        )
        room.players[host_id] = Player(user_id=host_id, nickname=host_name)
        room.participants[host_id] = host_name
        self.rooms[group_id] = room
        self._arm_waiting_timer(room)
        return (True, "")

    def join(self, group_id: str, user_id: str, nickname: str) -> tuple[bool, str]:
        """Join a waiting or playing room. SYNCHRONOUS (atomic; no lock).

        Mid-game join (v2): allowed when ``phase=="playing"`` — the new player
        is eligible from the **next** lottery (not mid-current round). Still
        rejects duplicate / full / no-room. Updates ``room.last_action_at``
        (sliding waiting window while waiting).

        Returns ``(True, "")`` or ``(False, reason)``.
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, "本群还没有真心话房间，请先发送 真心话 创建。")
        if room.phase not in ("waiting", "playing"):
            return (False, "当前房间状态无法报名。")
        if user_id in room.players:
            return (False, "你已经报名了，请等待房主开始。")
        if len(room.players) >= self._max_players():
            return (False, f"人数已满（上限 {self._max_players()} 人），无法报名。")
        room.players[user_id] = Player(user_id=user_id, nickname=nickname)
        if room.phase == "playing":
            room.participants[user_id] = nickname
        room.last_action_at = time.time()
        return (True, "")

    def status(self, group_id: str) -> Optional[dict]:
        """Synchronous snapshot for display (no lock; torn reads acceptable)."""
        room = self.rooms.get(group_id)
        if room is None:
            return None
        return {
            "group_id": room.group_id,
            "phase": room.phase,
            "host_id": room.host_id,
            "players": [
                {"user_id": p.user_id, "nickname": p.nickname}
                for p in room.players.values()
            ],
            "advance_count": room.advance_count,
            "idle_streak": room.idle_streak,
            "asked_count": len(room.asked_history),
            "created_at": room.created_at,
            "started_at": room.started_at,
            "round_stage": room.round_stage,
        }

    # ------------------------------------------------------------- locked ops
    async def start(
        self, group_id: str, caller_id: str, is_admin: bool
    ) -> tuple[bool, str]:
        """Start the game. LOCKED. Host-or-admin; already-playing rejected.

        Min-players check: needs ``>= min_players``, OR ``>= 1`` when
        ``bot_ask_enabled`` (the bot can be the asker for a solo player).
        On success: ``phase="playing"``, ``started_at`` set, the waiting timer is
        cancelled, the round-idle timer is armed, and **one** ``_do_advance``
        runs under the same lock (auto first question / choice prompt).

        Returns ``(True, display_text)`` or ``(False, reason)``.
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, "本群还没有真心话房间，请先发送 真心话 创建。")
        if caller_id != room.host_id and not is_admin:
            return (False, "只有房主或管理员可以开始游戏。")
        async with room._lock:
            # identity recheck: lock.acquire() is an await point
            if self.rooms.get(group_id) is not room:
                return (False, "房间状态已变化，请重试。")
            if room.phase == "settled":
                return (False, "上局已结束，成果仍在保存。")
            if room.phase == "playing":
                return (False, "游戏已经开始了。")
            n = len(room.players)
            min_p = self._min_players()
            if n < min_p and not (self._bot_ask_enabled() and n >= 1):
                return (
                    False,
                    f"报名人数不足（至少 {min_p} 人），当前 {n} 人。",
                )
            room.phase = "playing"
            room.participants = {uid: player.nickname for uid, player in room.players.items()}
            room.started_at = time.time()
            self._cancel_waiting_timer(group_id)
            self._arm_idle_timer(room)
            # Auto first advance still holding the lock (v2).
            return await self._do_advance(room)

    async def next(
        self,
        group_id: str,
        caller_id: str,
        is_admin: bool,
        by_human: bool = True,
    ) -> tuple[bool, str]:
        """Advance one round. LOCKED. Only in ``playing``.

        Permission: caller must be a player, the host, or an admin. When
        ``by_human`` (下一题/跳过), ``idle_streak`` is reset to 0. Cancels any
        pending choice timer before the ADVANCE CONTRACT. Returns
        ``(True, question_text)`` on success, or ``(False, reason)`` (including
        ``"房间已结束"`` when the room vanished while the advance hook awaited
        — dropped silently, nothing announced).
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, "本群还没有进行中的真心话房间。")
        if room.phase != "playing":
            return (False, "游戏还未开始，请先发送 开始真心话。")
        if (
            caller_id not in room.players
            and caller_id != room.host_id
            and not is_admin
        ):
            return (False, "只有参与玩家、房主或管理员可以推进下一题。")
        cancelled_choice: Optional[asyncio.Task] = None
        async with room._lock:
            # identity recheck after lock.acquire() (an await point)
            if self.rooms.get(group_id) is not room:
                return (False, "房间状态已变化，请重试。")
            if room.phase != "playing":
                return (False, "游戏还未开始，请先发送 开始真心话。")
            if by_human:
                room.idle_streak = 0
            # cancel BEFORE advance; await AFTER unlock (same as end())
            cancelled_choice = self._cancel_choice_timer(group_id)
            result = await self._do_advance(room)
        if cancelled_choice is not None:
            await asyncio.gather(cancelled_choice, return_exceptions=True)
        return result

    async def skip(
        self, group_id: str, caller_id: str, is_admin: bool
    ) -> tuple[bool, str]:
        """Alias of ``next`` with ``by_human=True`` (identical state change)."""
        return await self.next(group_id, caller_id, is_admin, by_human=True)

    async def leave(
        self, group_id: str, user_id: str
    ) -> tuple[bool, dict]:
        """Leave the room. LOCKED. Any player in ``room.players`` may leave.

        Rules (v2 Scope):
        - Host leaves & others remain → transfer ``host_id`` to
          ``next(iter(room.players))`` (dict insertion order).
        - Current askee leaves while ``last_pair`` set and leaver == pair[1]:
          void round (see below) then auto-next via ``_do_advance`` unless
          min-players forces end first.
        - After removal, if ``n < min_players`` AND NOT
          (``bot_ask_enabled`` and ``n >= 1``): auto-end (cancel timers, pop,
          summary-style text).
        - Host sole player → n=0 → auto-end (no transfer).

        Void-round (askee leave): do NOT decrement ``advance_count``. If
        ``round_stage == "question_out"``, rollback ``ask_counts[askee]`` by 1
        (floor 0) and **keep** last ``asked_history`` entry. Clear
        ``last_pair``, set ``round_stage=""``, cancel choice timer, then
        ``_do_advance``.

        Returns ``(True, payload)`` or ``(False, {"error": reason})``.
        Payload keys (success): ``left_id``, ``ended``, optional
        ``new_host_id``, ``voided``, ``advance_text``, ``summary_text``,
        ``rounds``, ``ask_counts``.
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, {"error": "本群还没有真心话房间。"})
        if user_id not in room.players:
            return (False, {"error": "你不在真心话房间中，无需退出。"})

        cancelled: list[asyncio.Task] = []
        payload: dict[str, Any] = {"left_id": user_id, "ended": False}

        async with room._lock:
            if self.rooms.get(group_id) is not room:
                return (False, {"error": "房间状态已变化，请重试。"})
            if room.phase == "settled":
                return (False, {"error": "上局已结束，成果仍在保存。"})
            if user_id not in room.players:
                return (False, {"error": "你不在真心话房间中，无需退出。"})

            was_host = room.host_id == user_id
            is_askee = (
                room.phase == "playing"
                and room.last_pair is not None
                and user_id == room.last_pair[1]
            )
            void_stage = room.round_stage

            del room.players[user_id]
            room.last_action_at = time.time()

            n = len(room.players)
            min_p = self._min_players()
            should_end = n < min_p and not (self._bot_ask_enabled() and n >= 1)

            if should_end:
                summary, cancelled = self._settle_locked(room, "too_few_players")
                payload.update(summary, ended=True)
            else:
                if was_host and n > 0:
                    new_host = next(iter(room.players))
                    room.host_id = new_host
                    payload["new_host_id"] = new_host

                if is_askee:
                    # Void current round, then auto-next (advance_count stays
                    # monotonic — never decremented).
                    if void_stage == "question_out":
                        room.ask_counts[user_id] = max(
                            0, room.ask_counts.get(user_id, 0) - 1
                        )
                        # keep last asked_history entry (question was shown)
                    room.last_pair = None
                    room.round_stage = ""
                    ct = self._cancel_choice_timer(group_id)
                    if ct is not None:
                        cancelled.append(ct)
                    ok, text = await self._do_advance(room)
                    payload["voided"] = True
                    if ok:
                        payload["advance_text"] = text

        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        if payload["ended"]:
            await self.flush_outcomes()
            self._note_pending_outcome(room, payload)
        return (True, payload)

    async def end(
        self, group_id: str, caller_id: str, is_admin: bool
    ) -> tuple[bool, dict]:
        """End the room. LOCKED. Host-or-admin.

        Cancels this room's timer tasks FIRST, then pops the room. Returns
        ``(True, {"rounds": ..., "ask_counts": {...}})`` — ``rounds`` is
        ``len(asked_history)`` falling back to ``advance_count`` — or
        ``(False, {"error": reason})`` (room persists on permission failure).
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, {"error": "本群还没有进行中的真心话房间。"})
        async with room._lock:
            # identity recheck after lock.acquire() (an await point)
            if self.rooms.get(group_id) is not room:
                return (False, {"error": "房间状态已变化，请重试。"})
            if caller_id != room.host_id and not is_admin:
                return (False, {"error": "只有房主或管理员可以结束房间。"})
            pending = room.phase == "settled"
            if pending:
                snapshot = self._pending_outcomes[room.room_id]
                summary = {key: snapshot[key] for key in ("rounds", "ask_counts", "summary_text")}
                cancelled = []
            else:
                summary, cancelled = self._settle_locked(room, "manual")
        # let the cancelled timer tasks finish processing CancelledError so a
        # stale timer can never wake into a new room reusing this key
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self.flush_outcomes()
        if room.room_id in self._pending_outcomes:
            return (False, {"error": "本局已结束，成果暂未写入活动库，稍后将重试。"})
        if pending:
            return (False, {"error": "上局已结束，成果已保存。"})
        return (True, summary)

    async def end_at_limit(self, room: Room, max_questions: int) -> dict | None:
        async with room._lock:
            if (self.rooms.get(room.group_id) is not room or room.phase != "playing"
                    or len(room.asked_history) < max_questions):
                return None
            summary, cancelled = self._settle_locked(room, "max_questions")
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self.flush_outcomes()
        self._note_pending_outcome(room, summary)
        return summary

    def _settle_locked(self, room: Room, reason: str) -> tuple[dict, list[asyncio.Task]]:
        cancelled = self._cancel_room_timers(room.group_id)
        summary = {
            "rounds": len(room.asked_history) or room.advance_count,
            "ask_counts": dict(room.ask_counts),
            "summary_text": self._summary_text(room),
        }
        participants = [
            {"uid": uid, "name": name} for uid, name in room.participants.items()
        ]
        if (self.outcome_callback is not None and room.phase == "playing"
                and ":GroupMessage:" in room.umo and summary["rounds"] > 0
                and (len(participants) >= 2 or summary["rounds"] >= 2)):
            self._pending_outcomes[room.room_id] = {
                **summary, "room_id": room.room_id, "umo": room.umo,
                "group_id": room.group_id, "participants": participants,
                "created_at": room.created_at, "occurred_at": time.time(),
                "end_reason": reason,
            }
            room.phase = "settled"
        else:
            self.rooms.pop(room.group_id, None)
        return summary, cancelled

    def _note_pending_outcome(self, room: Room, summary: dict) -> None:
        if room.room_id in self._pending_outcomes:
            summary["summary_text"] += "\n成果暂未写入活动库，稍后将重试。"

    async def flush_outcomes(self) -> None:
        if self.outcome_callback is None or not self._pending_outcomes:
            return
        async with self._outcome_lock:
            for room_id, outcome in list(self._pending_outcomes.items()):
                try:
                    await self.outcome_callback(outcome)
                except Exception:
                    logger.exception("Could not archive settled truth room %s", room_id)
                else:
                    self._pending_outcomes.pop(room_id, None)
                    group_id = outcome["group_id"]
                    room = self.rooms.get(group_id)
                    if room and room.room_id == room_id:
                        self.rooms.pop(group_id, None)

    async def terminate(self) -> None:
        """Cancel ALL rooms' timer tasks (plugin unload path) and clear rooms."""
        cancelled: list[asyncio.Task] = []
        for gid in list(self._waiting_tasks):
            task = self._cancel_task(self._waiting_tasks, gid)
            if task is not None:
                cancelled.append(task)
        for gid in list(self._idle_tasks):
            task = self._cancel_task(self._idle_tasks, gid)
            if task is not None:
                cancelled.append(task)
        for gid in list(self._choice_tasks):
            task = self._cancel_task(self._choice_tasks, gid)
            if task is not None:
                cancelled.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self.flush_outcomes()
        if self._pending_outcomes:
            logger.error("Truth rooms still await durable handoff on shutdown: %s",
                         ", ".join(self._pending_outcomes))
        self.rooms.clear()

    # ------------------------------------------------------------ advance hook
    async def _advance_impl(self, room: Room, manager: "RoomManager") -> str:
        """Default advance stub: returns a placeholder question.

        T10 replaces ``manager.advance_hook`` with the real question generator.
        NOTE: the ``advance_count`` increment is owned by the RoomManager
        advance path (see ADVANCE CONTRACT), NOT by this hook — so any injected
        hook keeps the idle-vs-human race-detection mechanism intact.
        """
        return f"（占位问题 · 第 {room.advance_count} 题）"

    async def resolve_asker_bot(self, room: Room) -> str:
        """Bot-resolve path for human-asker choice timeout / 「机器人出题」.

        **Contract (T1 placeholder; T3 wires the real LLM path):**

        * Caller MUST hold ``room._lock`` (choice timer / command path).
        * Does **not** bump ``advance_count`` (pair already drawn this round).
        * Does **not** bump ``idle_streak`` (timeout is not a human action).
        * Bookkeeping: append fallback question to ``asked_history``,
          ``ask_counts[askee] += 1`` when ``last_pair`` is set, set
          ``last_askee_id``, ``round_stage = "question_out"``.
        * Cancels the choice timer for this room.
        * Returns plain question text suitable for ``room.bot.send``.

        T3 will replace the body with enrich + ``build_question`` while keeping
        this method name and the bookkeeping side-effects above.
        """
        question = f"（机器人出题 · 第 {room.advance_count} 题）"
        if room.last_pair is not None:
            askee = room.last_pair[1]
            room.ask_counts[askee] = room.ask_counts.get(askee, 0) + 1
            room.last_askee_id = askee
        room.asked_history.append(question)
        room.round_stage = "question_out"
        self._cancel_choice_timer(room.group_id)
        return question

    def _submit_human_question(self, room: Room, text: str) -> str:
        """Bookkeeping for a human-submitted question (no flow import — cycle).

        Mirrors ``flow.submit_human_question``: bump ``ask_counts[askee]``,
        append history, set ``last_askee_id``, ``round_stage="question_out"``.
        Keeps existing ``last_pair``. Caller owns timer cancel + lock.
        """
        asker = ""
        askee = ""
        if room.last_pair is not None:
            asker, askee = room.last_pair
        q = str(text) if text is not None else ""
        if askee:
            room.ask_counts[askee] = room.ask_counts.get(askee, 0) + 1
            room.last_askee_id = askee
        room.asked_history.append(q)
        if asker or askee:
            room.last_pair = (asker, askee)
        room.round_stage = "question_out"
        return q

    async def choose_asker_mode(
        self, group_id: str, caller_id: str, mode: str
    ) -> tuple[bool, Optional[str]]:
        """Human asker picks self-question vs bot-question. LOCKED.

        * ``mode="self"`` → ``round_stage="awaiting_asker_question"``, re-arm
          choice timer (whole-round timeout covers typing), return
          ``(True, None)`` (caller renders stage-aware choice re-prompt).
        * ``mode="bot"`` → cancel choice timer, ``await resolve_asker_bot``,
          return ``(True, question_text)``.
        * Resets ``idle_streak`` on success (human activity).
        * Wrong state / non-asker → ``(False, reason)``.
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, "房间状态已变化")
        cancelled_choice: Optional[asyncio.Task] = None
        result: tuple[bool, Optional[str]] = (False, "状态已变化")
        async with room._lock:
            if self.rooms.get(group_id) is not room:
                return (False, "房间状态已变化")
            if room.phase != "playing":
                return (False, "状态已变化")
            if room.round_stage not in _AWAITING_ASKER_STAGES:
                return (False, "状态已变化")
            if room.last_pair is None or caller_id != room.last_pair[0]:
                return (False, "还没有轮到你")
            room.idle_streak = 0
            if mode == "self":
                room.round_stage = "awaiting_asker_question"
                # Re-arm: whole-round timeout still covers typing the question.
                self._arm_choice_timer(room)
                result = (True, None)
            elif mode == "bot":
                cancelled_choice = self._cancel_choice_timer(group_id)
                text = await self.resolve_asker_bot(room)
                if (
                    self.rooms.get(group_id) is not room
                    or room.phase != "playing"
                ):
                    result = (False, "房间状态已变化")
                else:
                    result = (True, text)
            else:
                result = (False, "未知选择")
        if cancelled_choice is not None:
            await asyncio.gather(cancelled_choice, return_exceptions=True)
        return result

    async def submit_question(
        self, group_id: str, caller_id: str, text: str
    ) -> tuple[bool, Optional[str]]:
        """Asker submits a self-written question. LOCKED.

        Requires ``round_stage == "awaiting_asker_question"`` and caller ==
        ``last_pair[0]``. Non-empty ``text``; cancels choice timer; runs
        :meth:`_submit_human_question`; resets ``idle_streak``.
        Returns ``(True, question_text)`` or ``(False, reason)``.
        """
        room = self.rooms.get(group_id)
        if room is None:
            return (False, "房间状态已变化")
        stripped = str(text).strip() if text is not None else ""
        if not stripped:
            return (False, "题目不能为空，请重新发送。")
        cancelled_choice: Optional[asyncio.Task] = None
        result: tuple[bool, Optional[str]] = (False, "状态已变化")
        async with room._lock:
            if self.rooms.get(group_id) is not room:
                return (False, "房间状态已变化")
            if room.phase != "playing":
                return (False, "状态已变化")
            if room.round_stage != "awaiting_asker_question":
                return (False, "状态已变化")
            if room.last_pair is None or caller_id != room.last_pair[0]:
                return (False, "还没有轮到你")
            room.idle_streak = 0
            cancelled_choice = self._cancel_choice_timer(group_id)
            q = self._submit_human_question(room, stripped)
            result = (True, q)
        if cancelled_choice is not None:
            await asyncio.gather(cancelled_choice, return_exceptions=True)
        return result

    async def _do_advance(self, room: Room) -> tuple[bool, str]:
        """Run ONE advance. MUST be called with ``room._lock`` held.

        ADVANCE CONTRACT:
          1. ``room.advance_count += 1`` (monotonic race-detection source).
          2. ``text = await self.advance_hook(room, self)``  <- await point.
          3. Identity recheck after the await: ``rooms.get(gid) is room`` AND
             ``phase == "playing"``. On failure return ``(False, "房间已结束")``
             WITHOUT announcing (drop silently).
          4. If hook left the room in ``awaiting_asker_*``, arm the choice timer.

        Returns ``(True, question_text)`` or ``(False, reason)``.
        """
        room.advance_count += 1
        text: Optional[str] = None
        try:
            text = await self.advance_hook(room, self)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "advance_hook failed for group=%s; using placeholder",
                room.group_id,
            )
            text = None
        # after-await identity recheck — the room may have ended mid-generate
        if self.rooms.get(room.group_id) is not room or room.phase != "playing":
            return (False, "房间已结束")
        if text is None or not str(text).strip():
            text = f"（占位问题 · 第 {room.advance_count} 题）"
        if room.round_stage in _AWAITING_ASKER_STAGES:
            self._arm_choice_timer(room)
        return (True, str(text))

    # ---------------------------------------------------------------- timers
    def _arm_waiting_timer(self, room: Room) -> None:
        self._cancel_task(self._waiting_tasks, room.group_id)
        self._waiting_tasks[room.group_id] = asyncio.create_task(
            self._waiting_timer_loop(room.group_id, room)
        )

    def _arm_idle_timer(self, room: Room) -> None:
        self._cancel_task(self._idle_tasks, room.group_id)
        self._idle_tasks[room.group_id] = asyncio.create_task(
            self._idle_timer_loop(room.group_id, room)
        )

    def _arm_choice_timer(self, room: Room) -> None:
        """Arm (or re-arm) the human-asker choice timeout for ``room``."""
        self._cancel_task(self._choice_tasks, room.group_id)
        self._choice_tasks[room.group_id] = asyncio.create_task(
            self._choice_timer_loop(room.group_id, room)
        )

    def _cancel_task(
        self, tasks: dict[str, asyncio.Task], group_id: str
    ) -> Optional[asyncio.Task]:
        """Pop and cancel a timer task; return the cancelled task (or None).

        A timer cancelling itself only detaches from the map — cancelling the
        running task would abort the pop/summary it is currently executing.
        """
        task = tasks.pop(group_id, None)
        if task is None or task.done():
            return None
        try:
            if task is asyncio.current_task():
                return None
        except RuntimeError:
            pass
        task.cancel()
        return task

    def _cancel_waiting_timer(self, group_id: str) -> None:
        self._cancel_task(self._waiting_tasks, group_id)

    def _cancel_idle_timer(self, group_id: str) -> None:
        self._cancel_task(self._idle_tasks, group_id)

    def _cancel_choice_timer(self, group_id: str) -> Optional[asyncio.Task]:
        return self._cancel_task(self._choice_tasks, group_id)

    def _cancel_room_timers(self, group_id: str) -> list[asyncio.Task]:
        """Cancel waiting + idle + choice timer tasks of a room; return the
        cancelled tasks so the caller can await their completion (a cancelled
        task is only 'done' once it has processed the CancelledError)."""
        cancelled: list[asyncio.Task] = []
        for tasks in (self._waiting_tasks, self._idle_tasks, self._choice_tasks):
            task = self._cancel_task(tasks, group_id)
            if task is not None:
                cancelled.append(task)
        return cancelled

    async def _choice_timer_loop(self, group_id: str, self_room: Room) -> None:
        """Human-asker choice timeout loop (ONE task per room while awaiting).

        Sleeps ``asker_choice_timeout_seconds``; on wake CAPTURES
        ``(advance_count, round_stage)``; optional ``_choice_wake_hook`` seam;
        then under the lock: identity + phase + still ``awaiting_asker_*`` +
        advance_count unchanged → ``resolve_asker_bot`` and announce via
        ``room.bot``. Does NOT bump ``idle_streak``. Exits on cancel / room gone.
        """
        try:
            await asyncio.sleep(self._asker_choice_timeout())
        except asyncio.CancelledError:
            return
        if self.rooms.get(group_id) is not self_room:
            return
        if self_room.phase != "playing":
            return
        captured_advance = self_room.advance_count
        captured_stage = self_room.round_stage
        wake_hook = self._choice_wake_hook
        if wake_hook is not None:
            try:
                await wake_hook()
            except asyncio.CancelledError:
                return
        text = ""
        summary = None
        cancelled = []
        try:
            async with self_room._lock:
                if self.rooms.get(group_id) is not self_room:
                    return
                if self_room.phase != "playing":
                    return
                if self_room.advance_count != captured_advance:
                    return  # advanced while we slept → no-op
                if self_room.round_stage not in _AWAITING_ASKER_STAGES:
                    return
                # stage may have moved mode→question; still awaiting is enough
                _ = captured_stage  # captured for race diagnostics / future use
                text = await self.resolve_asker_bot(self_room)
                if self.rooms.get(group_id) is not self_room:
                    return
                if len(self_room.asked_history) >= self._cfg_int("max_questions_per_game", 30):
                    summary, cancelled = self._settle_locked(self_room, "max_questions")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("choice timer failed for group=%s", group_id)
            return
        # announce outside the lock (identity already rechecked)
        if text:
            # Lazy import avoids room↔flow cycle at module load (flow owns
            # round_plain_text; room only needs it for bot.send nicknames).
            from .flow import round_plain_text

            body = round_plain_text(self_room, text, self._config)
            await self._send_text(
                self_room.bot,
                f"⏰ 提问者超时未选择，由机器人出题：{body}",
            )
        if summary is not None:
            if cancelled:
                await asyncio.gather(*cancelled, return_exceptions=True)
            await self.flush_outcomes()
            self._note_pending_outcome(self_room, summary)
            await self._send_text(self_room.bot, summary["summary_text"])

    async def _idle_timer_loop(self, group_id: str, self_room: Room) -> None:
        """Round-idle auto-advance loop (ONE task per room).

        Sleeps ``round_idle_seconds``; on wake CAPTURES ``advance_count``
        (before the lock); then, under the lock: identity recheck + phase check
        + staleness check (``advance_count != captured`` → a human advanced
        while we slept → ABORT this cycle, re-arm). Otherwise cancels any
        choice timer, performs an ADVANCE (which itself bumps
        ``advance_count``), increments ``idle_streak``, announces the new
        question via ``room.bot``, and auto-ends (cancel timers + pop + send
        summary) once ``idle_streak >= max_idle_rounds``. Exits when the room
        is gone (identity check fails) or the task is cancelled.
        """
        while True:
            try:
                await asyncio.sleep(self._round_idle())
            except asyncio.CancelledError:
                return
            if self.rooms.get(group_id) is not self_room:
                return  # room gone or replaced (identity, not key presence)
            if self_room.phase != "playing":
                return
            captured = self_room.advance_count
            wake_hook = self._idle_wake_hook
            if wake_hook is not None:
                try:
                    await wake_hook()
                except asyncio.CancelledError:
                    return
            summary = None
            cancelled = []
            text = ""
            try:
                async with self_room._lock:
                    # identity recheck after lock.acquire() (an await point)
                    if self.rooms.get(group_id) is not self_room:
                        return
                    if self_room.phase != "playing":
                        return
                    if self_room.advance_count != captured:
                        continue  # advanced while we slept/waiting → re-arm
                    self._cancel_choice_timer(group_id)
                    ok, text = await self._do_advance(self_room)
                    if not ok:
                        return  # room ended mid-advance → drop silently
                    self_room.idle_streak += 1
                    if len(self_room.asked_history) >= self._cfg_int("max_questions_per_game", 30):
                        summary, cancelled = self._settle_locked(self_room, "max_questions")
                    elif self_room.idle_streak >= self._max_idle_rounds():
                        summary, cancelled = self._settle_locked(self_room, "idle")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("idle timer failed for group=%s", group_id)
                return
            # announce outside the lock (recheck already passed immediately
            # before this send — no awaits in between)
            await self._send_text(
                self_room.bot, f"⏰ 长时间无人操作，自动进入下一题：{text}"
            )
            if summary is not None:
                if cancelled:
                    await asyncio.gather(*cancelled, return_exceptions=True)
                await self.flush_outcomes()
                self._note_pending_outcome(self_room, summary)
                await self._send_text(self_room.bot, summary["summary_text"])
                return

    async def _waiting_timer_loop(self, group_id: str, self_room: Room) -> None:
        """SLIDING-window waiting close loop (ONE task per room).

        Wakes every ``_waiting_check_interval`` seconds and, under the lock,
        re-validates ``now - room.last_action_at >= waiting_timeout_seconds``.
        A join (which updates ``last_action_at``) resets the window, so the
        room stays open. On expiry: sends the close notice via ``room.bot``
        (await), RE-VALIDATES again (identity + phase + window — a join during
        the send aborts the close), then pops synchronously (no await between
        the final recheck and the pop). Exits when the room is gone.
        """
        while True:
            try:
                await asyncio.sleep(self._waiting_check_interval())
            except asyncio.CancelledError:
                return
            if self.rooms.get(group_id) is not self_room:
                return
            if self_room.phase != "waiting":
                return
            try:
                async with self_room._lock:
                    # identity recheck after lock.acquire() (an await point)
                    if self.rooms.get(group_id) is not self_room:
                        return
                    if self_room.phase != "waiting":
                        return
                    if (
                        time.time() - self_room.last_action_at
                        < self._waiting_timeout()
                    ):
                        continue  # window not expired (yet)
                    await self._send_text(
                        self_room.bot,
                        "⏰ 真心话房间创建后长时间无人报名，已自动关闭，请重新发送 真心话 开房。",
                    )
                    # re-validate after the send await — abort if anything moved
                    if self.rooms.get(group_id) is not self_room:
                        return
                    if self_room.phase != "waiting":
                        return
                    if (
                        time.time() - self_room.last_action_at
                        < self._waiting_timeout()
                    ):
                        continue  # a join during the send reset the window
                    self._cancel_room_timers(group_id)
                    self.rooms.pop(group_id, None)
                    return
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("waiting timer failed for group=%s", group_id)
                return

    # ------------------------------------------------------------------ misc
    async def _send_text(self, bot: Any, text: str) -> None:
        """Duck-typed send: ``bot`` must expose ``send(text)`` returning an
        awaitable (or be ``None``). Failures are logged and swallowed."""
        if bot is None:
            return
        send = getattr(bot, "send", None)
        if not callable(send):
            return
        try:
            maybe = send(text)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            logger.warning("发送房间通知失败（忽略）: %r", text[:40])

    def _summary_text(self, room: Room) -> str:
        """Plain-text game-over summary for the idle auto-end (sent via bot)."""
        rounds = len(room.asked_history) or room.advance_count
        lines = [f"💬 真心话游戏结束，共进行了 {rounds} 轮。"]
        if room.ask_counts:
            lines.append("各玩家被问次数：")
            for uid, cnt in sorted(
                room.ask_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))
            ):
                player = room.players.get(uid)
                name = player.nickname if player is not None else uid
                lines.append(f"{name}：{cnt} 次")
        return "\n".join(lines)
