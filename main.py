"""群间游戏 (Dynamics Games) — 一起完成一件事，统一群聊协作活动与娱乐引擎。"""
# ruff: noqa: E402

from __future__ import annotations

from .reload_bootstrap import ensure_current

ensure_current(
    __package__,
    __name__,
    __file__,
    checks=[
        ("core.coordinator", "ActivityCoordinator", "sync_outcome_records"),
        ("core.store", "ActivityStore", "save_completion"),
    ],
    version="0.1.1-rc.4",
)

import asyncio
import json
import threading
from importlib import reload
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.star import star_registry

from . import series_panels
from .core.commands import (
    build_arcade_text,
    build_help_text,
    handle_cancel_command,
    handle_category_shortcut,
    handle_cleanup_command,
    handle_complete_command,
    handle_create_command,
    handle_detail_command,
    handle_join_command,
    handle_leave_command,
    handle_list_command,
    handle_outcome_command,
    handle_stage_command,
    handle_submit_command,
)
from .core.coordinator import ActivityCoordinator
from .core.models import ENGINE_CATALOG
from .core.store import ActivityStore
from .core.web_console import GamesWebConsole
from .engines.blackjack21.main import Blackjack21Plugin as Blackjack21Engine
from .engines.rollpig.main import (
    COMMAND_ALIASES as ROLLPIG_ALIASES,
)
from .engines.rollpig.main import (
    COMMAND_NAME as ROLLPIG_CMD,
)
from .engines.rollpig.main import (
    WAKE_FREE_PATTERN as ROLLPIG_WAKE_FREE_PATTERN,
)
from .engines.rollpig.main import (
    RollPigPlugin as RollPigEngine,
)
from .engines.truth_game.main import TruthGamePlugin as TruthGameEngine
from .engines.what_to_eat.main import WhatToEatPlugin as WhatToEatEngine
from .runtime import LEGACY_PLUGIN_IDS, PLUGIN_ID, ConfigSection, EngineContext

MODULE_FLAG_MAP: dict[str, str] = {
    item["key"]: item["config_flag"] for item in ENGINE_CATALOG
}
MODULE_LABEL_MAP: dict[str, str] = {
    item["key"]: item["label"] for item in ENGINE_CATALOG
}


class DynamicsGamesPlugin(Star):
    """群间游戏：组织共读、游戏约局、接龙创作、小型挑战、共同制作作品与群聊娱乐引擎总成。"""

    VERSION = "0.1.1-rc.4"
    name = PLUGIN_ID
    author = "ysyhlly"
    plugin_id = f"ysyhlly/{PLUGIN_ID}"

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.raw_config = config if config is not None else {}
        self._config_lock = threading.RLock()
        self.engine_context = EngineContext(self)
        self.store: ActivityStore | None = None
        self.coordinator: ActivityCoordinator | None = None
        self.web_console: GamesWebConsole | None = None
        self.engines: dict[str, Any] = {}
        self._truth_outcome_task: asyncio.Task | None = None
        self._sections: dict[str, ConfigSection] = {}
        self._legacy_conflicts: list[str] = []
        self._ready = False
        self._closed = False

    def _section(self, key: str, defaults: dict | None = None) -> ConfigSection:
        sec = self._sections.get(key)
        if sec is None:
            sec = ConfigSection(
                self.raw_config, key, defaults=defaults, lock=self._config_lock
            )
            self._sections[key] = sec
        return sec

    def activities_enabled(self) -> bool:
        act = self.raw_config.get("activities")
        if isinstance(act, dict):
            return bool(act.get("enabled", True))
        return bool(self.raw_config.get("enabled", True))

    def module_enabled(self, key: str) -> bool:
        flag = MODULE_FLAG_MAP.get(key, f"enable_{key}")
        mods = self.raw_config.get("modules")
        if isinstance(mods, dict) and flag in mods:
            return bool(mods.get(flag, True))
        return bool(self.raw_config.get(flag, True))

    def set_module_enabled(self, key: str, enabled: bool) -> None:
        if key not in MODULE_FLAG_MAP:
            raise ValueError(f"未知模块: {key}")
        flag = MODULE_FLAG_MAP[key]
        with self._config_lock:
            mods = self.raw_config.get("modules")
            if not isinstance(mods, dict):
                mods = {}
                self.raw_config["modules"] = mods
            mods[flag] = bool(enabled)
            if hasattr(self.raw_config, "save_config"):
                self.raw_config.save_config()

    def module_states(self) -> dict[str, bool]:
        return {k: self.module_enabled(k) for k in MODULE_FLAG_MAP}

    def _detect_legacy_conflicts(self) -> list[str]:
        found: list[str] = []
        getter = getattr(self.context, "get_registered_star", None)
        for pid in sorted(LEGACY_PLUGIN_IDS):
            if callable(getter):
                existing = getter(pid)
                if existing is not None and getattr(existing, "activated", False):
                    found.append(pid)
                    continue
            for existing in star_registry:
                if (
                    getattr(existing, "name", None) == pid
                    and getattr(existing, "activated", False)
                    and getattr(existing, "star_cls", None) is not None
                ):
                    found.append(pid)
                    break
        return found

    async def initialize(self) -> None:
        conflicts = self._detect_legacy_conflicts()
        if conflicts:
            self._legacy_conflicts = conflicts
            self._ready = False
            logger.warning(
                "[DynamicsGames] 检测到已启用的旧插件 (%s)，已进入降级待机模式；停用旧插件后重载即可恢复。",
                ", ".join(conflicts),
            )
            return
        try:
            reload(series_panels).register_series_panels(self, PLUGIN_ID)
            db_path = StarTools.get_data_dir(PLUGIN_ID) / "dynamics_games.db"
            self.store = await asyncio.to_thread(ActivityStore, db_path)
            self.coordinator = ActivityCoordinator(
                self.store, self.context, self.raw_config
            )
            self.web_console = GamesWebConsole(self)
            self.web_console.register()

            engine_builders: tuple[tuple[str, Any], ...] = (
                ("truth_game", TruthGameEngine),
                ("blackjack21", Blackjack21Engine),
                ("rollpig", RollPigEngine),
                ("what_to_eat", WhatToEatEngine),
            )
            for key, cls in engine_builders:
                sec = self._section(key)
                inst = cls(self.engine_context, sec)
                if key == "truth_game":
                    inst.room_manager.outcome_callback = self.coordinator.enqueue_truth_outcome
                elif key == "blackjack21":
                    inst.outcome_callback = self.coordinator.record_minigame_outcome
                init_fn = getattr(inst, "initialize", None)
                if callable(init_fn):
                    res = init_fn()
                    if asyncio.iscoroutine(res):
                        await res
                self.engines[key] = inst

            self._truth_outcome_task = asyncio.create_task(
                self.coordinator.run_truth_outcomes(self.engines["truth_game"].room_manager)
            )
            self._ready = True
            logger.info(
                "[DynamicsGames] 群间游戏 v%s 初始化完成（协作活动库 + 4 大娱乐引擎已就绪）",
                self.VERSION,
            )
        except BaseException:
            await self.terminate()
            raise

    async def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        try:
            if self._truth_outcome_task is not None:
                self._truth_outcome_task.cancel()
                await asyncio.gather(self._truth_outcome_task, return_exceptions=True)
            if getattr(self, "_series_panels", None) is not None:
                self._series_panels.close()
            if self.web_console is not None:
                self.web_console.close()
            for key, inst in list(self.engines.items()):
                term_fn = getattr(inst, "terminate", None)
                if callable(term_fn):
                    try:
                        res = term_fn()
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        logger.exception("[DynamicsGames] 引擎 %s 关闭异常", key)
            self.engines.clear()
            if self.store is not None:
                await asyncio.to_thread(self.store.close)
        finally:
            self.engine_context.release()

    # ------------------------------------------------------------------
    # Public Series Contract
    # ------------------------------------------------------------------
    def get_dynamics_capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "provider_id": PLUGIN_ID,
            "display_name": "群间游戏",
            "enabled": self.activities_enabled(),
            "ready": self._ready and not self._closed and self.store is not None,
            "capabilities": {"games": 1, "activities": 1},
            "api_version": 1,
        }

    async def get_group_activity_context(self, umo: str) -> dict[str, Any]:
        if not self._ready or self.coordinator is None:
            return {"schema_version": 1, "umo": umo, "active_count": 0, "active_activities": []}
        return await self.coordinator.get_group_activity_context(umo)

    # ------------------------------------------------------------------
    # LLM Tools (Group Activity Awareness)
    # ------------------------------------------------------------------
    @filter.llm_tool(name="list_group_activities")
    async def list_group_activities(self, event: AstrMessageEvent) -> str:
        """查询当前群正在进行的协作活动（组织共读、游戏约局、接龙创作、小型挑战、共同制作作品）及近期完结成果。"""
        if not self._ready or self.coordinator is None:
            return json.dumps({"available": False}, ensure_ascii=False)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ctx = await self.coordinator.get_group_activity_context(umo)
        return json.dumps(ctx, ensure_ascii=False)

    @filter.llm_tool(name="get_group_activity_detail")
    async def get_group_activity_detail(
        self, event: AstrMessageEvent, activity_id: str = ""
    ) -> str:
        """查看当前群指定活动（或最新活动）的报名成员、当前阶段与最近打卡/创作进展。

        Args:
            activity_id(string): 可选。活动编号（如 G101），留空则查看当前群最新进行中的活动。
        """
        if not self._ready or self.coordinator is None:
            return "群间游戏尚未初始化。"
        return await handle_detail_command(self.coordinator, event, activity_id)

    # ------------------------------------------------------------------
    # Layer 1: Unified Collaborative Activity Commands (/活动 | /群间游戏 | /共创)
    # ------------------------------------------------------------------
    @filter.command_group("活动", alias={"群间游戏", "共创", "games"})
    def activity_group(self):
        """群间游戏：一起完成一件事（组织共读、游戏约局、接龙创作、小型挑战、共同制作作品）"""
        pass

    @activity_group.command("帮助", alias={"help", "说明"})
    async def act_help(self, event: AstrMessageEvent):
        """查看群间游戏协作指令说明"""
        yield event.plain_result(build_help_text(self.module_states()))

    @activity_group.command("娱乐", alias={"arcade", "modules", "模块", "小游戏"})
    async def act_arcade(self, event: AstrMessageEvent):
        """查看所有已整合娱乐与小游戏模块状态"""
        yield event.plain_result(build_arcade_text(self.module_states()))

    @activity_group.command("发起", alias={"create", "new", "创建", "新建"})
    async def act_create(self, event: AstrMessageEvent, args: str = ""):
        """发起一项群间协作活动"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("发起", "create", "new", "创建", "新建"), args)
        yield event.plain_result(await handle_create_command(self.coordinator, event, raw))

    @activity_group.command("列表", alias={"list", "全部"})
    async def act_list(self, event: AstrMessageEvent):
        """查看本群活动列表"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        yield event.plain_result(await handle_list_command(self.coordinator, event))

    @activity_group.command("详情", alias={"状态", "status", "info", "进度"})
    async def act_detail(self, event: AstrMessageEvent, activity_id: str = ""):
        """查看活动报名名单、阶段与进度详情"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        yield event.plain_result(
            await handle_detail_command(self.coordinator, event, activity_id)
        )

    @activity_group.command("报名", alias={"加入", "join", "signup", "上车"})
    async def act_join(self, event: AstrMessageEvent, args: str = ""):
        """报名参加群间活动"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("报名", "加入", "join", "signup", "上车"), args)
        yield event.plain_result(await handle_join_command(self.coordinator, event, raw))

    @activity_group.command("退出", alias={"leave", "跳车"})
    async def act_leave(self, event: AstrMessageEvent, args: str = ""):
        """退出尚未完结的活动"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("退出", "leave", "跳车"), args)
        yield event.plain_result(await handle_leave_command(self.coordinator, event, raw))

    @activity_group.command("开始", alias={"start", "发车"})
    async def act_start(self, event: AstrMessageEvent, args: str = ""):
        """结束招募进入正式第一阶段"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("开始", "start", "发车"), args)
        yield event.plain_result(
            await handle_stage_command(self.coordinator, event, raw, start_only=True)
        )

    @activity_group.command("阶段", alias={"推进", "下一阶段", "advance", "next", "stage"})
    async def act_stage(self, event: AstrMessageEvent, args: str = ""):
        """推进活动至下一阶段"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(
            event, ("阶段", "推进", "下一阶段", "advance", "next", "stage"), args
        )
        yield event.plain_result(
            await handle_stage_command(self.coordinator, event, raw, start_only=False)
        )

    @activity_group.command("打卡", alias={"checkin", "progress", "记录"})
    async def act_checkin(self, event: AstrMessageEvent, args: str = ""):
        """活动阶段打卡"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("打卡", "checkin", "progress", "记录"), args)
        yield event.plain_result(
            await handle_submit_command(self.coordinator, event, raw, kind="checkin")
        )

    @activity_group.command("提交", alias={"续写", "submit", "contribute", "下一棒"})
    async def act_submit(self, event: AstrMessageEvent, args: str = ""):
        """提交接龙段落、共读笔记或共创片段"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(
            event, ("提交", "续写", "submit", "contribute", "下一棒"), args
        )
        yield event.plain_result(
            await handle_submit_command(self.coordinator, event, raw, kind="submission")
        )

    @activity_group.command("完结", alias={"定稿", "结算", "finish", "complete", "done"})
    async def act_complete(self, event: AstrMessageEvent, args: str = ""):
        """完结定稿并同步群间记忆与群史记"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(
            event, ("完结", "定稿", "结算", "finish", "complete", "done"), args
        )
        yield event.plain_result(
            await handle_complete_command(self.coordinator, event, raw)
        )

    @activity_group.command("取消", alias={"流局", "cancel", "abort"})
    async def act_cancel(self, event: AstrMessageEvent, args: str = ""):
        """取消活动（流局不入群史记）"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("取消", "流局", "cancel", "abort"), args)
        yield event.plain_result(
            await handle_cancel_command(self.coordinator, event, raw)
        )

    @activity_group.command("清理", alias={"cleanup", "prune"})
    async def act_cleanup(self, event: AstrMessageEvent, args: str = ""):
        """群管理员手动清理超时滞留活动"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        raw = self._extract_tail(event, ("清理", "cleanup", "prune"), args)
        yield event.plain_result(
            await handle_cleanup_command(self.coordinator, event, raw)
        )

    @activity_group.command("成果", alias={"导出", "outcome", "result", "export", "查看成果"})
    async def act_outcome(self, event: AstrMessageEvent, activity_id: str = ""):
        """查看已完结活动的完整汇编成果"""
        if self.coordinator is None:
            yield event.plain_result("群间游戏尚未就绪。")
            return
        yield event.plain_result(
            await handle_outcome_command(self.coordinator, event, activity_id)
        )

    # Category quick shortcuts
    @filter.command("共读", alias={"组织共读"})
    async def shortcut_reading(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起或打卡组织共读"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("组织共读", "共读"), args)
        yield event.plain_result(
            await handle_category_shortcut(self.coordinator, event, "reading", raw)
        )

    @filter.command("约局", alias={"游戏约局", "开黑"})
    async def shortcut_matchmaking(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起或查看游戏约局"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("游戏约局", "约局", "开黑"), args)
        yield event.plain_result(
            await handle_category_shortcut(self.coordinator, event, "matchmaking", raw)
        )

    @filter.command("接龙", alias={"接龙创作", "故事接龙"})
    async def shortcut_relay(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起接龙创作或续写下一棒"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("接龙创作", "故事接龙", "接龙"), args)
        yield event.plain_result(
            await handle_category_shortcut(self.coordinator, event, "relay", raw)
        )

    @filter.command("挑战", alias={"小型挑战", "打卡挑战"})
    async def shortcut_challenge(self, event: AstrMessageEvent, args: str = ""):
        """快捷发起小型挑战或提交打卡"""
        if not self.activities_enabled() or self.coordinator is None:
            yield event.plain_result("群间协作活动功能当前未开启。")
            return
        raw = self._extract_tail(event, ("小型挑战", "打卡挑战", "挑战"), args)
        yield event.plain_result(
            await handle_category_shortcut(self.coordinator, event, "challenge", raw)
        )

    @staticmethod
    def _extract_tail(
        event: AstrMessageEvent, subcommands: tuple[str, ...], fallback: str = ""
    ) -> str:
        """Preserve multi-word arguments even when AstrBot splits whitespace after subcommand."""
        full = str(getattr(event, "message_str", "") or "").strip()
        if not full:
            return fallback
        for sub in subcommands:
            idx = full.find(sub)
            if idx != -1:
                tail = full[idx + len(sub) :].strip()
                return tail or fallback
        return fallback

    def _disabled_reply(self, event: AstrMessageEvent, key: str):
        label = MODULE_LABEL_MAP.get(key, key)
        return event.plain_result(f"「{label}」模块当前已在群间游戏控制台关闭。")

    # ------------------------------------------------------------------
    # Engine 1: 真心话 (truth_game)
    # ------------------------------------------------------------------
    @filter.command("真心话", alias=["truth_create", "开真心话"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_create(self, event: AstrMessageEvent):
        """创建真心话房间"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_room_create(event):
            yield r

    @filter.command("报名真心话", alias=["truth_join", "加入真心话"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_join(self, event: AstrMessageEvent):
        """加入真心话房间"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_room_join(event):
            yield r

    @filter.command("开始真心话", alias=["truth_start"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_start(self, event: AstrMessageEvent):
        """开始真心话对局"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_game_start(event):
            yield r

    @filter.command("下一题", alias=["truth_next"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_next(self, event: AstrMessageEvent):
        """推进真心话下一题"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_next_question(event):
            yield r

    @filter.command("跳过", alias=["truth_skip"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_skip(self, event: AstrMessageEvent):
        """跳过当前真心话题目"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_skip_question(event):
            yield r

    @filter.command("退出真心话", alias=["truth_leave"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_leave(self, event: AstrMessageEvent):
        """退出真心话房间"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_room_leave(event):
            yield r

    @filter.command("结束真心话", alias=["truth_end"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_end(self, event: AstrMessageEvent):
        """结束真心话并生成总结"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_room_end(event):
            yield r

    @filter.command("真心话状态", alias=["truth_status"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_status(self, event: AstrMessageEvent):
        """查看真心话房间状态"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_room_status(event):
            yield r

    @filter.command("真心话帮助", alias=["truth_help", "真心话规则"])
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_help(self, event: AstrMessageEvent):
        """查看真心话帮助"""
        if not self.module_enabled("truth_game"):
            yield self._disabled_reply(event, "truth_game")
            return
        async for r in self.engines["truth_game"].cmd_help(event):
            yield r

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def tg_on_group_message(self, event: AstrMessageEvent):
        if not self._ready or not self.module_enabled("truth_game"):
            return
        eng = self.engines.get("truth_game")
        if eng is not None:
            async for r in eng.on_group_message(event):
                yield r

    # ------------------------------------------------------------------
    # Engine 2: 21点 (blackjack21)
    # ------------------------------------------------------------------
    @filter.command("21点帮助", alias={"blackjack_help", "21点规则", "帮助21点"})
    async def bj_help(self, event: AstrMessageEvent):
        """查看21点规则"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_help(event):
            yield r

    @filter.command("创建21点", alias={"blackjack_start", "开21点", "开房间21点"})
    async def bj_open(self, event: AstrMessageEvent):
        """创建21点房间"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_open(event):
            yield r

    @filter.command("加入21点", alias={"blackjack_join", "加入21"})
    async def bj_join(self, event: AstrMessageEvent):
        """加入21点房间"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_join(event):
            yield r

    @filter.command("开始21点", alias={"blackjack_go", "启动21点游戏", "开打21点"})
    async def bj_deal(self, event: AstrMessageEvent):
        """开始21点对局"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_deal(event):
            yield r

    @filter.command("要牌", alias={"blackjack_hit", "21点要牌"})
    async def bj_hit(self, event: AstrMessageEvent):
        """21点要牌"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_hit(event):
            yield r

    @filter.command("停牌", alias={"blackjack_stand", "21点停牌", "不再要了"})
    async def bj_stand(self, event: AstrMessageEvent):
        """21点停牌"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_stand(event):
            yield r

    @filter.command("21点状态", alias={"blackjack_status", "对局状态"})
    async def bj_status(self, event: AstrMessageEvent):
        """查看21点状态"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_status(event):
            yield r

    @filter.command("结束21点", alias={"blackjack_end", "解散21点"})
    async def bj_end(self, event: AstrMessageEvent):
        """结束21点房间"""
        if not self.module_enabled("blackjack21"):
            yield self._disabled_reply(event, "blackjack21")
            return
        async for r in self.engines["blackjack21"].cmd_end(event):
            yield r

    # ------------------------------------------------------------------
    # Engine 3: 今日小猪 (rollpig)
    # ------------------------------------------------------------------
    @filter.command(ROLLPIG_CMD, alias=ROLLPIG_ALIASES)
    async def rp_roll_pig(self, event: AstrMessageEvent):
        """抽取今日小猪"""
        if not self.module_enabled("rollpig"):
            yield self._disabled_reply(event, "rollpig")
            return
        await self.engines["rollpig"].roll_pig(event)

    @filter.regex(ROLLPIG_WAKE_FREE_PATTERN)
    async def rp_roll_pig_wake_free(self, event: AstrMessageEvent):
        if not self._ready or not self.module_enabled("rollpig"):
            return
        await self.engines["rollpig"].roll_pig_wake_free(event)

    # ------------------------------------------------------------------
    # Engine 4: 是啊，吃什么 (what_to_eat)
    # ------------------------------------------------------------------
    @filter.command("口味", alias=["我的口味", "口味档案"])
    async def wte_taste_show(self, event: AstrMessageEvent):
        """查看自己的口味档案"""
        if not self.module_enabled("what_to_eat"):
            yield self._disabled_reply(event, "what_to_eat")
            return
        async for r in self.engines["what_to_eat"].cmd_taste_show(event):
            yield r

    @filter.command("喜欢吃", alias=["爱吃"])
    async def wte_taste_like(self, event: AstrMessageEvent):
        """记录喜欢的口味/菜"""
        if not self.module_enabled("what_to_eat"):
            yield self._disabled_reply(event, "what_to_eat")
            return
        async for r in self.engines["what_to_eat"].cmd_taste_like(event):
            yield r

    @filter.command("不吃", alias=["不喜欢", "忌口"])
    async def wte_taste_dislike(self, event: AstrMessageEvent):
        """记录忌口"""
        if not self.module_enabled("what_to_eat"):
            yield self._disabled_reply(event, "what_to_eat")
            return
        async for r in self.engines["what_to_eat"].cmd_taste_dislike(event):
            yield r

    @filter.command("过敏")
    async def wte_taste_allergy(self, event: AstrMessageEvent):
        """记录过敏原"""
        if not self.module_enabled("what_to_eat"):
            yield self._disabled_reply(event, "what_to_eat")
            return
        async for r in self.engines["what_to_eat"].cmd_taste_allergy(event):
            yield r

    @filter.command("清空口味", alias=["重置口味"])
    async def wte_taste_clear(self, event: AstrMessageEvent):
        """清空口味档案"""
        if not self.module_enabled("what_to_eat"):
            yield self._disabled_reply(event, "what_to_eat")
            return
        async for r in self.engines["what_to_eat"].cmd_taste_clear(event):
            yield r

    @filter.event_message_type(EventMessageType.ALL)
    async def wte_on_message(self, event: AstrMessageEvent, *args, **kwargs):
        if not self._ready or not self.module_enabled("what_to_eat"):
            return
        eng = self.engines.get("what_to_eat")
        if eng is not None:
            async for r in eng.on_what_to_eat(event, *args, **kwargs):
                yield r
