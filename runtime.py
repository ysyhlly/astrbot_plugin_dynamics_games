"""Shared configuration, host ownership and lifecycle for integrated game engines."""

from __future__ import annotations

import logging
import threading
from typing import Any

try:
    from astrbot.api.star import Star
except ImportError:  # pragma: no cover - standalone test fallback
    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any = None) -> None:
            self.context = context

        async def html_render(self, *args: Any, **kwargs: Any) -> str:
            return ""

        async def text_to_image(self, *args: Any, **kwargs: Any) -> str:
            return ""

PLUGIN_ID = "astrbot_plugin_dynamics_games"

LEGACY_PLUGINS: tuple[tuple[str, str], ...] = (
    ("astrbot_plugin_truth_game", "真心话"),
    ("astrbot_plugin_blackjack21", "21点"),
    ("astrbot_plugin_rollpig", "今日小猪"),
    ("astrbot_plugin_what_to_eat", "是啊，吃什么"),
)

LEGACY_PLUGIN_IDS: frozenset[str] = frozenset(item[0] for item in LEGACY_PLUGINS)


class ConfigSection(dict):
    """Expose one engine's settings section without clobbering sibling sections on save."""

    def __init__(
        self,
        parent: dict,
        key: str,
        defaults: dict | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        merged = dict(defaults or {})
        raw = parent.get(key)
        if isinstance(raw, dict):
            merged.update(raw)
        super().__init__(merged)
        self.parent = parent
        self.key = key
        self.lock = lock or threading.RLock()
        self._revision = 0

    @property
    def _save_revision(self) -> int:
        return getattr(self.parent, "_save_revision", self._revision)

    def refresh_from_parent(self, defaults: dict | None = None) -> None:
        with self.lock:
            merged = dict(defaults or {})
            raw = self.parent.get(self.key)
            if isinstance(raw, dict):
                merged.update(raw)
            self.clear()
            self.update(merged)

    def save_config(self, updates: dict | None = None) -> None:
        with self.lock:
            before = dict(self)
            if updates is not None:
                self.update(updates)
            previous = self.parent.get(self.key)
            self.parent[self.key] = dict(self)
            try:
                if hasattr(self.parent, "save_config"):
                    self.parent.save_config()
                self._revision += 1
            except BaseException:
                self.parent[self.key] = previous
                self.clear()
                self.update(before)
                raise


class EngineContext:
    """Attribute API routes and manually registered tools to the single owner Star."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.host = owner.context
        self.routes: list[tuple] = []
        self.tools: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.host, name)

    def register_web_api(
        self, route: str, handler: Any, methods: list[str], description: str
    ) -> None:
        self.host.register_web_api(route, handler, methods, description)
        self.routes.append((route, handler, methods, description))

    def add_llm_tools(self, *tools: Any) -> None:
        self.host.add_llm_tools(*tools)
        for tool in tools:
            tool.handler_module_path = self.owner.__class__.__module__
            self.tools.append(tool)

    def release(self) -> None:
        registered = getattr(self.host, "registered_web_apis", None)
        if isinstance(registered, list):
            registered[:] = [r for r in registered if r not in self.routes]
        manager = getattr(
            getattr(self.host, "provider_manager", None), "llm_tools", None
        )
        if manager is not None and hasattr(manager, "func_list"):
            manager.func_list[:] = [
                t
                for t in manager.func_list
                if all(t is not owned for owned in self.tools)
            ]
        self.routes.clear()
        self.tools.clear()


class Component:
    """Internal engine base class sharing owner Star capabilities without a second registration."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.owner = getattr(context, "owner", None)
        self.name = getattr(self.owner, "name", PLUGIN_ID)
        self.author = getattr(self.owner, "author", "ysyhlly")
        self.plugin_id = getattr(
            self.owner, "plugin_id", f"ysyhlly/{PLUGIN_ID}"
        )
        self.logger = getattr(self.owner, "logger", None) or logging.getLogger(
            f"astrbot.plugin.{PLUGIN_ID}"
        )

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        if self.owner is not None:
            return await self.owner.get_kv_data(key, default)
        return default

    async def put_kv_data(self, key: str, value: Any) -> None:
        if self.owner is not None:
            await self.owner.put_kv_data(key, value)

    html_render = Star.html_render
    text_to_image = Star.text_to_image

    async def initialize(self) -> None:
        pass

    async def terminate(self) -> None:
        pass
