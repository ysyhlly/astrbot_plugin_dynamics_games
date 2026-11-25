"""LLM + 人格：按人设生成「今天吃什么」回复。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

DEFAULT_LLM_TIMEOUT_SECONDS = 20.0

try:
    from ...series_panels import (
        find_provider_silently,
        get_series_defaults,
        is_provider_budget_busy_error,
        resolve_series_provider,
        series_llm_call,
    )
except Exception:  # pragma: no cover - standalone fallback
    def find_provider_silently(
        context: Any, provider_id: str, *, allow_fallback: bool = False
    ) -> Any:
        _ = allow_fallback
        getter = getattr(context, "get_provider_by_id", None)
        return getter(provider_id) if callable(getter) else None

    def get_series_defaults(_context: Any) -> dict[str, Any]:
        return {}

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

if TYPE_CHECKING:
    from astrbot.api.star import Context

# 任务约束：拼在人设 system_prompt 后面，保证口吻一致且输出可控
_TASK_SYSTEM_SUFFIX = """

【任务补充】
你正在参与群聊/私聊里关于「今天吃什么」的闲聊。
请严格保持你的人设口吻、称呼习惯与性格，用自然口语回复。
硬性要求：
1. 只输出一句简短回复，不要解释、不要列菜单、不要加引号或前后缀
2. 尽量控制在 50 个中文字符以内
3. 不要暴露你是 AI、不要提「人设」「系统提示」
4. 不要使用 markdown 列表或标题
"""

_RECOMMEND_PROMPT = """用户消息：{user_message}
{taste_block}你这次要推荐的食物：{food}
请以你的人设口吻，自然地推荐这份食物（必须点名提到该食物）。
若口味摘要里有忌口/过敏，不要推荐相关内容；若最近推过类似的，可用「换个口味」口吻带过。"""

_ECHO_PROMPT = """用户消息：{user_message}
{taste_block}用户在纠结吃什么。你这次不要推荐具体菜名，只用人设口吻附和一句类似「是啊，吃什么」的共鸣或吐槽。"""

_FALLBACK_PROMPT = """用户消息：{user_message}
{taste_block}暂时想不出具体推荐。请以你的人设口吻，简短表示选不出来、让对方自己决定。"""


class PersonaLLMResponder:
    """解析会话/配置人格，并用 LLM 生成人设化回复。"""

    def __init__(
        self,
        context: Context,
        *,
        enabled: bool = True,
        provider_id: str = "",
        persona_id: str = "",
        use_session_persona: bool = True,
        also_for_echo: bool = True,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
    ) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.provider_id = (provider_id or "").strip()
        self.persona_id = (persona_id or "").strip()
        self.use_session_persona = bool(use_session_persona)
        self.also_for_echo = bool(also_for_echo)
        try:
            self.timeout_seconds = max(0.05, float(timeout_seconds))
        except (TypeError, ValueError):
            self.timeout_seconds = DEFAULT_LLM_TIMEOUT_SECONDS
        logger.info(
            "人格LLM回复器初始化: enabled=%s provider=%s persona=%s "
            "use_session=%s also_for_echo=%s",
            self.enabled,
            self.provider_id or "(session)",
            self.persona_id or "(auto)",
            self.use_session_persona,
            self.also_for_echo,
        )

    def should_use_llm(self, *, for_echo: bool = False) -> bool:
        """是否对本轮回复走 LLM。"""
        if not self.enabled:
            return False
        if for_echo and not self.also_for_echo:
            return False
        return True

    async def resolve_system_prompt(self, umo: str | None) -> str:
        """
        解析人设 system_prompt。

        优先级：
        1. 插件配置指定人格
        2. 会话/对话/默认人格（若开启继承）
        """
        persona_mgr = getattr(self.context, "persona_manager", None)
        if persona_mgr is None:
            return _TASK_SYSTEM_SUFFIX.strip()

        persona_prompt = ""

        # 1) 插件强制指定或调用期懒继承主控共享默认人格
        effective_persona_id = self.persona_id
        if not effective_persona_id:
            try:
                effective_persona_id = str(
                    (get_series_defaults(self.context) or {}).get("persona_id", "")
                    or ""
                ).strip()
            except Exception:
                effective_persona_id = ""
        if effective_persona_id and effective_persona_id != "[%None]":
            persona_prompt = await self._get_persona_prompt(
                persona_mgr, effective_persona_id
            )
            if persona_prompt:
                logger.debug("吃什么插件使用指定/继承人格: %s", effective_persona_id)

        # 2) 继承会话人格
        if not persona_prompt and self.use_session_persona and umo:
            persona_prompt = await self._resolve_session_persona_prompt(
                persona_mgr, str(umo)
            )

        base = (persona_prompt or "").strip()
        if base:
            return f"{base}{_TASK_SYSTEM_SUFFIX}"
        # 无人格时仍带任务约束，保证输出格式
        return _TASK_SYSTEM_SUFFIX.strip()

    async def generate_recommend(
        self,
        *,
        umo: str | None,
        user_message: str,
        food: str,
        taste_summary: str | None = None,
    ) -> str | None:
        """以人设口吻生成推荐文案。"""
        prompt = _RECOMMEND_PROMPT.format(
            user_message=self._safe_user_message(user_message),
            food=food.strip() or "随便吃点",
            taste_block=self._taste_block(taste_summary),
        )
        return await self._generate(umo=umo, prompt=prompt)

    async def generate_echo(
        self,
        *,
        umo: str | None,
        user_message: str,
        taste_summary: str | None = None,
    ) -> str | None:
        """以人设口吻生成附和/复读文案。"""
        prompt = _ECHO_PROMPT.format(
            user_message=self._safe_user_message(user_message),
            taste_block=self._taste_block(taste_summary),
        )
        return await self._generate(umo=umo, prompt=prompt)

    async def generate_fallback(
        self,
        *,
        umo: str | None,
        user_message: str,
        taste_summary: str | None = None,
    ) -> str | None:
        """无可用食物时的人设化兜底。"""
        prompt = _FALLBACK_PROMPT.format(
            user_message=self._safe_user_message(user_message),
            taste_block=self._taste_block(taste_summary),
        )
        return await self._generate(umo=umo, prompt=prompt)

    @staticmethod
    def _taste_block(taste_summary: str | None) -> str:
        text = (taste_summary or "").strip()
        if not text:
            return ""
        # 防止过长塞爆上下文
        if len(text) > 280:
            text = text[:280] + "…"
        return f"该用户口味档案：{text}\n"

    async def _generate(self, *, umo: str | None, prompt: str) -> str | None:
        if not self.enabled:
            return None

        try:
            provider_id = await self._resolve_provider_id(umo)
            if not provider_id:
                logger.warning("吃什么插件：未找到可用 LLM 提供商，回退模板回复")
                return None

            system_prompt = await self.resolve_system_prompt(umo)

            async def _invoke() -> Any:
                async with asyncio.timeout(self.timeout_seconds):
                    return await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                    )

            llm_resp = await series_llm_call(
                self.context,
                provider_id,
                "astrbot_plugin_dynamics_games",
                "games_what_to_eat",
                _invoke,
                acquire_timeout=min(15.0, self.timeout_seconds),
            )
            text = self._extract_text(llm_resp)
            if not text:
                logger.warning("吃什么插件：LLM 返回空内容，回退模板回复")
                return None
            return text
        except TimeoutError:
            logger.warning(
                "吃什么插件：LLM 生成超时 (%.1fs)，回退模板回复",
                self.timeout_seconds,
            )
            return None
        except Exception as exc:
            if is_provider_budget_busy_error(exc):
                logger.warning("吃什么插件：共享模型队列繁忙，回退模板回复")
                return None
            logger.exception("吃什么插件：LLM 生成失败，回退模板回复")
            return None

    async def _resolve_provider_id(self, umo: str | None) -> str | None:
        preferred = resolve_series_provider(
            self.context, self.provider_id, role="reply"
        )
        if preferred:
            # 校验配置的 provider 是否存在（冷启动时静默探测，避免触发宿主缺失告警）
            try:
                prov = find_provider_silently(self.context, preferred)
                if prov is not None:
                    return preferred
                logger.warning(
                    "吃什么插件：配置的 provider 不存在: %s，尝试会话默认",
                    preferred,
                )
            except Exception as e:
                logger.warning("吃什么插件：校验 provider 失败: %s", e)

        if umo:
            try:
                pid = await self.context.get_current_chat_provider_id(str(umo))
                if pid:
                    return pid
            except Exception as e:
                logger.warning("吃什么插件：获取会话 provider 失败: %s", e)

        # 最后尝试 get_using_provider
        try:
            prov = self.context.get_using_provider(umo=umo)
            if prov is None:
                return None
            meta_id = getattr(getattr(prov, "meta", None), "id", None)
            if meta_id:
                return str(meta_id)
            # 部分版本 provider 直接有 id
            pid = getattr(prov, "provider_id", None) or getattr(prov, "id", None)
            return str(pid) if pid else None
        except Exception as e:
            logger.warning("吃什么插件：get_using_provider 失败: %s", e)
            return None

    async def _resolve_session_persona_prompt(
        self, persona_mgr: Any, umo: str
    ) -> str:
        """按会话级 → 对话级 → 默认人格顺序解析。"""
        # 会话级 session_service_config.persona_id
        try:
            from astrbot.api import sp

            session_cfg = (
                await sp.get_async(
                    scope="umo",
                    scope_id=umo,
                    key="session_service_config",
                    default={},
                )
                or {}
            )
            persona_id = session_cfg.get("persona_id")
            if persona_id and persona_id != "[%None]":
                prompt = await self._get_persona_prompt(persona_mgr, str(persona_id))
                if prompt:
                    logger.debug("吃什么插件继承会话人格: %s", persona_id)
                    return prompt
        except Exception as e:
            logger.debug("吃什么插件读取会话人格失败: %s", e)

        # 对话分支级
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                curr_id = await conv_mgr.get_curr_conversation_id(umo)
                if curr_id:
                    conv = await conv_mgr.get_conversation(umo, curr_id)
                    conv_persona = getattr(conv, "persona_id", None) if conv else None
                    if conv_persona and conv_persona != "[%None]":
                        prompt = await self._get_persona_prompt(
                            persona_mgr, str(conv_persona)
                        )
                        if prompt:
                            logger.debug("吃什么插件继承对话人格: %s", conv_persona)
                            return prompt
        except Exception as e:
            logger.debug("吃什么插件读取对话人格失败: %s", e)

        # 全局/UMO 默认
        try:
            default_persona = await persona_mgr.get_default_persona_v3(umo)
            if isinstance(default_persona, dict):
                prompt = (default_persona.get("prompt") or "").strip()
            else:
                prompt = (getattr(default_persona, "prompt", None) or "").strip()
            if prompt:
                logger.debug("吃什么插件使用默认人格")
                return prompt
        except Exception as e:
            logger.debug("吃什么插件读取默认人格失败: %s", e)

        return ""

    @staticmethod
    async def _get_persona_prompt(persona_mgr: Any, persona_id: str) -> str:
        try:
            persona = await persona_mgr.get_persona(persona_id)
            if not persona:
                return ""
            # 新版 Persona 用 system_prompt；部分兼容对象用 prompt
            text = getattr(persona, "system_prompt", None)
            if text is None and isinstance(persona, dict):
                text = persona.get("system_prompt") or persona.get("prompt")
            if text is None:
                text = getattr(persona, "prompt", None)
            return (text or "").strip()
        except Exception as e:
            logger.warning("吃什么插件获取人格失败 id=%s: %s", persona_id, e)
            return ""

    @staticmethod
    def _extract_text(llm_resp: Any) -> str | None:
        if llm_resp is None:
            return None
        text = getattr(llm_resp, "completion_text", None)
        if text is None and isinstance(llm_resp, dict):
            text = llm_resp.get("completion_text") or llm_resp.get("text")
        if not isinstance(text, str):
            return None
        cleaned = text.strip().strip("\"'“”‘’")
        # 去掉常见模型废话前缀
        for prefix in ("回复：", "回复:", "答：", "答:"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
        # 只取第一段，避免模型输出多段
        for sep in ("\n\n", "\n"):
            if sep in cleaned:
                cleaned = cleaned.split(sep, 1)[0].strip()
        # 过长时截断到合理长度（尽量在句号处）
        if len(cleaned) > 80:
            for end in ("。", "！", "？", "!", "?", "…", "~", "～"):
                idx = cleaned.find(end, 10, 80)
                if idx != -1:
                    cleaned = cleaned[: idx + 1]
                    break
            else:
                cleaned = cleaned[:80].rstrip() + "…"
        return cleaned or None

    @staticmethod
    def _safe_user_message(user_message: str, limit: int = 200) -> str:
        msg = (user_message or "").strip().replace("\r\n", "\n")
        if len(msg) > limit:
            msg = msg[:limit] + "…"
        return msg or "（空消息）"
