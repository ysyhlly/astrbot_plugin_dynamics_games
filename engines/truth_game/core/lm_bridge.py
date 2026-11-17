"""Soft discovery bridge to the LivingMemory plugin (T9).

This is the ONLY module in this plugin that ever touches
``astrbot_plugin_livingmemory``. Every other module (e.g. ``memory_enrich``)
must go through :func:`detect_lm` / :func:`ensure_ready` / :func:`get_engine` /
:func:`lm_unavailable_reason`.

Hard constraints:

- Importable WITHOUT AstrBot installed: the only LivingMemory imports live in
  the guarded ``try/except`` blocks below. If LivingMemory (or one of its
  transitive deps) is missing or broken, ``_lm_get_active`` degrades to ``None``.
- Never raises: every external access here is wrapped in ``try/except`` or
  ``getattr(..., None)`` so older / half-initialized LivingMemory versions
  degrade to ``None`` / ``False`` / ``[]`` instead of crashing the game.
- Never reads LivingMemory DB files directly.

Note (B2): primary dependency is now LivingMemory's **public facade**
``astrbot_plugin_livingmemory.api.public``. The older private import of
``core.passive_group_capture.get_active_plugin`` is kept only as a fallback for
LivingMemory versions that predate the facade.

Note (B6): facade **functions are resolved lazily at every call** (see
:func:`_facade_fn`) instead of being bound at import time. LivingMemory's
``reload_bootstrap.ensure_current`` removes ``astrbot_plugin_livingmemory.*``
submodules from ``sys.modules`` when its source fingerprint changes, so an
import-time-bound function object would keep pointing at the pre-reload module
forever. Lazy lookup always yields the current module.
"""

from __future__ import annotations

import importlib
from typing import Any

try:
    from ....bridges.memory_bridge import (
        FACADE_MODULES as _LM_FACADE_MODULES,
        MEMORY_ALIASES as _SHARED_LM_ALIASES,
        _star_is_usable,
        _star_meta,
    )
except Exception:  # pragma: no cover - standalone truth_game import fallback
    _LM_FACADE_MODULES = (
        "astrbot_plugin_dynamics_memory.api.public",
        "astrbot_plugin_livingmemory.api.public",
    )
    _SHARED_LM_ALIASES = (
        "astrbot_plugin_dynamics_memory",
        "DynamicsMemory",
        "群间记忆",
        "LivingMemory",
        "astrbot_plugin_livingmemory",
    )

    def _star_is_usable(star: Any) -> bool:
        if star is None or getattr(star, "activated", True) is False:
            return False
        owner = getattr(star, "star_cls", None)
        return (
            owner is not None
            and not getattr(owner, "_terminating", False)
            and not getattr(owner, "_closed", False)
        )

    def _star_meta(star: Any) -> set[str]:
        names: set[str] = set()
        for field in ("name", "display_name", "root_dir_name", "module_path"):
            value = getattr(star, field, None)
            if value is not None:
                text = str(value).strip().lower()
                if text:
                    names.add(text)
        return names

_LM_FACADE_MODULE = _LM_FACADE_MODULES[0]

#: 门面 ALIASES 是常量契约（运行期不会变），import 期取一次即可。
try:
    from astrbot_plugin_livingmemory.api.public import (
        ALIASES as _LM_PUBLIC_ALIASES,
    )
except Exception:  # ImportError or any load error -> facade unavailable
    _LM_PUBLIC_ALIASES = None

#: PRIMARY weakref 读取器：这是**唯一**保留 import 期绑定的门面函数。
#: B6 起 LivingMemory 把 weakref 放进不会被热重载摘除的 ``<pkg>.main``，
#: 因此这个旧函数对象也能读到重载后的新实例（见 passive_group_capture._plugin_ref_holder），
#: 同时保留 ``_lm_get_active is None`` == "LM 不可导入" 的既有语义（测试依赖）。
try:
    from astrbot_plugin_livingmemory.api.public import (
        get_active_plugin as _lm_public_get_active_plugin,
    )
except Exception:  # ImportError or any load error -> facade unavailable
    _lm_public_get_active_plugin = None

#: 旧私有路径（仅当门面不可用时使用；旧版本 LivingMemory 没有 ``api`` 包）。
try:
    from astrbot_plugin_livingmemory.core.passive_group_capture import (
        get_active_plugin as _lm_legacy_get_active,
    )
except Exception:  # ImportError or any plugin load error -> LivingMemory absent
    _lm_legacy_get_active = None

#: PRIMARY weakref reader. Facade first, private module only as fallback.
_lm_get_active = _lm_public_get_active_plugin or _lm_legacy_get_active

#: Plugin identities checked against ``context.get_registered_star`` and
#: against star metadata fields (name / display_name / root_dir_name /
#: module_path), case-insensitively. 门面可用时以门面的 ALIASES 为唯一真源。
_LM_ALIASES: tuple[str, ...] = tuple(_SHARED_LM_ALIASES)

if _LM_PUBLIC_ALIASES:
    _LM_ALIASES = tuple(_LM_PUBLIC_ALIASES)


def _facade_fn(name: str):
    """返回当前门面模块上的可调用对象；门面不可用时 None。"""
    for mod_name in _LM_FACADE_MODULES:
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            continue
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _facade_find_plugin(context: Any) -> Any | None:
    """经门面解析插件实例；门面不可用/调用失败时 None（由本地回退接手）。"""
    fn = _facade_fn("find_plugin")
    if fn is None:
        return None
    try:
        inst = fn(context)
        if inst is not None and (getattr(inst, "_terminating", False) or getattr(inst, "_closed", False)):
            return None
        return inst
    except Exception:
        return None


def detect_lm(context: Any) -> Any | None:
    """Return the LivingMemory plugin instance, or None.

    FIXED resolution order:

    1. PRIMARY — ``_lm_get_active()``: the weakref LivingMemory publishes
       itself; returns the LivingMemoryPlugin instance directly. (Tests drive
       this path by monkeypatching ``_lm_get_active``, so it stays first.)
    2. FACADE — LivingMemory's ``api.public.find_plugin(context)``, resolved
       lazily at call time (B6), which re-implements steps 1/3/4 inside the
       plugin that owns them.
    3. FALLBACK — ``context.get_registered_star(alias)`` for each of
       ``_LM_ALIASES``; the first usable star yields its ``star_cls``.
    4. LAST RESORT — scan ``context.get_all_stars()`` and match each star's
       metadata fields against the same aliases (case-insensitive); the first
       hit yields its ``star_cls``.

    The plugin instance (``star.star_cls``) is returned, never the star meta
    object. Every external access is guarded; this function never raises.

    Steps 3–4 are kept verbatim as a compatibility fallback: they run when the
    facade is unavailable, or when the facade finds nothing (e.g. a partially
    initialised context that the local scan can still resolve).
    """
    if _lm_get_active is not None:
        try:
            plugin = _lm_get_active()
        except Exception:
            plugin = None
        if plugin is not None:
            return plugin

    plugin = _facade_find_plugin(context)
    if plugin is not None:
        return plugin

    try:
        lookup = getattr(context, "get_registered_star", None)
    except Exception:
        lookup = None
    if callable(lookup):
        for alias in _LM_ALIASES:
            try:
                star = lookup(alias)
            except Exception:
                star = None
            if _star_is_usable(star):
                return getattr(star, "star_cls", None)

    try:
        scanner = getattr(context, "get_all_stars", None)
    except Exception:
        scanner = None
    if not callable(scanner):
        return None
    try:
        stars = scanner() or []
    except Exception:
        return None

    wanted = {alias.lower() for alias in _LM_ALIASES}
    for star in stars:
        if _star_is_usable(star) and (_star_meta(star) & wanted):
            return getattr(star, "star_cls", None)
    return None


async def ensure_ready(plugin: Any) -> bool:
    """Ensure the LivingMemory plugin instance is initialized and usable.

    Prefers the facade ``api.public.ensure_ready(plugin, timeout=30.0,
    require=("memory_engine",))`` (resolved lazily, B6); falls back to the local
    implementation for LivingMemory versions without the facade. Both paths only
    ``getattr`` the plugin instance when not yet initialized, await
    ``initializer.ensure_initialized(timeout=30.0)`` (guarded), then confirm the
    memory engine is present. ``False`` on any missing piece; never raises.

    Behaviour is unchanged: this bridge only requires ``memory_engine``
    (the conversation manager is not needed by truth_game).
    """
    facade_ensure_ready = _facade_fn("ensure_ready")
    if facade_ensure_ready is not None:
        try:
            return bool(
                await facade_ensure_ready(
                    plugin, timeout=30.0, require=("memory_engine",)
                )
            )
        except Exception:
            return False

    initializer = getattr(plugin, "initializer", None)
    if initializer is None:
        return False
    if not getattr(initializer, "is_initialized", False):
        ensure = getattr(initializer, "ensure_initialized", None)
        if not callable(ensure):
            return False
        try:
            ok = await ensure(timeout=30.0)
        except Exception:
            ok = False
        if not ok:
            return False
    return getattr(initializer, "memory_engine", None) is not None


def get_engine(plugin: Any) -> Any | None:
    """Return the LivingMemory memory engine, or None when unavailable.

    Prefers the facade accessor ``api.public.get_memory_engine(plugin)``
    (resolved lazily, B6) so the private ``initializer.memory_engine`` attribute
    chain is no longer duplicated here; the local ``getattr`` remains as a
    fallback for older LivingMemory.
    """
    facade_get_memory_engine = _facade_fn("get_memory_engine")
    if facade_get_memory_engine is not None:
        try:
            return facade_get_memory_engine(plugin)
        except Exception:
            return None

    initializer = getattr(plugin, "initializer", None)
    if initializer is None:
        return None
    return getattr(initializer, "memory_engine", None)


def lm_unavailable_reason(context: Any = None) -> str:
    """Most specific Chinese reason LivingMemory is unavailable:

    - module-level import failed            -> "LivingMemory 插件未加载"
    - no plugin instance found              -> "未检测到 LivingMemory"
    - plugin found but not initialized      -> "LivingMemory 尚未初始化"

    Priority: 插件未加载 > 未检测到 > 尚未初始化 (module-level absence wins).
    """
    if _lm_get_active is None:
        return "LivingMemory 插件未加载"
    try:
        plugin = _lm_get_active()
    except Exception:
        plugin = None
    if plugin is None and context is not None:
        plugin = detect_lm(context)
    if plugin is None:
        return "未检测到 LivingMemory"
    return "LivingMemory 尚未初始化"


__all__ = [
    "detect_lm",
    "ensure_ready",
    "get_engine",
    "lm_unavailable_reason",
]
