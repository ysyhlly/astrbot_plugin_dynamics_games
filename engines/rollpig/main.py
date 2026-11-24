from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime
from pathlib import Path

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools
from astrbot.core import AstrBotConfig

from ...runtime import PLUGIN_ID, Component
from ...series_panels import resolve_series_timezone
from .rollpig.adapter import AtTargetResolver, PigPresenter
from .rollpig.config import PluginSettings
from .rollpig.messages import CATALOG_UNAVAILABLE
from .rollpig.models import Pig
from .rollpig.renderer import CanvasSpec, PigCardRenderer
from .rollpig.repository import PigRepository
from .rollpig.service import RollPigService
from .rollpig.storage import BoardStore
from .rollpig.text import FontLibrary, TextMetrics

PLUGIN_NAME = "astrbot_plugin_rollpig"
DATA_FILE_NAME = "rollpig_today.json"


def resolve_rollpig_data_dir(
    *,
    new_dir: Path | None = None,
    legacy_dir: Path | None = None,
    logger_obj=None,
) -> Path:
    """Resolve RollPig data dir under Dynamics Games plugin data, migrating legacy data once."""
    target_dir = (
        Path(new_dir)
        if new_dir is not None
        else Path(StarTools.get_data_dir(PLUGIN_ID)) / "rollpig"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / DATA_FILE_NAME
    if not target_file.exists():
        old_dir = (
            Path(legacy_dir)
            if legacy_dir is not None
            else Path(StarTools.get_data_dir(PLUGIN_NAME))
        )
        old_file = old_dir / DATA_FILE_NAME
        try:
            if (
                old_file.is_file()
                and old_file.resolve() != target_file.resolve()
            ):
                tmp = target_file.with_suffix(".json.migrate.tmp")
                shutil.copy2(old_file, tmp)
                tmp.replace(target_file)
                if logger_obj is not None:
                    logger_obj.info(
                        "[RollPig] 已自动迁移旧版今日小猪数据: %s -> %s",
                        old_file,
                        target_file,
                    )
        except Exception as exc:  # noqa: BLE001
            if logger_obj is not None:
                logger_obj.warning("[RollPig] 迁移旧版数据失败（已保留原文件）: %s", exc)
    return target_dir

# 触发词是唯一事实来源：既作为指令名/别名，也作为「免唤醒词」模式的首尾匹配词
COMMAND_NAME = "今日小猪"
TRIGGER_WORDS = (
    "今日小猪",
    "抽小猪",
    "我的小猪",
    "rollpig",
    "pig",
    "猪咪",
    "猪",
    "🐖",
    "🐷",
    "㊗️",
)
COMMAND_ALIASES = {word for word in TRIGGER_WORDS if word != COMMAND_NAME}
# 免唤醒匹配规则：消息以触发词开头或结尾即可命中（不区分大小写）
WAKE_FREE_PATTERN = "(?i)(?:^(?:{0})|(?:{0})$)".format(
    "|".join(map(re.escape, TRIGGER_WORDS))
)


class RollPigPlugin(Component):
    """每日抽取专属小猪，并渲染成卡片发送。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.settings = PluginSettings.from_mapping(
            self.config,
            context.get_config().get("admins_id", []),
        )

        resource_dir = Path(__file__).parent / "resource"
        font_dir = resource_dir / "font"
        data_dir = resolve_rollpig_data_dir(logger_obj=self.logger)
        font_dir.mkdir(parents=True, exist_ok=True)  # 兼容旧版：允许用户自行补充字体

        self.repository = PigRepository(
            catalog_path=resource_dir / "pig.json",
            image_dir=resource_dir / "image",
            logger=self.logger,
        )
        self.fonts = FontLibrary(font_dir, self.logger)
        self.renderer = PigCardRenderer(
            repository=self.repository,
            fonts=self.fonts,
            metrics=TextMetrics(),
            spec=CanvasSpec(),
            logger=self.logger,
        )
        self.service = RollPigService(
            BoardStore(data_dir / DATA_FILE_NAME, self.logger),
            self.repository,
            clock=lambda: datetime.now(resolve_series_timezone(context)).date(),
        )
        self.presenter = PigPresenter(self.settings, self.logger)
        self.targeting = AtTargetResolver(self.settings)

    async def roll_pig(self, event: AstrMessageEvent):
        """抽取今日小猪"""
        await self._draw_and_send(event)

    async def roll_pig_wake_free(self, event: AstrMessageEvent):
        """免唤醒词触发（需开启配置项 wake_free_trigger）"""
        if not self.settings.wake_free_trigger:
            return
        text = event.get_message_str().strip()
        if (
            (event.is_at_or_wake_command or event.is_private_chat())
            and text in TRIGGER_WORDS
        ):
            # 该消息已由原有指令路径命中，交回 roll_pig，避免重复回复
            return
        await self._draw_and_send(event)

    async def _draw_and_send(self, event: AstrMessageEvent) -> None:
        decision = self.targeting.resolve(event)
        if decision.rejected:
            await event.send(event.plain_result(decision.message))
            return

        pig = await asyncio.to_thread(self.service.draw, decision.user_id)
        if pig is None:
            await event.send(event.plain_result(CATALOG_UNAVAILABLE))
            return

        await self._send_card(event, pig, decision.user_id)

    async def _send_card(self, event: AstrMessageEvent, pig: Pig, user_id: str) -> None:
        """渲染并发送卡片；渲染或发送失败时降级为原始图片 + 文本。"""
        # 图片合成是 CPU 密集操作，放到线程池避免阻塞事件循环
        image_path = await asyncio.to_thread(self.renderer.render, pig)
        if image_path is not None and image_path.exists():
            try:
                mention = self.presenter.should_mention(event)
                await event.send(
                    event.chain_result(
                        self.presenter.card_chain(user_id, mention=mention)
                    )
                )
                await event.send(event.image_result(str(image_path.absolute())))
                return
            except Exception as exc:
                self.logger.error("发送合成图片失败：%s", exc)
            finally:
                self._remove_temp_file(image_path)

        await event.send(
            event.chain_result(
                self.presenter.fallback_chain(pig, self.repository.find_image(pig.id))
            )
        )

    def _remove_temp_file(self, path: Path) -> None:
        """清理渲染产生的临时文件。"""
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            self.logger.warning("清理临时图片失败：%s", exc)

    async def terminate(self):
        """插件卸载清理"""
        self.logger.info("今日小猪插件已卸载")
