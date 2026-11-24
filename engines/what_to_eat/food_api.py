"""HowToCook MCP 客户端（可选外部菜谱数据源）。

接入 AstrBot 已配置的 HowToCook MCP（ModelScope 广场 looklookyou/How-To-Cook，
或本地 npx howtocook-mcp），用于替代原先的 TheMealDB HTTP API。

优先路径：
1. 通过 AstrBot FunctionToolManager 已建立的 MCPClient 调用工具
2. 可选：按配置的远程 SSE/HTTP URL 直连
3. 失败时由调用方回退本地菜库
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.star import Context

# HowToCook-MCP 官方工具名（worryzyy/howtocook-mcp / ModelScope How-To-Cook）
TOOL_WHAT_TO_EAT = "mcp_howtocook_whatToEat"
TOOL_BY_CATEGORY = "mcp_howtocook_getRecipesByCategory"
TOOL_GET_RECIPE = "mcp_howtocook_getRecipeById"
TOOL_RECOMMEND_MEALS = "mcp_howtocook_recommendMeals"
TOOL_ALL_RECIPES = "mcp_howtocook_getAllRecipes"

# 常见中文分类（与 HowToCook 数据源对齐）
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "荤菜",
    "素菜",
    "水产",
    "早餐",
    "主食",
    "汤羹",
    "甜品",
    "饮品",
)

# 默认候选 MCP 服务名（AstrBot mcp_server.json 中的 key）
DEFAULT_SERVER_CANDIDATES: tuple[str, ...] = (
    "cook",
    "howtocook-mcp",
    "How-To-Cook",
    "howtocook",
    "HowToCook",
)


class FoodApiClient:
    """
    HowToCook MCP 客户端。

    对外保持 get_random_food() 接口，供 FoodDataManager 按概率调用。
    默认优先从全量菜谱 / 分类中真正随机抽菜，并避开最近抽过的结果。
    """

    # 进程内避开最近 N 道，减轻 MCP 反复推同一道菜的问题
    _RECENT_PICK_LIMIT = 24

    def __init__(
        self,
        context: Context | None = None,
        enabled: bool = False,
        timeout_seconds: float = 10.0,
        server_name: str = "cook",
        people_count: int = 1,
        mode: str = "random",
        categories: list[str] | None = None,
        remote_url: str = "",
        remote_transport: str = "sse",
        remote_headers: dict[str, str] | None = None,
    ) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.timeout_seconds = max(1.0, min(60.0, float(timeout_seconds)))
        self.server_name = (server_name or "").strip()
        self.people_count = max(1, min(10, int(people_count or 1)))
        mode_norm = (mode or "random").strip().lower()
        # random: 全量菜谱随机；category: 分类随机；
        # what_to_eat: 按人数出菜单再抽；auto: 在 random/category 间切换
        if mode_norm not in {"what_to_eat", "category", "auto", "random"}:
            mode_norm = "random"
        self.mode = mode_norm

        raw_cats = categories if categories else list(DEFAULT_CATEGORIES)
        self.categories = [
            c.strip()
            for c in raw_cats
            if isinstance(c, str) and c.strip()
        ] or list(DEFAULT_CATEGORIES)

        self.remote_url = (remote_url or "").strip()
        transport = (remote_transport or "sse").strip().lower()
        if transport in {"http", "streamable_http", "streamable-http"}:
            transport = "streamable_http"
        elif transport != "sse":
            transport = "sse"
        self.remote_transport = transport
        self.remote_headers = dict(remote_headers or {})

        # 分类菜名缓存：category -> [name, ...]
        self._category_cache: dict[str, list[str]] = {}
        # 全量菜谱名缓存（getAllRecipes）
        self._all_recipes_cache: list[str] | None = None
        # 最近抽出的菜（归一化 key），用于去重
        self._recent_picks: list[str] = []
        # 懒连接的独立 MCP 客户端（仅 remote_url 模式）
        self._remote_client: Any = None
        self._remote_lock = asyncio.Lock()

        logger.info(
            "HowToCook MCP 客户端初始化: enabled=%s, timeout=%ss, server=%s, "
            "mode=%s, people=%s, remote=%s",
            self.enabled,
            self.timeout_seconds,
            self.server_name or "(auto)",
            self.mode,
            self.people_count,
            bool(self.remote_url),
        )

    def set_name_translator(self, translator: Any) -> None:
        """兼容旧接口：HowToCook 为中文菜谱，无需翻译，忽略注入。"""
        return

    async def get_random_food(self) -> str | None:
        """
        从 HowToCook MCP 获取一道随机菜。

        Returns:
            适合展示的菜名；失败或未启用时返回 None
        """
        if not self.enabled:
            return None

        try:
            # 多路径尝试，优先真正随机，失败再回退
            strategies: list[Any]
            if self.mode == "category":
                strategies = [
                    self._get_from_category,
                    self._get_from_all_recipes,
                    self._get_from_what_to_eat,
                ]
            elif self.mode == "what_to_eat":
                strategies = [
                    self._get_from_what_to_eat,
                    self._get_from_all_recipes,
                    self._get_from_category,
                ]
            elif self.mode == "auto":
                # 70% 全量/分类随机，30% 菜单组合，避免总走同一路径
                if random.random() < 0.7:
                    strategies = [
                        self._get_from_all_recipes,
                        self._get_from_category,
                        self._get_from_what_to_eat,
                    ]
                else:
                    strategies = [
                        self._get_from_what_to_eat,
                        self._get_from_category,
                        self._get_from_all_recipes,
                    ]
            else:
                # 默认 random：全量菜谱真正随机
                strategies = [
                    self._get_from_all_recipes,
                    self._get_from_category,
                    self._get_from_what_to_eat,
                ]

            # 每条策略可多试几次，跳过最近刚推过的
            for strategy in strategies:
                for _ in range(3):
                    food = await strategy()
                    if not food:
                        break
                    if self._is_recent_pick(food):
                        logger.debug("MCP 结果与最近重复，换一道: %s", food)
                        continue
                    self._remember_pick(food)
                    return food

            return None
        except Exception as e:
            logger.warning("HowToCook MCP 请求失败，将回退本地菜库: %s", e)
            return None

    def _is_recent_pick(self, food: str) -> bool:
        key = self._normalize_pick_key(food)
        return bool(key) and key in self._recent_picks

    def _remember_pick(self, food: str) -> None:
        key = self._normalize_pick_key(food)
        if not key:
            return
        self._recent_picks = [k for k in self._recent_picks if k != key]
        self._recent_picks.append(key)
        if len(self._recent_picks) > self._RECENT_PICK_LIMIT:
            self._recent_picks = self._recent_picks[-self._RECENT_PICK_LIMIT :]

    @classmethod
    def _normalize_pick_key(cls, food: str) -> str:
        return cls._clean_recipe_name(food).strip().lower()

    @staticmethod
    def _clean_recipe_name(name: str) -> str:
        """去掉菜谱站点常见后缀，展示更自然。"""
        text = (name or "").strip()
        if not text:
            return ""
        # 去掉括号分类后再清后缀，最后再拼回分类由 _format_display_name 处理
        text = re.sub(r"[（(].*?[）)]", "", text).strip()
        for suffix in ("的做法", "做法", "的详细做法", "食谱", "菜谱"):
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)].strip()
                break
        return text

    async def _get_from_what_to_eat(self) -> str | None:
        """调用 mcp_howtocook_whatToEat，从推荐菜单中抽一道。"""
        raw = await self._call_tool(
            TOOL_WHAT_TO_EAT,
            {"peopleCount": self.people_count},
        )
        data = self._parse_json_payload(raw)
        if data is None:
            return None

        dishes = self._extract_dishes(data)
        if not dishes:
            return None

        # 优先从未推过的候选里抽
        candidates = list(dishes)
        random.shuffle(candidates)
        for dish in candidates:
            formatted = self._format_dish(dish)
            if formatted and not self._is_recent_pick(formatted):
                return formatted
        # 全部都推过时仍返回一道
        return self._format_dish(random.choice(dishes))

    async def _get_from_category(self) -> str | None:
        """按分类随机取一道菜。"""
        categories = list(self.categories)
        random.shuffle(categories)

        fallback: str | None = None
        for category in categories:
            names = await self._get_category_names(category)
            if not names:
                continue
            shuffled = list(names)
            random.shuffle(shuffled)
            for name in shuffled:
                display = self._format_display_name(name, category)
                if not display:
                    continue
                if fallback is None:
                    fallback = display
                if not self._is_recent_pick(display):
                    return display
            # 该分类最近都推过，继续试下一个分类
        return fallback

    async def _get_from_all_recipes(self) -> str | None:
        """从 HowToCook 全量菜谱中真正随机抽一道。"""
        names = await self._get_all_recipe_names()
        if not names:
            return None
        shuffled = list(names)
        random.shuffle(shuffled)
        for name in shuffled:
            display = self._format_display_name(name)
            if display and not self._is_recent_pick(display):
                return display
        return self._format_display_name(random.choice(names))

    async def _get_all_recipe_names(self) -> list[str]:
        """获取全量菜名（带内存缓存）。"""
        if self._all_recipes_cache is not None:
            return self._all_recipes_cache

        raw = await self._call_tool(
            TOOL_ALL_RECIPES,
            {"no_param": ""},
        )
        data = self._parse_json_payload(raw)
        names = self._extract_names_from_list(data)
        # 清洗后缀后去重
        cleaned = list(
            dict.fromkeys(
                n for n in (self._clean_recipe_name(x) for x in names) if n
            )
        )
        if cleaned:
            self._all_recipes_cache = cleaned
            logger.info("HowToCook 全量菜谱缓存完成: %s 道", len(cleaned))
        return cleaned

    async def _get_category_names(self, category: str) -> list[str]:
        """获取某分类下菜名（带内存缓存）。"""
        if category in self._category_cache:
            return self._category_cache[category]

        raw = await self._call_tool(
            TOOL_BY_CATEGORY,
            {"category": category},
        )
        data = self._parse_json_payload(raw)
        names = [
            n
            for n in (self._clean_recipe_name(x) for x in self._extract_names_from_list(data))
            if n
        ]
        if names:
            self._category_cache[category] = names
        return names

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """
        调用 MCP 工具，返回文本 content。

        优先使用 AstrBot 已连接的 MCPClient；否则尝试 remote_url 直连。
        """
        # 1) AstrBot 已注册 MCP
        text = await self._call_via_astrbot(tool_name, arguments)
        if text is not None:
            return text

        # 2) 远程 URL 直连
        if self.remote_url:
            text = await self._call_via_remote(tool_name, arguments)
            if text is not None:
                return text

        logger.debug("HowToCook MCP 工具不可用: %s", tool_name)
        return None

    async def _call_via_astrbot(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """通过 AstrBot FunctionToolManager 中的 MCPClient 调用。"""
        if self.context is None:
            return None

        try:
            tool_mgr = self.context.get_llm_tool_manager()
        except Exception as e:
            logger.debug("无法获取 LLM 工具管理器: %s", e)
            return None

        if tool_mgr is None:
            return None

        client = self._resolve_mcp_client(tool_mgr)
        if client is None:
            return None

        session = getattr(client, "session", None)
        if session is None:
            logger.warning(
                "HowToCook MCP 客户端无有效 session（服务可能未连上）: server=%s",
                getattr(client, "name", self.server_name),
            )
            return None

        try:
            call_with_reconnect = getattr(client, "call_tool_with_reconnect", None)
            if callable(call_with_reconnect):
                result = await asyncio.wait_for(
                    call_with_reconnect(
                        tool_name=tool_name,
                        arguments=arguments,
                        read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                    ),
                    timeout=self.timeout_seconds + 2.0,
                )
            else:
                result = await asyncio.wait_for(
                    session.call_tool(
                        name=tool_name,
                        arguments=arguments,
                        read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                    ),
                    timeout=self.timeout_seconds + 2.0,
                )
            return self._tool_result_to_text(result)
        except asyncio.TimeoutError:
            logger.warning("HowToCook MCP 调用超时: %s", tool_name)
            return None
        except Exception as e:
            logger.warning("HowToCook MCP 调用失败 (%s): %s", tool_name, e)
            return None

    def _resolve_mcp_client(self, tool_mgr: Any) -> Any | None:
        """按配置名 / 候选名 / 工具名解析 MCPClient。"""
        clients_map = None
        try:
            clients_map = getattr(tool_mgr, "mcp_client_dict", None)
        except Exception:
            clients_map = None

        # MappingProxy / dict-like
        if clients_map:
            # 1) 明确配置的服务名
            if self.server_name:
                client = self._mapping_get(clients_map, self.server_name)
                if client is not None:
                    return client

            # 2) 常见默认名
            for name in DEFAULT_SERVER_CANDIDATES:
                if self.server_name and name == self.server_name:
                    continue
                client = self._mapping_get(clients_map, name)
                if client is not None:
                    return client

            # 3) 扫描已连接客户端，找拥有 howtocook 工具的那个
            try:
                for name, client in clients_map.items():
                    tools = getattr(client, "tools", None) or []
                    tool_names = {
                        getattr(t, "name", "") for t in tools if getattr(t, "name", "")
                    }
                    if TOOL_WHAT_TO_EAT in tool_names or TOOL_BY_CATEGORY in tool_names:
                        logger.debug("自动匹配 HowToCook MCP 服务: %s", name)
                        return client
                    # 宽松匹配：任意 howtocook 相关工具
                    if any("howtocook" in n.lower() for n in tool_names):
                        logger.debug("宽松匹配 HowToCook MCP 服务: %s", name)
                        return client
            except Exception as e:
                logger.debug("扫描 MCP 客户端失败: %s", e)

        # 兜底：从已注册 FunctionTool 里找 MCPTool
        try:
            func_list = getattr(tool_mgr, "func_list", None) or []
            for tool in func_list:
                t_name = getattr(tool, "name", "") or ""
                if t_name in {TOOL_WHAT_TO_EAT, TOOL_BY_CATEGORY} or "howtocook" in t_name.lower():
                    mcp_client = getattr(tool, "mcp_client", None)
                    if mcp_client is not None:
                        return mcp_client
        except Exception as e:
            logger.debug("从 func_list 解析 MCP 客户端失败: %s", e)

        return None

    @staticmethod
    def _mapping_get(mapping: Any, key: str) -> Any | None:
        try:
            if hasattr(mapping, "get"):
                return mapping.get(key)
            return mapping[key]
        except Exception:
            return None

    async def _call_via_remote(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """通过配置的远程 SSE/HTTP 端点直连 MCP（可选）。"""
        try:
            client = await self._ensure_remote_client()
            if client is None:
                return None
            session = getattr(client, "session", None)
            if session is None:
                return None

            result = await asyncio.wait_for(
                session.call_tool(
                    name=tool_name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                ),
                timeout=self.timeout_seconds + 2.0,
            )
            return self._tool_result_to_text(result)
        except asyncio.TimeoutError:
            logger.warning("HowToCook 远程 MCP 调用超时: %s", tool_name)
            return None
        except Exception as e:
            logger.warning("HowToCook 远程 MCP 调用失败 (%s): %s", tool_name, e)
            return None

    async def _ensure_remote_client(self) -> Any | None:
        """懒初始化远程 MCP 客户端。"""
        if self._remote_client is not None:
            return self._remote_client

        async with self._remote_lock:
            if self._remote_client is not None:
                return self._remote_client

            if not self.remote_url:
                return None

            try:
                from astrbot.core.agent.mcp_client import MCPClient
            except Exception as e:
                logger.warning("无法导入 AstrBot MCPClient，远程直连不可用: %s", e)
                return None

            cfg: dict[str, Any] = {
                "url": self.remote_url,
                "transport": self.remote_transport,
                "timeout": int(self.timeout_seconds),
            }
            if self.remote_headers:
                cfg["headers"] = self.remote_headers

            client = MCPClient()
            try:
                await asyncio.wait_for(
                    client.connect_to_server(cfg, "howtocook-remote"),
                    timeout=self.timeout_seconds + 5.0,
                )
                # 列出工具（可选，便于日志）
                try:
                    await asyncio.wait_for(
                        client.list_tools_and_save(),
                        timeout=self.timeout_seconds,
                    )
                    names = [getattr(t, "name", "") for t in (client.tools or [])]
                    logger.info("HowToCook 远程 MCP 已连接，工具: %s", names)
                except TimeoutError:
                    logger.warning(
                        "HowToCook 远程 MCP list_tools_and_save 超时 (%.1fs)，跳过工具列表缓存",
                        self.timeout_seconds,
                    )
                except Exception:
                    pass
                self._remote_client = client
                return client
            except Exception as e:
                logger.warning("连接 HowToCook 远程 MCP 失败: %s", e)
                try:
                    await client.cleanup()
                except Exception:
                    pass
                return None

    async def close(self) -> None:
        """释放远程客户端资源。"""
        client = self._remote_client
        self._remote_client = None
        if client is not None:
            try:
                await client.cleanup()
            except Exception as e:
                logger.debug("关闭远程 MCP 客户端时出错: %s", e)

    # ------------------------------------------------------------------
    # 结果解析
    # ------------------------------------------------------------------

    @staticmethod
    def _tool_result_to_text(result: Any) -> str | None:
        """把 MCP CallToolResult 转成文本。"""
        if result is None:
            return None

        # 已是字符串
        if isinstance(result, str):
            return result.strip() or None

        content = getattr(result, "content", None)
        if content is None and isinstance(result, dict):
            content = result.get("content")

        if not content:
            # 有的实现直接把 structured 放 result 上
            text = getattr(result, "structuredContent", None) or getattr(
                result, "structured_content", None
            )
            if text is not None:
                try:
                    return json.dumps(text, ensure_ascii=False)
                except Exception:
                    return str(text)
            return None

        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            # pydantic / mcp types TextContent
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        joined = "\n".join(parts).strip()
        return joined or None

    @staticmethod
    def _parse_json_payload(raw: str | None) -> Any | None:
        if not raw:
            return None
        text = raw.strip()
        if not text:
            return None

        # 直接 JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 从文本中截取首个 JSON 对象/数组
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start >= 0 and end > start:
                snippet = text[start : end + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    continue
        logger.debug("HowToCook MCP 返回非 JSON: %s", text[:200])
        return None

    @classmethod
    def _extract_dishes(cls, data: Any) -> list[dict[str, Any]]:
        """从 whatToEat / 其他结构中提取菜品 dict 列表。"""
        if data is None:
            return []

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and cls._dish_name(x)]

        if not isinstance(data, dict):
            return []

        for key in ("dishes", "recipes", "items", "data", "result"):
            value = data.get(key)
            if isinstance(value, list):
                dishes = [x for x in value if isinstance(x, dict) and cls._dish_name(x)]
                if dishes:
                    return dishes

        # 单道菜
        if cls._dish_name(data):
            return [data]

        return []

    @classmethod
    def _extract_names_from_list(cls, data: Any) -> list[str]:
        """从分类查询结果提取菜名列表。"""
        if data is None:
            return []

        items: list[Any]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("dishes", "recipes", "items", "data", "result"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            else:
                if cls._dish_name(data):
                    return [cls._dish_name(data)]  # type: ignore[list-item]
                return []
        else:
            return []

        names: list[str] = []
        for item in items:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                name = cls._dish_name(item)
                if name:
                    names.append(name)
        # 去重保序
        return list(dict.fromkeys(names))

    @staticmethod
    def _dish_name(dish: dict[str, Any]) -> str | None:
        for key in ("name", "title", "recipeName", "recipe_name", "strMeal"):
            value = dish.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @classmethod
    def _format_dish(cls, dish: dict[str, Any]) -> str | None:
        name = cls._dish_name(dish)
        if not name:
            return None
        category = ""
        for key in ("category", "categoryName", "type", "strCategory"):
            value = dish.get(key)
            if isinstance(value, str) and value.strip():
                category = value.strip()
                break
        return cls._format_display_name(name, category)

    @classmethod
    def _format_display_name(cls, name: str, category: str = "") -> str:
        name = cls._clean_recipe_name(name)
        category = (category or "").strip()
        # 去掉分类里多余空白
        category = re.sub(r"\s+", "", category)
        if not name:
            return ""
        if category and category not in name:
            return f"{name}（{category}）"
        return name
