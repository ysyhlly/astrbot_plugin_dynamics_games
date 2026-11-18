"""T5 command-handler logic and shared text constants for the truth-game plugin.

Design (AstrBot-free seam)
--------------------------
* This module imports NO astrbot symbols — pure stdlib. The ``event`` argument
  of every handler is duck-typed: only ``get_group_id`` / ``get_sender_id`` /
  ``get_sender_name`` / ``is_admin`` / ``bot`` are used, so AstrBot passes a
  real ``AstrMessageEvent`` and tests pass a ``FakeEvent``.
* Every handler returns a :class:`Reply` — an ordered list of parts where a
  part is ``("text", str)`` or ``("at", qq)``. ``main.py`` renders ``Reply``
  into a ``MessageEventResult`` chain (``At`` / ``Plain``) with the blackjack21
  whitespace rules (space after ``At``; zero-width space preserves
  whitespace-only Plain segments).
* ``main.py`` keeps only the ``@filter.command`` +
  ``@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)``
  decorators and the render call — all command logic lives here, so the whole
  T5 surface is unit-testable without an AstrBot runtime.

Identity rules (pinned)
-----------------------
* ``user_id_of`` NEVER falls back to ``event.unified_msg_origin``: the UMO is
  the session identifier ``platform:message_type:group_id`` — identical for
  every member of a group — so using it as a player id would collapse all
  players into one (duplicate-join / ask_counts / lottery all break).
* Nickname fallback ``"用户" + uid[-4:]``; group id ``None``/empty → friendly
  hint and return.

RoomManager contract used (T3): ``create/join/start -> (ok, reason)``;
``next/skip -> (ok, question_text)``;
``end -> (True, {"rounds", "ask_counts"})`` or ``(False, {"error": reason})``;
``status -> dict | None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Part = tuple[str, str]  # ("text", str) | ("at", qq: str)


@dataclass
class Reply:
    """A structured group reply: text parts and/or ``@`` mentions.

    ``main.py`` renders this into an AstrBot message result; tests assert on
    :meth:`text` or on the rendered chain.
    """

    parts: list[Part] = field(default_factory=list)

    @classmethod
    def plain(cls, text: str) -> Reply:
        return cls(parts=[("text", text)])

    def text(self) -> str:
        """Plain-text rendering (main.py uses it for At-free replies)."""
        return "".join(value for _, value in self.parts)


# ------------------------------------------------------------- shared texts
HELP_TEXT = """🎯 真心话 · 群聊真心话小游戏

玩法：报名 → 开局 → 每轮随机抽取玩家，回答 AI 生成的真心话问题。

指令：
  真心话        — 创建房间，发起者自动成为房主并报名
  报名真心话    — 加入房间（开局后也可加入，从下一轮参与）
  开始真心话    — 房主或管理员开局（自动出第一题；默认至少 2 人；开启机器人提问时可 1 人开局）
  下一题        — 玩家、房主或管理员推进到下一题
  跳过          — 跳过当前题目（与 下一题 效果相同）
  退出真心话    — 退出房间（房主退出会转让房主；被提问者退出则本轮作废）
  结束真心话    — 房主或管理员结束游戏并查看总结
  真心话状态    — 查看房间阶段、玩家列表与已出题数
  真心话帮助    — 查看本帮助

说明：
  · 每个群独立一个房间，互不影响
  · 题目由 AI 生成，AI 不可用时自动使用内置题库兜底
  · 本局出过的题目不会立即重复
  · 被提问者可引用任意消息回答，即视为答完并自动进入下一题
  · 房间仅保存在内存中，插件重载后自动重置"""


# ----------------------------------------------------------- event extraction
def group_id_of(event: Any) -> str | None:
    """Group id via ``get_group_id``; ``None``/empty on private chats."""
    try:
        return str(event.get_group_id() or "") or None
    except Exception:
        return None


def user_id_of(event: Any) -> str | None:
    """Sender id via ``get_sender_id`` with ``sender_id`` attribute fallback.

    NEVER falls back to ``event.unified_msg_origin`` (see module docstring).
    """
    uid = None
    try:
        uid = event.get_sender_id()
    except Exception:
        uid = None
    if uid is None:
        uid = getattr(event, "sender_id", None)
    return None if uid is None else str(uid)


def nickname_of(event: Any, uid: str) -> str:
    """Sender nickname via ``get_sender_name``; fallback ``用户<uid尾4位>``."""
    try:
        name = event.get_sender_name()
    except Exception:
        name = None
    if name is None or not str(name).strip():
        return "用户" + str(uid)[-4:] if uid else "未知用户"
    return str(name)


def is_admin_of(event: Any) -> bool:
    try:
        return bool(event.is_admin())
    except Exception:
        return False


def room_origin_of(event: Any) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


def _preflight(
    manager: Any, event: Any, need_uid: bool, *, checks_room: bool = True,
) -> tuple[Reply | None, str | None, str | None]:
    """Common guards; returns ``(failure_reply_or_None, gid, uid)``.

    Order matters: group check first (private chat → hint), then the manager
    presence check (``None`` → "插件尚未完成初始化"), then the identity check.
    """
    gid = group_id_of(event)
    if not gid:
        return (Reply.plain("真心话只能在群里玩哦。"), None, None)
    if manager is None:
        return (Reply.plain("插件尚未完成初始化，请稍后再试。"), None, None)
    if checks_room:
        room = manager.rooms.get(gid)
        if room is not None and room.umo != room_origin_of(event):
            return (Reply.plain("本会话没有可访问的真心话房间。"), None, None)
    uid = None
    if need_uid:
        uid = user_id_of(event)
        if not uid:
            return (Reply.plain("无法获取你的身份信息，请重试。"), None, None)
    return (None, gid, uid)


def _manager_config(manager: Any) -> dict:
    """Plugin config dict as stored on the RoomManager (auto-end / choice timeout).

    Prefers the public ``manager.config`` alias (v2); falls back to private
    ``_config`` for older fixtures.
    """
    cfg = getattr(manager, "config", None)
    if cfg is None:
        cfg = getattr(manager, "_config", None)
    return cfg or {}


async def _announce_reply(
    manager: Any,
    gid: str,
    uid: str,
    is_admin: bool,
    text: str,
    skip_prefix: bool,
) -> Reply:
    """Build the 下一题/跳过 announcement via stage-aware complete_and_announce.

    After ``next()``/``skip()`` the flow hook has stored ``room.last_pair`` and
    ``round_stage``. Choice stages → ``announce_choice``; question path →
    ``announce_round`` + optional ``maybe_auto_end``. Imported at function
    level to keep the import graph acyclic (``core.flow`` imports ``Reply``).
    """
    from .flow import complete_and_announce  # noqa: PLC0415 - cycle break

    return await complete_and_announce(
        manager,
        gid,
        uid,
        is_admin,
        text,
        skip_prefix=skip_prefix,
        config=_manager_config(manager),
    )


# ------------------------------------------------------------------ commands
async def cmd_create(manager: Any, event: Any) -> Reply:
    """真心话 — 创建房间；发起者自动成为房主并加入玩家列表。

    重复开房由 RoomManager 拒绝（提示已存在）。无房可报时回复提示。
    """
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    nickname = nickname_of(event, uid)
    ok, reason = manager.create(gid, uid, nickname, bot=getattr(event, "bot", None))
    if not ok:
        return Reply.plain(reason)
    room = manager.rooms.get(gid)
    if room is not None:
        # T10: capture the session origin so the flow hook can scope its
        # LLM/LM calls (session_id for memory search, provider resolution).
        room.umo = room_origin_of(event)
    return Reply(
        parts=[
            ("text", "🎲 真心话房间已创建！"),
            ("at", uid),
            (
                "text",
                " 成为房主，并已自动报名。\n"
                "其他人请发送 报名真心话 加入。\n"
                "人齐后由房主或管理员发送 开始真心话 开局。\n"
                "规则说明：真心话帮助\n"
                "（创建后长时间无人报名将自动关闭）",
            ),
        ]
    )


async def cmd_join(manager: Any, event: Any) -> Reply:
    """报名真心话 — 加入 waiting 或 playing 房间。

    无房 / 重复报名 / 人数已满 由 RoomManager 给出拒绝原因。
    开局后加入成功时提示从下一轮参与。
    """
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    nickname = nickname_of(event, uid)
    ok, reason = manager.join(gid, uid, nickname)
    if not ok:
        return Reply.plain(reason)
    room = manager.rooms.get(gid)
    count = len(room.players) if room is not None else 1
    if room is not None and getattr(room, "phase", None) == "playing":
        second = "游戏进行中，你将从下一轮开始参与。"
    else:
        second = "等待房主或管理员发送 开始真心话 开局。"
    return Reply(
        parts=[
            ("text", "✅ "),
            ("at", uid),
            (
                "text",
                f" 报名成功！当前共 {count} 人报名。\n{second}",
            ),
        ]
    )


async def cmd_start(manager: Any, event: Any) -> Reply:
    """开始真心话 — 房主或管理员开局，并自动播报第一轮。

    min_players 校验在 RoomManager 内完成：默认至少 2 人；
    ``bot_ask_enabled`` 时 1 人可开局（机器人可当提问者）。
    ``start`` 成功时 ``reason`` 为第一轮展示文本（选择提示或题目）。
    """
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    is_admin = is_admin_of(event)
    ok, reason = await manager.start(gid, uid, is_admin)
    if not ok:
        return Reply.plain(reason)
    room = manager.rooms.get(gid)
    n = len(room.players) if room is not None else 0
    from .flow import complete_and_announce  # noqa: PLC0415 - cycle break

    announce_reply = await complete_and_announce(
        manager,
        gid,
        uid,
        is_admin,
        reason,
        config=_manager_config(manager),
    )
    return Reply(
        parts=[
            ("text", f"🎉 真心话游戏开始！共 {n} 名玩家。\n"),
            *announce_reply.parts,
        ]
    )


async def cmd_leave(manager: Any, event: Any) -> Reply:
    """退出真心话 — 任意已报名玩家退出；按 leave payload 渲染。

    Payload keys (T1): ``left_id``, ``ended``, optional ``new_host_id``,
    ``voided``, ``advance_text``, ``summary_text``, ``rounds``, ``ask_counts``.
    """
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    ok, payload = await manager.leave(gid, uid)
    if not ok:
        return Reply.plain(str(payload.get("error", "退出失败，请稍后再试。")))

    left_id = str(payload.get("left_id") or uid)
    is_admin = is_admin_of(event)

    if payload.get("ended"):
        summary = str(payload.get("summary_text") or "").strip()
        leave_line = f"{left_id} 已退出。"
        if summary:
            return Reply.plain(f"{leave_line}\n{summary}")
        return Reply.plain(leave_line)

    new_host_id = payload.get("new_host_id")
    voided = bool(payload.get("voided"))
    advance_text = payload.get("advance_text")

    if new_host_id:
        reply = Reply(
            parts=[
                ("at", left_id),
                ("text", " 已退出，"),
                ("at", str(new_host_id)),
                ("text", " 成为新房主。"),
            ]
        )
        if voided and advance_text:
            await _append_leave_announce(
                reply, manager, gid, uid, is_admin, str(advance_text)
            )
        return reply

    if voided:
        reply = Reply(
            parts=[
                ("at", left_id),
                ("text", " 已退出，本轮作废，自动进入下一题："),
            ]
        )
        if advance_text:
            await _append_leave_announce(
                reply, manager, gid, uid, is_admin, str(advance_text)
            )
        return reply

    reply = Reply(parts=[("at", left_id), ("text", " 已退出。")])
    if advance_text:
        await _append_leave_announce(
            reply, manager, gid, uid, is_admin, str(advance_text)
        )
    return reply


async def _append_leave_announce(
    reply: Reply,
    manager: Any,
    gid: str,
    uid: str,
    is_admin: bool,
    advance_text: str,
) -> None:
    """Append a stage-aware announce block after a leave notice (mutates reply)."""
    from .flow import complete_and_announce  # noqa: PLC0415 - cycle break

    ann = await complete_and_announce(
        manager,
        gid,
        uid,
        is_admin,
        advance_text,
        config=_manager_config(manager),
    )
    reply.parts.append(("text", "\n"))
    reply.parts.extend(ann.parts)


async def cmd_next(manager: Any, event: Any) -> Reply:
    """下一题 — playing 中推进一轮，播报 @asker 问 @askee。

    权限（玩家 / 房主 / 管理员）与状态校验均由 RoomManager 完成；题目文本
    与提问对被 T10 的 advance_hook 写入房间，成功后本函数读取 ``last_pair``
    构造播报，并在达到题目上限时自动结束游戏并附上总结。
    """
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    ok, text = await manager.next(gid, uid, is_admin_of(event))
    if not ok:
        return Reply.plain(text)
    return await _announce_reply(
        manager, gid, uid, is_admin_of(event), text, skip_prefix=False
    )


async def cmd_skip(manager: Any, event: Any) -> Reply:
    """跳过 — 与 下一题 完全相同的状态变更，仅播报前缀不同。"""
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    ok, text = await manager.skip(gid, uid, is_admin_of(event))
    if not ok:
        return Reply.plain(text)
    return await _announce_reply(
        manager, gid, uid, is_admin_of(event), text, skip_prefix=True
    )


async def cmd_end(manager: Any, event: Any) -> Reply:
    """结束真心话 — 房主或管理员结束并给出总结（轮数 + 每人被问次数）。"""
    fail, gid, uid = _preflight(manager, event, need_uid=True)
    if fail is not None:
        return fail
    assert gid and uid
    ok, summary = await manager.end(gid, uid, is_admin_of(event))
    if not ok:
        return Reply.plain(str(summary.get("error", "结束失败，请稍后再试。")))
    return Reply.plain(summary["summary_text"])


async def cmd_status(manager: Any, event: Any) -> Reply:
    """真心话状态 — 任何人可查看房间快照；无房则提示先开房。"""
    fail, gid, _uid = _preflight(manager, event, need_uid=False)
    if fail is not None:
        return fail
    assert gid
    st = manager.status(gid)
    if st is None:
        return Reply.plain("本群还没有真心话房间，请先发送 真心话 创建。")
    phase = {"playing": "进行中", "settled": "已结束，成果待保存"}.get(
        st.get("phase"), "等待报名中"
    )
    players = st.get("players", []) or []
    host_nick = str(st.get("host_id"))
    for p in players:
        if p.get("user_id") == st.get("host_id"):
            host_nick = str(p.get("nickname") or host_nick)
            break
    names = []
    for p in players:
        label = str(p.get("nickname") or p.get("user_id") or "?")
        if p.get("user_id") == st.get("host_id"):
            label += "（房主）"
        names.append(label)
    return Reply.plain(
        "\n".join(
            [
                "📊 真心话房间状态",
                f"阶段：{phase}",
                f"房主：{host_nick}",
                f"玩家（{len(players)} 人）：{'、'.join(names) or '无'}",
                f"已出题数：{st.get('asked_count', 0)}",
            ]
        )
    )


async def cmd_help(manager: Any, event: Any) -> Reply:
    """真心话帮助 — 任何人可查看静态规则文本（常量在本模块，不在 RoomManager）。"""
    fail, _gid, _uid = _preflight(manager, event, need_uid=False, checks_room=False)
    if fail is not None:
        return fail
    return Reply.plain(HELP_TEXT)


# ---------------------------------------------------- GROUP_MESSAGE catch-all
# Exact-match keywords after strip (ascii ``bot`` via casefold).
_SELF_MODE_KEYWORDS = frozenset({"自己出题", "1"})
_BOT_MODE_KEYWORDS = frozenset({"机器人出题", "2"})


def _is_bot_mode_keyword(stripped: str) -> bool:
    """True when stripped text is 机器人出题 / 2 / bot (casefold)."""
    if stripped in _BOT_MODE_KEYWORDS:
        return True
    return stripped.casefold() == "bot"


async def handle_playing_message(
    manager: Any,
    gid: str,
    uid: str,
    text: str,
    has_reply: bool,
    is_admin: bool,
    *,
    umo: str = "",
) -> Reply | None:
    """Playing-phase free-form handler (choice + self-question + quote-answer).

    AstrBot-free: main.py applies wake/prefix/self-id guards and passes
    ``(manager, gid, uid, text, has_reply, is_admin)``. Returns a :class:`Reply`
    to render, or ``None`` to ignore.

    Branches on ``room.round_stage``:

    * ``awaiting_asker_mode`` — only asker (``last_pair[0]``); exact keywords
      ``自己出题``|``1`` → self mode; ``机器人出题``|``2``|``bot`` → bot resolve.
    * ``awaiting_asker_question`` — only asker; non-empty text → submit;
      empty → plain ``题目不能为空，请重新发送。``.
    * ``question_out`` — askee with any quote (``has_reply``) → ``manager.next``
      (``by_human=True``) + announce; no Reply-target id/sender match required.
      Failures from ``next`` are silent (``None``) to avoid spam on concurrent end.
    """
    if manager is None or not gid or not uid:
        return None
    room = manager.rooms.get(gid)
    if room is None or room.umo != umo or room.phase != "playing":
        return None

    stage = str(getattr(room, "round_stage", "") or "")
    stripped = (text or "").strip()
    pair = getattr(room, "last_pair", None)
    asker_id = str(pair[0]) if pair is not None else None
    askee_id = str(pair[1]) if pair is not None else None

    if stage == "awaiting_asker_mode":
        if asker_id is None or uid != asker_id:
            return None
        if stripped in _SELF_MODE_KEYWORDS:
            ok, qtext = await manager.choose_asker_mode(gid, uid, "self")
            if not ok:
                return None
            return await _announce_reply(
                manager, gid, uid, is_admin, qtext or "", skip_prefix=False
            )
        if _is_bot_mode_keyword(stripped):
            ok, qtext = await manager.choose_asker_mode(gid, uid, "bot")
            if not ok:
                return None
            return await _announce_reply(
                manager, gid, uid, is_admin, qtext or "", skip_prefix=False
            )
        return None

    if stage == "awaiting_asker_question":
        if asker_id is None or uid != asker_id:
            return None
        if not stripped:
            return Reply.plain("题目不能为空，请重新发送。")
        ok, qtext = await manager.submit_question(gid, uid, stripped)
        if not ok:
            return None
        return await _announce_reply(
            manager, gid, uid, is_admin, qtext or stripped, skip_prefix=False
        )

    if stage == "question_out":
        # Quote-answer auto-next: any Reply component from the current askee.
        if not has_reply or askee_id is None or uid != askee_id:
            return None
        ok, qtext = await manager.next(gid, uid, is_admin, by_human=True)
        if not ok:
            # Concurrent end / room gone — stay silent (do not spam the askee).
            return None
        return await _announce_reply(
            manager, gid, uid, is_admin, qtext or "", skip_prefix=False
        )

    return None
