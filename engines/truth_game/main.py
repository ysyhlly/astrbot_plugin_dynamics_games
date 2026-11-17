from ...runtime import Component
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.api.message_components import Reply as MsgReply
from astrbot.api.star import Context
from astrbot.core.message.message_event_result import MessageEventResult

from .core.commands import (
    Reply,
    cmd_create,
    cmd_end,
    cmd_help,
    cmd_join,
    cmd_leave,
    cmd_next,
    cmd_skip,
    cmd_start,
    cmd_status,
    group_id_of,
    handle_playing_message,
    is_admin_of,
    room_origin_of,
    user_id_of,
)
from .core.flow import build_advance_hook
from .core.room import RoomManager


class TruthGamePlugin(Component):
    """群聊真心话小游戏插件。

    9 个群聊指令（真心话 / 报名真心话 / 开始真心话 / 下一题 / 跳过 /
    退出真心话 / 结束真心话 / 真心话状态 / 真心话帮助）全部叠加
    ``@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)``
    （AND 语义）——私聊消息不会触发任何游戏指令。

    另有一个无 ``@filter.command`` 的 GROUP_MESSAGE catch-all
    （:meth:`on_group_message`）：处理提问者选择关键词、自写出题，以及
    （T7）被提问者引用回复自动下一题。唤醒指令 / 命令前缀 / 机器人自身
    消息在进入 core 前被跳过。

    游戏逻辑（房间调用与回复文案）位于 core/commands.py，本文件只做装饰器
    注册与 Reply → MessageEventResult 的渲染，方便无 AstrBot 环境下的单元
    测试（tests/test_commands.py 通过 astrbot 桩模块直接导入本文件）。

    T10 已将 RoomManager.advance_hook 替换为真实出题链路
    （lottery + question_gen + 记忆增强），下一题/跳过 的回复会自动带出
    提问者与被提问者的 @ 提及。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        # 每群一间房的状态机；config 为 AstrBot 传入的插件配置字典
        self.room_manager = RoomManager(self.config)
        # Public config alias for command-layer complete_and_announce / auto-end.
        self.room_manager.config = self.config
        # T10/v2-T3: 装配真实出题链路（抽人 → 人类选择 / 机器人出题）。
        # 工厂是同步的，可在 __init__（无法 await）中直接赋值；返回的 hook
        # 才是 async (room, manager) -> str，由 RoomManager._do_advance 持锁调用。
        # 副作用：build_advance_hook 同时把 manager.resolve_asker_bot 覆写为
        # 绑定 context/config 的 resolve_bot_question 包装（取消 choice timer +
        # 真实 LLM/题库出题），避免 room.py → flow.py 循环导入。
        self.room_manager.advance_hook = build_advance_hook(
            context, self.config, self.room_manager
        )

    async def terminate(self):
        """插件卸载时清理所有房间计时任务。"""
        try:
            rm = getattr(self, "room_manager", None)
            if rm is not None and hasattr(rm, "terminate"):
                await rm.terminate()
        except Exception as e:
            logger.error(f"TruthGamePlugin terminate 清理房间资源失败: {e}")

    # ------------------------------------------------------------- rendering
    def _render(self, event: AstrMessageEvent, reply: Reply) -> MessageEventResult:
        """把 core/commands.py 的 Reply 渲染成 AstrBot 消息结果。

        纯文本回复直接走 ``event.plain_result``；含 @ 的回复构造
        ``MessageEventResult(chain=[At, Plain, ...])``，遵循 blackjack21
        ``_result`` 的空白规则：At 之后的 Plain 必须以空格开头，纯空白的
        Plain 段补零宽空格（\\u200b）防止被管线剥离。
        """
        if not any(kind == "at" for kind, _ in reply.parts):
            return event.plain_result(reply.text())
        chain: list = []
        for kind, value in reply.parts:
            if kind == "at":
                chain.append(At(qq=str(value)))
                continue
            text = value
            if chain and isinstance(chain[-1], At):
                if not text.startswith((" ", "\u3000")):
                    text = " " + text
            # 纯空白（含单独换行）会被管线 strip 掉，补零宽字符保住换行
            if text and not text.strip():
                text = text + "\u200b"
            if text:
                chain.append(Plain(text))
        return MessageEventResult(chain=chain)

    # --------------------------------------------------------------- commands
    async def cmd_room_create(self, event: AstrMessageEvent):
        """创建房间：发起者自动成为房主并报名。"""
        yield self._render(event, await cmd_create(self.room_manager, event))

    async def cmd_room_join(self, event: AstrMessageEvent):
        """加入等待中的房间。"""
        yield self._render(event, await cmd_join(self.room_manager, event))

    async def cmd_game_start(self, event: AstrMessageEvent):
        """房主或管理员开局（min_players 校验在 RoomManager）。"""
        yield self._render(event, await cmd_start(self.room_manager, event))

    async def cmd_next_question(self, event: AstrMessageEvent):
        """推进一轮（玩家 / 房主 / 管理员）。"""
        yield self._render(event, await cmd_next(self.room_manager, event))

    async def cmd_skip_question(self, event: AstrMessageEvent):
        """与 下一题 相同的状态变更，仅播报前缀不同。"""
        yield self._render(event, await cmd_skip(self.room_manager, event))

    async def cmd_room_leave(self, event: AstrMessageEvent):
        """退出房间（房主转让 / 被提问者作废本轮 / 人数不足自动结束）。"""
        yield self._render(event, await cmd_leave(self.room_manager, event))

    async def cmd_room_end(self, event: AstrMessageEvent):
        """房主或管理员结束游戏并给出总结。"""
        yield self._render(event, await cmd_end(self.room_manager, event))

    async def cmd_room_status(self, event: AstrMessageEvent):
        """查看房间快照（任何人）。"""
        yield self._render(event, await cmd_status(self.room_manager, event))

    async def cmd_help(self, event: AstrMessageEvent):
        """静态规则文本（任何人）。"""
        yield self._render(event, await cmd_help(self.room_manager, event))

    # ----------------------------------------------------- GROUP_MESSAGE catch-all
    async def on_group_message(self, event: AstrMessageEvent):
        """Choice keywords / self-question / (T7) quote-advance.

        Guards (before core): wake-command, ``/`` ``!`` prefix, bot self-id.
        Does not ``stop_event`` on every message — only yields when core
        returns a :class:`Reply`.
        """
        if getattr(event, "is_at_or_wake_command", False):
            return
        text = getattr(event, "message_str", None) or ""
        if text.startswith(("/", "!")):
            return
        get_self = getattr(event, "get_self_id", None)
        if callable(get_self):
            try:
                self_id = get_self()
            except Exception:
                self_id = None
            if self_id is not None:
                sender = None
                try:
                    sender = event.get_sender_id()
                except Exception:
                    sender = getattr(event, "sender_id", None)
                if sender is not None and str(sender) == str(self_id):
                    return

        gid = group_id_of(event)
        uid = user_id_of(event)
        if not gid or not uid:
            return

        try:
            messages = event.get_messages() if hasattr(event, "get_messages") else []
        except Exception:
            messages = []
        has_reply = any(isinstance(c, MsgReply) for c in (messages or []))

        reply = await handle_playing_message(
            self.room_manager,
            gid,
            uid,
            text,
            has_reply,
            is_admin_of(event),
            umo=room_origin_of(event),
        )
        if reply is not None:
            yield self._render(event, reply)
