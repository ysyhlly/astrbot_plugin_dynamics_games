"""食物数据管理模块。"""

from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api import AstrBotConfig

    from .food_api import FoodApiClient
    from .taste_memory import TasteMemoryStore


# 默认内置菜肴（大学生/打工人常见选择，约 120 道）
DEFAULT_BUILTIN_FOODS: list[str] = [
    # —— 校园/平价快餐 ——
    "黄焖鸡米饭",
    "麻辣烫",
    "兰州拉面",
    "沙县小吃",
    "重庆小面",
    "螺蛳粉",
    "米线",
    "炒饭",
    "炒面",
    "盖浇饭",
    "水饺",
    "馄饨",
    "煎饼果子",
    "肉夹馍",
    "烤冷面",
    "关东煮",
    "冒菜",
    "香锅",
    "炸鸡",
    "汉堡",
    "披萨",
    "寿司",
    "便当",
    "凉皮",
    "凉面",
    "热干面",
    "酸辣粉",
    "炸酱面",
    "牛肉面",
    "叉烧饭",
    "烧腊饭",
    "煲仔饭",
    "石锅拌饭",
    "部队锅",
    "烤肉饭",
    "猪脚饭",
    "卤肉饭",
    "烤鸭饭",
    "口水鸡",
    "酸菜鱼",
    "水煮鱼",
    "毛血旺",
    "干锅",
    "烤鱼",
    "烧烤",
    "奶茶+面包",
    "便利店",
    "泡面",
    "食堂自选",
    "轻食沙拉",
    # —— 面食/粉类 ——
    "刀削面",
    "油泼面",
    "担担面",
    "阳春面",
    "海鲜面",
    "番茄鸡蛋面",
    "豚骨拉面",
    "乌冬面",
    "意面",
    "炒河粉",
    "肠粉",
    "过桥米线",
    "桂林米粉",
    "老友粉",
    "螺蛳粉+炸蛋",
    "火鸡面",
    "拌面",
    "葱油拌面",
    "烩面",
    "羊肉泡馍",
    # —— 盖饭/快餐升级 ——
    "鱼香肉丝盖饭",
    "宫保鸡丁盖饭",
    "番茄炒蛋盖饭",
    "回锅肉盖饭",
    "土豆牛肉盖饭",
    "咖喱饭",
    "黑椒牛柳饭",
    "照烧鸡排饭",
    "鳗鱼饭",
    "肥牛饭",
    "鸡公煲",
    "木桶饭",
    "铁板烧",
    "牛排饭",
    "鸡腿饭",
    "蛋包饭",
    "沙拉碗",
    "越南粉",
    "泰式炒河粉",
    "韩式拌饭",
    # —— 火锅/冒菜/汤锅 ——
    "火锅",
    "串串香",
    "麻辣香锅",
    "番茄火锅",
    "椰子鸡",
    "牛肉火锅",
    "鱼头泡饭",
    "酸汤肥牛",
    "水煮肉片",
    "肥肠粉",
    # —— 小吃/路边摊 ——
    "炸串",
    "臭豆腐",
    "烤红薯",
    "烤面筋",
    "铁板豆腐",
    "鸡蛋灌饼",
    "手抓饼",
    "生煎包",
    "小笼包",
    "锅贴",
    "烧麦",
    "葱油饼",
    "韭菜盒子",
    "豆皮",
    "烤包子",
    "炸鸡排",
    "鸡米花",
    "薯条+可乐",
    "章鱼小丸子",
    "鱼豆腐串",
    # —— 早餐/轻食 ——
    "包子豆浆",
    "油条豆腐脑",
    "胡辣汤+油饼",
    "肠粉+皮蛋瘦肉粥",
    "三明治",
    "饭团",
    "紫菜包饭",
    "燕麦碗",
    "酸奶水果杯",
    "全麦吐司",
    # —— 地方菜/家常 ——
    "红烧肉",
    "糖醋里脊",
    "可乐鸡翅",
    "麻婆豆腐",
    "鱼香茄子",
    "地三鲜",
    "蒜蓉西兰花",
    "青椒肉丝",
    "土豆炖牛腩",
    "排骨汤泡饭",
    "扬州炒饭",
    "虾仁滑蛋饭",
    "蒜香排骨",
    "啤酒鸭",
    "椒盐虾",
    "干煸豆角",
    "酸辣土豆丝",
    "西红柿鸡蛋汤+饼",
    # —— 夜宵/饮品搭子 ——
    "小龙虾",
    "炸鸡啤酒",
    "烤生蚝",
    "海鲜粥",
    "鸭血粉丝汤",
    "麻辣拌",
    "凉菜拼盘",
    "炸物拼盘",
    "芝士焗饭",
    "奶油蘑菇意面",
    "日式定食",
    "寿司拼盘",
    "刺身",
    "韩式炸鸡",
    "部队火锅",
    "芝士排骨",
    "泰式冬阴功",
    "咖喱牛腩",
    "新加坡炒面",
    "马来拉茶+椰浆饭",
    # —— 懒人/应急 ——
    "外卖盲盒",
    "速冻水饺",
    "自热火锅",
    "自热米饭",
    "罐头+米饭",
    "面包牛奶",
    "水果餐",
    "减脂鸡胸餐",
    "牛肉汉堡套餐",
    "肯德基随心选",
    "麦当劳随心选",
    "必胜客小食拼盘",
]


class FoodDataManager:
    """管理食物数据，包括内置列表、用户自定义列表与可选 MCP 源。"""

    def __init__(
        self,
        config: AstrBotConfig,
        api_client: FoodApiClient | None = None,
        api_probability: float = 0.0,
        taste_memory: TasteMemoryStore | None = None,
    ) -> None:
        """
        初始化食物数据管理器。

        Args:
            config: 插件配置 (AstrBotConfig)
            api_client: 可选的 HowToCook MCP 客户端
            api_probability: 推荐时使用 MCP 的概率 (0.0-1.0)
            taste_memory: 可选的用户口味记忆
        """
        self.config = config
        self.api_client = api_client
        self.api_probability = max(0.0, min(1.0, float(api_probability)))
        self.taste_memory = taste_memory

        # 验证并清理内置食物列表；配置为空时使用默认扩展列表
        builtin_foods_raw = config.get("builtin_foods", None)
        if not builtin_foods_raw:
            self.builtin_foods = list(DEFAULT_BUILTIN_FOODS)
        else:
            self.builtin_foods = self._sanitize_food_list(builtin_foods_raw, "builtin_foods")
            if not self.builtin_foods:
                self.builtin_foods = list(DEFAULT_BUILTIN_FOODS)

        # 验证并清理自定义食物列表
        custom_foods_raw = config.get("custom_foods", [])
        self.custom_foods = self._sanitize_food_list(custom_foods_raw, "custom_foods")

        # 缓存合并后的列表
        self._cached_foods: list[str] | None = None

        logger.info(
            f"食物数据管理器初始化完成: builtin={len(self.builtin_foods)}, "
            f"custom={len(self.custom_foods)}, api_prob={self.api_probability}, "
            f"taste={'on' if taste_memory and taste_memory.enabled else 'off'}"
        )

    def _sanitize_food_list(self, raw_value: Any, field_name: str) -> list[str]:
        """
        清理配置文件中的食物列表。

        Args:
            raw_value: 原始配置值
            field_name: 用于日志记录的字段名

        Returns:
            清理后的字符串列表
        """
        if raw_value is None:
            return []

        if isinstance(raw_value, str):
            # 处理配置可能是字符串而非列表的情况
            logger.warning(f"{field_name} 是字符串，正在转换为列表")
            return [raw_value] if raw_value.strip() else []

        if not isinstance(raw_value, list):
            logger.warning(f"{field_name} 类型异常 {type(raw_value).__name__}，使用空列表")
            return []

        # 过滤掉非字符串项和空字符串
        result = []
        for item in raw_value:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
            else:
                logger.warning(f"跳过 {field_name} 中的无效项: {item!r}")

        return result

    def clear_cache(self) -> None:
        """清除缓存的食物列表。配置热重载时调用。"""
        self._cached_foods = None
        logger.debug("食物缓存已清除")

    def get_all_foods(self) -> list[str]:
        """
        获取所有可用的本地食物项。

        Returns:
            内置和自定义食物合并后的列表
        """
        # 如果缓存可用，直接返回
        if self._cached_foods is not None:
            return self._cached_foods

        # 使用 dict.fromkeys() 去重同时保持顺序（Python 3.7+）
        all_foods = self.builtin_foods + self.custom_foods
        self._cached_foods = list(dict.fromkeys(all_foods))

        return self._cached_foods

    def get_random_food(self, user_id: str | None = None) -> str | None:
        """
        获取随机本地食物项（可按用户口味加权）。

        Returns:
            食物名称，如果没有可用食物则返回 None
        """
        foods = self.get_all_foods()
        if not foods:
            return None
        if self.taste_memory and self.taste_memory.enabled and user_id:
            picked = self.taste_memory.pick_food(user_id, foods)
            if picked:
                return picked
        return random.choice(foods)

    @staticmethod
    def _normalize_food_key(food: str) -> str:
        text = re.sub(r"[（(].*?[）)]", "", food or "").strip()
        # 与 MCP 侧一致：去掉「的做法」等后缀，便于去重
        for suffix in ("的做法", "做法", "的详细做法", "食谱", "菜谱"):
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = text[: -len(suffix)].strip()
                break
        return text.lower()

    def _accept_external_food(self, user_id: str | None, food: str | None) -> str | None:
        """外部源菜名是否符合用户忌口；不符合则丢弃。"""
        if not food:
            return None
        if (
            self.taste_memory
            and self.taste_memory.enabled
            and user_id
            and self.taste_memory.get_profile(user_id).is_forbidden(food)
        ):
            logger.debug("外部推荐触碰忌口，已丢弃: %s", food)
            return None
        # 最近刚推过的直接换掉，避免 MCP 反复推同一道（如啤酒鸭）
        if self.taste_memory and self.taste_memory.enabled and user_id:
            recent_raw = self.taste_memory.get_profile(user_id).recent_foods(
                self.taste_memory.avoid_recent_count
            )
            recent_keys = {self._normalize_food_key(r) for r in recent_raw if r}
            key = self._normalize_food_key(food)
            if key and key in recent_keys:
                logger.debug("外部推荐与最近重复，换一道: %s", food)
                return None
        return food

    async def get_recommend_food(self, user_id: str | None = None) -> str | None:
        """
        获取推荐食物：按概率尝试 MCP，失败则回退本地列表；可按用户口味筛选。

        Returns:
            食物名称，若本地与 MCP 都无可用则返回 None
        """
        # 按概率尝试 HowToCook MCP（或其它外部源）
        if (
            self.api_client
            and self.api_client.enabled
            and self.api_probability > 0
            and random.random() < self.api_probability
        ):
            # MCP 可能连续抽到忌口/重复菜，多试几次再回退本地
            for _ in range(5):
                api_food = await self.api_client.get_random_food()
                accepted = self._accept_external_food(user_id, api_food)
                if accepted:
                    logger.debug(f"使用MCP推荐: {accepted}")
                    return accepted
            logger.debug("MCP未返回合适菜肴，回退本地列表")

        return self.get_random_food(user_id=user_id)

    def has_foods(self) -> bool:
        """检查是否有可用的本地食物。"""
        # 使用缓存列表避免重复计算
        return len(self.get_all_foods()) > 0
