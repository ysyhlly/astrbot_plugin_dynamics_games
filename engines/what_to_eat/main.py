"""吃什么推荐插件主文件。"""

from __future__ import annotations
from ...runtime import Component

import asyncio
import os
import random
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll
from astrbot.api.star import Context, Star

from .food_api import FoodApiClient
from .food_data import FoodDataManager
from .image_manager import ImageManager
from .persona_llm import PersonaLLMResponder
from .rate_limiter import RateLimiter
from .responder import Responder
from .taste_memory import TasteMemoryStore


class WhatToEatPlugin(Component):
    """
    吃什么推荐插件。

    群聊默认仅在被 @ 且消息含「吃什么」等关键词时回复；私聊仍直接识别关键词。
    根据配置概率：
    - 推荐美食（本地菜库 / 可选 HowToCook MCP）
    - 或复读"是啊，吃什么"

    支持：按人记住口味与历史推荐、LLM 人设口吻回复、频率限制、
    复读冷却、回复延迟、食物配图。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        """
        Initialize the plugin.

        Args:
            context: AstrBot context
            config: Plugin configuration
        """
        super().__init__(context)
        self.config = config

        # Read trigger keywords configuration
        trigger_keywords = config.get("trigger_keywords", ["吃什么"])
        if not trigger_keywords or not isinstance(trigger_keywords, list):
            trigger_keywords = ["吃什么"]
        # Filter out empty strings
        self.trigger_keywords = [k for k in trigger_keywords if isinstance(k, str) and k.strip()]
        if not self.trigger_keywords:
            self.trigger_keywords = ["吃什么"]
        # Build regex pattern from keywords
        escaped_keywords = [re.escape(k) for k in self.trigger_keywords]
        self.keyword_pattern = re.compile("|".join(escaped_keywords))
        logger.info(f"关键词配置: {self.trigger_keywords}")

        # 群聊是否必须被 @ / 唤醒后才回复关键词
        self.require_mention = bool(config.get("require_mention", True))

        # Read configuration with defaults
        probability = config.get("recommend_probability", 0.3)

        # Initialize rate limiter
        rate_limit_enabled = config.get("rate_limit_enabled", True)
        rate_limit_max = config.get("rate_limit_max", 3)
        rate_limit_window = config.get("rate_limit_window_seconds", 60)
        echo_cooldown_enabled = config.get("echo_cooldown_enabled", True)
        echo_cooldown_seconds = config.get("echo_cooldown_seconds", 15)
        if rate_limit_enabled:
            self.rate_limiter = RateLimiter(
                max_responses=rate_limit_max,
                window_seconds=rate_limit_window,
                echo_cooldown_enabled=echo_cooldown_enabled,
                echo_cooldown_seconds=echo_cooldown_seconds,
            )
        else:
            self.rate_limiter = None

        # Response delay (thinking effect)
        self.delay_enabled = bool(config.get("response_delay_enabled", True))
        delay_min = float(config.get("response_delay_min_seconds", 0.8))
        delay_max = float(config.get("response_delay_max_seconds", 2.5))
        self.delay_min = max(0.0, min(delay_min, 30.0))
        self.delay_max = max(self.delay_min, min(delay_max, 30.0))
        self.thinking_message_enabled = bool(config.get("thinking_message_enabled", False))

        # Optional HowToCook MCP（兼容旧 api_* 配置键）
        mcp_enabled = bool(
            config.get(
                "mcp_enabled",
                config.get("api_enabled", False),
            )
        )
        mcp_timeout = float(
            config.get(
                "mcp_timeout_seconds",
                config.get("api_timeout_seconds", 10.0),
            )
        )
        mcp_probability = float(
            config.get(
                "mcp_probability",
                config.get("api_probability", 0.35),
            )
        )
        mcp_server_name = str(config.get("mcp_server_name", "cook") or "cook").strip()
        mcp_people_count = int(config.get("mcp_people_count", 1) or 1)
        mcp_mode = str(config.get("mcp_mode", "random") or "random")
        mcp_categories_raw = config.get("mcp_categories", None)
        mcp_categories: list[str] | None
        if isinstance(mcp_categories_raw, list):
            mcp_categories = [
                c for c in mcp_categories_raw if isinstance(c, str) and c.strip()
            ]
        else:
            mcp_categories = None
        mcp_remote_url = str(config.get("mcp_remote_url", "") or "").strip()
        mcp_remote_transport = str(
            config.get("mcp_remote_transport", "sse") or "sse"
        ).strip()

        self.food_api = FoodApiClient(
            context=context,
            enabled=mcp_enabled,
            timeout_seconds=mcp_timeout,
            server_name=mcp_server_name,
            people_count=mcp_people_count,
            mode=mcp_mode,
            categories=mcp_categories,
            remote_url=mcp_remote_url,
            remote_transport=mcp_remote_transport,
        )

        # 用户口味记忆（按人）
        taste_enabled = bool(config.get("taste_memory_enabled", True))
        taste_max_recent = int(config.get("taste_max_recent", 20) or 20)
        taste_avoid_recent = int(config.get("taste_avoid_recent_count", 6) or 6)
        taste_learn = bool(config.get("taste_learn_from_message", True))
        self.taste_memory = TasteMemoryStore(
            enabled=taste_enabled,
            max_recent=taste_max_recent,
            avoid_recent_count=taste_avoid_recent,
            learn_from_message=taste_learn,
        )

        # Initialize components
        self.food_manager = FoodDataManager(
            config,
            api_client=self.food_api,
            api_probability=mcp_probability if mcp_enabled else 0.0,
            taste_memory=self.taste_memory if taste_enabled else None,
        )
        self.responder = Responder(probability)

        # LLM + 人格（失败时自动回退模板）
        llm_enabled = bool(config.get("llm_enabled", True))
        llm_provider_id = str(config.get("llm_provider_id", "") or "")
        llm_persona_id = str(config.get("llm_persona_id", "") or "")
        llm_use_session_persona = bool(config.get("llm_use_session_persona", True))
        llm_also_for_echo = bool(config.get("llm_also_for_echo", True))
        self.persona_llm = PersonaLLMResponder(
            context,
            enabled=llm_enabled,
            provider_id=llm_provider_id,
            persona_id=llm_persona_id,
            use_session_persona=llm_use_session_persona,
            also_for_echo=llm_also_for_echo,
        )

        # Initialize image manager
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.image_manager = ImageManager(config, plugin_dir)

        logger.info(
            f"吃什么插件初始化成功: delay={self.delay_enabled} "
            f"({self.delay_min}-{self.delay_max}s), mcp={mcp_enabled}, "
            f"mcp_server={mcp_server_name}, mcp_mode={mcp_mode}, "
            f"taste={taste_enabled}, llm={llm_enabled}, "
            f"require_mention={self.require_mention}"
        )

    async def _maybe_delay(self, event: AstrMessageEvent, for_recommend: bool):
        """
        应用回复延迟，可选先发一条「思考中」提示。

        Yields:
            可选的思考提示消息
        """
        if not self.delay_enabled:
            return

        # 仅在推荐食物时发送思考提示，避免复读也刷屏
        if for_recommend and self.thinking_message_enabled:
            yield event.plain_result(self.responder.get_thinking_message())

        delay = random.uniform(self.delay_min, self.delay_max)
        if delay > 0:
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # 口味管理指令
    # ------------------------------------------------------------------

    async def cmd_taste_show(self, event: AstrMessageEvent):
        """查看自己的口味档案与最近推荐。"""
        event.should_call_llm(True)
        if not self.taste_memory.enabled:
            yield event.plain_result("口味记忆未启用。")
            event.stop_event()
            return
        user_id = str(event.get_sender_id() or "")
        nick = self._get_sender_name(event)
        self.taste_memory.touch_nickname(user_id, nick)
        profile = self.taste_memory.get_profile(user_id)
        yield event.plain_result(self._format_taste_card(profile))
        event.stop_event()

    async def cmd_taste_like(self, event: AstrMessageEvent):
        """记录喜欢的口味/菜，用法：喜欢吃 麻辣 面食"""
        event.should_call_llm(True)
        if not self.taste_memory.enabled:
            yield event.plain_result("口味记忆未启用。")
            event.stop_event()
            return
        items = self._parse_command_items(event, prefixes=("喜欢吃", "爱吃"))
        if not items:
            yield event.plain_result("用法：喜欢吃 麻辣 黄焖鸡\n可写多个，空格或顿号分隔。")
            event.stop_event()
            return
        user_id = str(event.get_sender_id() or "")
        self.taste_memory.touch_nickname(user_id, self._get_sender_name(event))
        added = self.taste_memory.add_likes(user_id, items)
        if added:
            yield event.plain_result("已记住你喜欢：" + "、".join(added))
        else:
            yield event.plain_result("这些喜好像已经记过了，或格式不对。")
        event.stop_event()

    async def cmd_taste_dislike(self, event: AstrMessageEvent):
        """记录不吃/不喜欢，用法：不吃 香菜 苦瓜"""
        event.should_call_llm(True)
        if not self.taste_memory.enabled:
            yield event.plain_result("口味记忆未启用。")
            event.stop_event()
            return
        items = self._parse_command_items(event, prefixes=("不吃", "不喜欢", "忌口"))
        if not items:
            yield event.plain_result("用法：不吃 香菜 苦瓜\n可写多个，空格或顿号分隔。")
            event.stop_event()
            return
        user_id = str(event.get_sender_id() or "")
        self.taste_memory.touch_nickname(user_id, self._get_sender_name(event))
        added = self.taste_memory.add_dislikes(user_id, items)
        if added:
            yield event.plain_result("已记住你不吃：" + "、".join(added))
        else:
            yield event.plain_result("这些忌口好像已经记过了，或格式不对。")
        event.stop_event()

    async def cmd_taste_allergy(self, event: AstrMessageEvent):
        """记录过敏原，用法：过敏 虾 花生"""
        event.should_call_llm(True)
        if not self.taste_memory.enabled:
            yield event.plain_result("口味记忆未启用。")
            event.stop_event()
            return
        items = self._parse_command_items(event, prefixes=("过敏",))
        if not items:
            yield event.plain_result("用法：过敏 虾 花生")
            event.stop_event()
            return
        user_id = str(event.get_sender_id() or "")
        self.taste_memory.touch_nickname(user_id, self._get_sender_name(event))
        added = self.taste_memory.add_allergies(user_id, items)
        if added:
            yield event.plain_result("已记住过敏：" + "、".join(added))
        else:
            yield event.plain_result("这些过敏原好像已经记过了，或格式不对。")
        event.stop_event()

    async def cmd_taste_clear(self, event: AstrMessageEvent):
        """清空自己的口味档案与推荐历史。"""
        event.should_call_llm(True)
        if not self.taste_memory.enabled:
            yield event.plain_result("口味记忆未启用。")
            event.stop_event()
            return
        user_id = str(event.get_sender_id() or "")
        ok = self.taste_memory.clear_profile(user_id)
        if ok:
            yield event.plain_result("已清空你的口味档案。")
        else:
            yield event.plain_result("你还没有口味档案。")
        event.stop_event()

    async def on_what_to_eat(self, event: AstrMessageEvent, *args, **kwargs):
        """
        处理消息：先做口味学习，再按关键词回复。

        群聊默认仅在被 @（或唤醒词 / 回复机器人）且含关键词时回复。

        Args:
            event: 消息事件
            **kwargs: AstrBot 框架传入的额外参数
        """
        # 获取消息文本内容
        message_text = event.message_str
        if not message_text:
            return

        user_id = str(event.get_sender_id() or "")
        nickname = self._get_sender_name(event)

        # 闲聊中自动学习口味 / 对最近推荐做反馈（不阻断其它插件）
        if self.taste_memory.enabled and user_id:
            # 指令消息跳过自动学习，避免和命令重复
            if not self._looks_like_taste_command(message_text):
                learned = self.taste_memory.learn_from_message(user_id, message_text)
                if any(learned.get(k) for k in ("likes", "dislikes", "allergies", "feedback")):
                    logger.debug("口味自动学习 user=%s: %s", user_id, learned)

        # 检查是否匹配关键词
        if not self.keyword_pattern.search(message_text):
            return

        # 群聊默认只在被 @（或唤醒词 / 回复机器人）时才插话
        if self.require_mention and not self._is_bot_addressed(event):
            logger.debug("关键词命中但未被@，跳过回复: %s", message_text[:50])
            return

        try:
            # 立即阻止默认 LLM 请求
            # 注意：在 AstrBot 中，调用前会检查 'if not event.call_llm'
            # 所以设置 call_llm=True 实际上是阻止默认 LLM 被调用
            event.should_call_llm(True)

            # 获取群组 ID 用于频率限制
            group_id = event.get_group_id()
            if not group_id:
                # 私聊，使用发送者 ID
                group_id = event.get_sender_id()

            # 检查频率限制和复读冷却（原子操作）
            force_recommend = False
            echo_cooldown_active = False
            if self.rate_limiter:
                # 使用原子操作检查并记录，避免竞态条件
                _, force_recommend = self.rate_limiter.check_and_record(group_id)
                echo_cooldown_active = self.rate_limiter.is_in_echo_cooldown(group_id)

            # 决定是否推荐食物
            should_recommend = (
                force_recommend or echo_cooldown_active or self.responder.should_recommend()
            )

            # 思考延迟（推荐时可选先发提示）
            async for thinking_result in self._maybe_delay(event, should_recommend):
                yield thinking_result

            umo = event.unified_msg_origin
            taste_summary = None
            if self.taste_memory.enabled and user_id:
                self.taste_memory.touch_nickname(user_id, nickname)
                taste_summary = self.taste_memory.get_profile(user_id).summary_text()

            if should_recommend:
                # 推荐食物（强制或按概率）：本地菜库 + 可选 HowToCook MCP + 口味
                if self.food_manager.has_foods() or (self.food_api and self.food_api.enabled):
                    food = await self.food_manager.get_recommend_food(user_id=user_id)
                    response_text = await self._compose_recommend_text(
                        umo=umo,
                        user_message=message_text,
                        food=food,
                        taste_summary=taste_summary,
                    )

                    if food and self.taste_memory.enabled and user_id:
                        self.taste_memory.record_recommendation(
                            user_id,
                            food,
                            group_id=str(group_id or ""),
                            nickname=nickname,
                        )

                    # 尝试获取食物图片（仅本地菜名能匹配到配图）
                    image_path = self.image_manager.get_random_image(food) if food else None

                    if image_path:
                        yield event.make_result().message(response_text).file_image(image_path)
                    else:
                        yield event.plain_result(response_text)
                else:
                    response_text = await self._compose_fallback_text(
                        umo=umo,
                        user_message=message_text,
                        taste_summary=taste_summary,
                    )
                    yield event.plain_result(response_text)
            else:
                # 复读/附和（不发送图片，保持简洁）
                response_text = await self._compose_echo_text(
                    umo=umo,
                    user_message=message_text,
                    taste_summary=taste_summary,
                )
                yield event.plain_result(response_text)
                # 记录复读时间用于冷却追踪
                if self.rate_limiter:
                    self.rate_limiter.record_echo(group_id)

            event.stop_event()

        except Exception:
            logger.exception("吃什么插件发生错误")
            try:
                yield event.plain_result("哎呀，出错了...")
                event.stop_event()
            except Exception as inner_e:
                logger.error(f"发送错误消息失败: {inner_e}")
                error_msg = str(inner_e).lower()
                if any(kw in error_msg for kw in ["event", "yield", "generator", "async"]):
                    logger.critical(f"检测到事件系统错误: {inner_e}")

    def _is_bot_addressed(self, event: AstrMessageEvent) -> bool:
        """判断这条消息是否在对机器人说话。

        私聊视为已对机器人说话；群聊要求 @机器人、@全体、回复机器人，
        或带有 AstrBot 唤醒词（is_at_or_wake_command）。
        """
        try:
            if event.is_private_chat():
                return True
        except Exception:
            pass
        if getattr(event, "is_at_or_wake_command", False):
            return True
        return self._message_mentions_bot(event)

    @staticmethod
    def _message_mentions_bot(event: AstrMessageEvent) -> bool:
        """从消息链检查是否 @ 了机器人或全体成员。"""
        bot_id = ""
        try:
            bot_id = str(event.get_self_id() or "").strip()
        except Exception:
            bot_id = ""
        try:
            messages = event.get_messages() or []
        except Exception:
            message_obj = getattr(event, "message_obj", None)
            messages = getattr(message_obj, "message", None) or []
        for comp in messages:
            if isinstance(comp, AtAll):
                return True
            if isinstance(comp, At):
                qq = str(
                    getattr(comp, "qq", "") or getattr(comp, "user_id", "") or ""
                ).strip()
                if qq in ("all", "0"):
                    return True
                if bot_id and qq == bot_id:
                    return True
        return False

    def _get_sender_name(self, event: AstrMessageEvent) -> str:
        for attr in ("get_sender_name", "get_sender_nickname"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    name = fn()
                    if isinstance(name, str) and name.strip():
                        return name.strip()
                except Exception:
                    pass
        try:
            msg = getattr(event, "message_obj", None)
            sender = getattr(msg, "sender", None)
            nick = getattr(sender, "nickname", None) or getattr(sender, "name", None)
            if isinstance(nick, str) and nick.strip():
                return nick.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _looks_like_taste_command(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        # 去掉命令前缀
        t = re.sub(r"^[/／!！.。]", "", t)
        heads = (
            "口味",
            "我的口味",
            "口味档案",
            "喜欢吃",
            "喜欢",
            "爱吃",
            "不吃",
            "不喜欢",
            "忌口",
            "过敏",
            "清空口味",
            "重置口味",
        )
        return any(t == h or t.startswith(h + " ") or t.startswith(h + "　") for h in heads)

    @staticmethod
    def _parse_command_items(event: AstrMessageEvent, *, prefixes: tuple[str, ...]) -> list[str]:
        text = (event.message_str or "").strip()
        text = re.sub(r"^[/／!！.。]", "", text)
        for p in sorted(prefixes, key=len, reverse=True):
            if text.startswith(p):
                text = text[len(p) :].strip()
                break
        if not text:
            return []
        parts = re.split(r"[\s,，、；;+/]+", text)
        return [x.strip() for x in parts if x and x.strip()]

    @staticmethod
    def _format_taste_card(profile) -> str:
        lines = ["【你的口味档案】"]
        if profile.nickname:
            lines.append(f"昵称：{profile.nickname}")
        lines.append(
            "喜欢：" + ("、".join(profile.likes) if profile.likes else "（还没有）")
        )
        lines.append(
            "不吃：" + ("、".join(profile.dislikes) if profile.dislikes else "（还没有）")
        )
        lines.append(
            "过敏：" + ("、".join(profile.allergies) if profile.allergies else "（还没有）")
        )
        recent = profile.recent_foods(8)
        lines.append("最近推荐：" + ("、".join(recent) if recent else "（还没有）"))
        lines.append("")
        lines.append("设置：喜欢吃 / 不吃 / 过敏 …")
        lines.append("也可直接说「我不吃香菜」「我喜欢麻辣烫」")
        lines.append("对推荐反馈「好吃」「换一个」也会记住")
        return "\n".join(lines)

    async def _compose_recommend_text(
        self,
        *,
        umo: str | None,
        user_message: str,
        food: str | None,
        taste_summary: str | None = None,
    ) -> str:
        """生成推荐文案：优先 LLM 人设，失败回退模板。"""
        if food and self.persona_llm.should_use_llm(for_echo=False):
            llm_text = await self.persona_llm.generate_recommend(
                umo=umo,
                user_message=user_message,
                food=food,
                taste_summary=taste_summary,
            )
            if llm_text:
                return llm_text
        return self.responder.get_food_response(food)

    async def _compose_echo_text(
        self,
        *,
        umo: str | None,
        user_message: str,
        taste_summary: str | None = None,
    ) -> str:
        """生成附和文案：可选 LLM 人设，失败回退固定复读。"""
        if self.persona_llm.should_use_llm(for_echo=True):
            llm_text = await self.persona_llm.generate_echo(
                umo=umo,
                user_message=user_message,
                taste_summary=taste_summary,
            )
            if llm_text:
                return llm_text
        return self.responder.get_echo_response()

    async def _compose_fallback_text(
        self,
        *,
        umo: str | None,
        user_message: str,
        taste_summary: str | None = None,
    ) -> str:
        """无食物时的兜底文案。"""
        if self.persona_llm.should_use_llm(for_echo=False):
            llm_text = await self.persona_llm.generate_fallback(
                umo=umo,
                user_message=user_message,
                taste_summary=taste_summary,
            )
            if llm_text:
                return llm_text
        return self.responder.get_fallback_response()

    async def terminate(self) -> None:
        """插件卸载时调用。"""
        try:
            if self.food_api is not None:
                await self.food_api.close()
        except Exception as e:
            logger.debug(f"关闭 MCP 客户端时出错: {e}")
        logger.info("吃什么插件已终止")
