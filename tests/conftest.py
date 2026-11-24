"""Pytest configuration and lightweight AstrBot SDK doubles for host-free unit tests."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TEST_DIR.parent
WORKSPACE_ROOT = PLUGIN_DIR.parent

_sys_paths = [
    path for path in sys.path if path not in {str(PLUGIN_DIR), str(WORKSPACE_ROOT)}
]
sys.path[:] = [str(WORKSPACE_ROOT), str(PLUGIN_DIR), *_sys_paths]


def _install_astrbot_test_double() -> None:
    """Install minimal SDK doubles only when real AstrBot is unavailable."""
    spec = importlib.util.find_spec("astrbot")
    api_spec = importlib.util.find_spec("astrbot.api") if spec is not None else None
    if spec is not None and api_spec is not None:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    star_mod = types.ModuleType("astrbot.api.star")
    web_mod = types.ModuleType("astrbot.api.web")
    comp_mod = types.ModuleType("astrbot.api.message_components")

    core_mod = types.ModuleType("astrbot.core")
    core_star_pkg = types.ModuleType("astrbot.core.star")
    core_star_mod = types.ModuleType("astrbot.core.star.star")
    core_msg_pkg = types.ModuleType("astrbot.core.message")
    core_msg_mod = types.ModuleType("astrbot.core.message.message_event_result")
    core_plat_pkg = types.ModuleType("astrbot.core.platform")
    core_sources_pkg = types.ModuleType("astrbot.core.platform.sources")
    core_aiocq_pkg = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    core_aiocq_mod = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    dash_pkg = types.ModuleType("astrbot.dashboard")
    dash_api_pkg = types.ModuleType("astrbot.dashboard.api")
    dash_auth_mod = types.ModuleType("astrbot.dashboard.api.auth")

    class AstrBotConfig(dict):
        def save_config(self) -> None:
            return None

    class Plain:
        def __init__(self, text: str = "") -> None:
            self.text = str(text)

    class At:
        def __init__(self, qq: str | int = "", name: str = "") -> None:
            self.qq = str(qq)
            self.name = str(name)

    class AtAll:
        pass

    class Reply:
        def __init__(self, id: str | int = "") -> None:
            self.id = str(id)

    class Image:
        def __init__(self, file: str = "", url: str = "", base64: str = "") -> None:
            self.file = file
            self.url = url
            self.base64 = base64

        @classmethod
        def fromURL(cls, url: str) -> Image:
            return cls(url=str(url))

        @classmethod
        def fromBase64(cls, b64: str) -> Image:
            return cls(base64=str(b64))

        @classmethod
        def fromFileSystem(cls, path: str) -> Image:
            return cls(file=str(path))

    class MessageChain:
        def __init__(self, chain=None) -> None:
            self.chain = list(chain or [])

        def message(self, text: str) -> MessageChain:
            self.chain.append(Plain(str(text)))
            return self

        def file_image(self, path: str) -> MessageChain:
            self.chain.append(Image.fromFileSystem(str(path)))
            return self

        def get_plain_text(self, *_args) -> str:
            return "".join(
                item if isinstance(item, str) else getattr(item, "text", "")
                for item in self.chain
            )

        def __repr__(self) -> str:
            parts = []
            for item in self.chain:
                if isinstance(item, Plain):
                    parts.append(f"Plain({item.text!r})")
                elif isinstance(item, Image):
                    parts.append(f"Image(file={item.file!r}, url={item.url!r})")
                else:
                    parts.append(repr(item))
            return f"MessageChain([{', '.join(parts)}])"

        __str__ = __repr__

    class EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"

    class PermissionType:
        ADMIN = "ADMIN"
        MEMBER = "MEMBER"

    def _decorator(*_args, **_kwargs):
        return lambda target: target

    def _command_group(*_args, **_kwargs):
        def _wrap(target):
            target.command = _decorator
            target.group = _command_group
            return target

        return _wrap

    filter_mod.EventMessageType = EventMessageType
    filter_mod.PermissionType = PermissionType
    filter_mod.event_message_type = _decorator
    filter_mod.command = _decorator
    filter_mod.command_group = _command_group
    filter_mod.regex = _decorator
    filter_mod.permission_type = _decorator
    filter_mod.llm_tool = _decorator
    filter_mod.on_llm_request = _decorator
    filter_mod.on_llm_response = _decorator
    filter_mod.on_decorating_result = _decorator
    filter_mod.after_message_sent = _decorator

    class Star:
        def __init__(self, context=None) -> None:
            self.context = context
            self._kv: dict = {}

        async def put_kv_data(self, key: str, value) -> None:
            self._kv[key] = value

        async def get_kv_data(self, key: str, default=None):
            return self._kv.get(key, default)

        async def delete_kv_data(self, key: str) -> None:
            self._kv.pop(key, None)

        async def html_render(self, *_args, **_kwargs) -> str:
            return ""

        async def text_to_image(self, *_args, **_kwargs) -> str:
            return ""

    class Context:
        pass

    _test_data_base = Path(tempfile.gettempdir()) / "astrbot_games_pytest_data"

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str = "astrbot_plugin_dynamics_games") -> Path:
            p = _test_data_base / str(plugin_name)
            p.mkdir(parents=True, exist_ok=True)
            return p

    class _SP:
        async def get_async(self, *, scope="", scope_id="", key="", default=None):
            _ = (scope, scope_id, key)
            return default

    class Request:
        args: dict = {}
        query: dict = {}
        username: str = "pytest"

        async def json(self, default=None):
            return default or {}

    def json_response(data=None, *, status_code: int = 200, headers=None):
        payload = dict(data) if isinstance(data, dict) else {"data": data}
        if status_code != 200:
            payload["status_code"] = status_code
        if headers:
            payload["headers"] = headers
        return payload

    def error_response(message, *, status_code: int = 400, data=None, headers=None):
        return json_response(
            {
                "status": "error",
                "message": message,
                "data": data,
                "ok": False,
                "error": message,
            },
            status_code=status_code,
            headers=headers,
        )

    class AstrMessageEvent:
        pass

    class MessageEventResult:
        def __init__(self, chain=None) -> None:
            self.chain = list(chain or [])

    class AiocqhttpMessageEvent(AstrMessageEvent):
        pass

    async def require_dashboard_user(_raw):
        return "admin"

    api.logger = logging.getLogger("astrbot-test")
    api.AstrBotConfig = AstrBotConfig
    api.sp = _SP()

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.EventMessageType = EventMessageType
    event_mod.MessageChain = MessageChain
    event_mod.filter = filter_mod

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = _decorator

    web_mod.json_response = json_response
    web_mod.error_response = error_response
    web_mod.request = Request()

    comp_mod.Plain = Plain
    comp_mod.At = At
    comp_mod.AtAll = AtAll
    comp_mod.Reply = Reply
    comp_mod.Image = Image

    core_mod.AstrBotConfig = AstrBotConfig
    core_star_mod.star_registry = []
    core_msg_mod.MessageEventResult = MessageEventResult
    core_aiocq_mod.AiocqhttpMessageEvent = AiocqhttpMessageEvent
    dash_auth_mod.require_dashboard_user = require_dashboard_user

    astrbot.api = api
    astrbot.core = core_mod
    astrbot.dashboard = dash_pkg
    api.event = event_mod
    api.star = star_mod
    api.web = web_mod
    api.message_components = comp_mod
    core_mod.star = core_star_pkg
    core_star_pkg.star = core_star_mod
    core_mod.message = core_msg_pkg
    core_msg_pkg.message_event_result = core_msg_mod
    core_mod.platform = core_plat_pkg
    core_plat_pkg.sources = core_sources_pkg
    core_sources_pkg.aiocqhttp = core_aiocq_pkg
    core_aiocq_pkg.aiocqhttp_message_event = core_aiocq_mod
    dash_pkg.api = dash_api_pkg
    dash_api_pkg.auth = dash_auth_mod

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.web": web_mod,
            "astrbot.api.message_components": comp_mod,
            "astrbot.core": core_mod,
            "astrbot.core.star": core_star_pkg,
            "astrbot.core.star.star": core_star_mod,
            "astrbot.core.message": core_msg_pkg,
            "astrbot.core.message.message_event_result": core_msg_mod,
            "astrbot.core.platform": core_plat_pkg,
            "astrbot.core.platform.sources": core_sources_pkg,
            "astrbot.core.platform.sources.aiocqhttp": core_aiocq_pkg,
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": core_aiocq_mod,
            "astrbot.dashboard": dash_pkg,
            "astrbot.dashboard.api": dash_api_pkg,
            "astrbot.dashboard.api.auth": dash_auth_mod,
        }
    )

    # Allow child Python subprocesses spawned by tests to inherit the same SDK double
    # when running host-free outside an AstrBot checkout.
    stub_dir = Path(tempfile.gettempdir()) / "astrbot_games_pytest_stub"
    stub_dir.mkdir(parents=True, exist_ok=True)
    site_file = stub_dir / "sitecustomize.py"
    site_code = (
        "import importlib.util, sys\n"
        f"sys.path.insert(0, {str(TEST_DIR)!r})\n"
        "import conftest as _c\n"
        "_c._install_astrbot_test_double()\n"
    )
    if not site_file.exists() or site_file.read_text(encoding="utf-8") != site_code:
        site_file.write_text(site_code, encoding="utf-8")
    existing_pp = os.environ.get("PYTHONPATH", "")
    parts = [p for p in existing_pp.split(os.pathsep) if p]
    if str(stub_dir) not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([str(stub_dir), *parts])


_install_astrbot_test_double()


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "asyncio: mark test as an asyncio coroutine test"
    )


if importlib.util.find_spec("pytest_asyncio") is None:

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(pyfuncitem):
        test_func = pyfuncitem.obj
        if inspect.iscoroutinefunction(test_func):
            sig = inspect.signature(test_func)
            kwargs = {
                name: pyfuncitem.funcargs[name]
                for name in sig.parameters
                if name in pyfuncitem.funcargs
            }
            asyncio.run(test_func(**kwargs))
            return True
        return None
