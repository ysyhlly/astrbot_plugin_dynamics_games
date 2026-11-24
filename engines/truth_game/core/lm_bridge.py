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
    )
    from ....bridges.memory_bridge import (
        MEMORY_ALIASES as _SHARED_LM_ALIASES,
    )
    from ....bridges.memory_bridge import (
        _star_is_usable,
        _star_meta,
    )
    from ....bridges.memory_bridge import (
        find_memory_plugin as _shared_find_memory_plugin,
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
    _shared_find_memory_plugin = None

    def _star_is_usable(star: Any) -> bool:
        if star is None:
            return False
        activated = (
            star.get("activated", True)
            if isinstance(star, dict)
            else getattr(star, "activated", True)
        )
        if activated is False:
            return False
        owner = (
            (star.get("star_cls") or star.get("star") or star.get("plugin"))
            if isinstance(star, dict)
            else (
                getattr(star, "star_cls", None)
                or getattr(star, "star", None)
                or getattr(star, "plugin", None)
            )
        )
        return (
            owner is not None
            and not isinstance(owner, type)
            and not getattr(owner, "_terminating", False)
            and not getattr(owner, "_closed", False)
        )

    def _star_meta(star: Any) -> set[str]:
        names: set[str] = set()
        for field in ("name", "display_name", "root_dir_name", "module_path"):
            value = (
                star.get(field)
                if isinstance(star, dict)
                else getattr(star, field, None)
            )
            if value is not None:
                text = str(value).strip().lower()
                if text:
                    names.add(text)
        return names

_LM_FACADE_MODULE = _LM_FACADE_MODULES[0]


def _is_usable_plugin(inst: Any) -> bool:
    return (
        inst is not None
        and not isinstance(inst, type)
        and not getattr(inst, "_terminating", False)
        and not getattr(inst, "_closed", False)
    )


def _extract_star_owner(star: Any) -> Any | None:
    if star is None:
        return None
    if isinstance(star, dict):
        owner = star.get("star_cls") or star.get("star") or star.get("plugin")
    else:
        owner = (
            getattr(star, "star_cls", None)
            or getattr(star, "star", None)
            or getattr(star, "plugin", None)
        )
    return owner if _is_usable_plugin(owner) else None


#: 优先探测 Dynamics Memory 公开门面，再回退旧版 LivingMemory 门面与旧私有路径。
_facade_active_getters: list[Any] = []
_merged_aliases: list[str] = list(_SHARED_LM_ALIASES)

for _mod_name in _LM_FACADE_MODULES:
    try:
        _mod = importlib.import_module(_mod_name)
    except Exception:
        continue
    _aliases = getattr(_mod, "ALIASES", None)
    if _aliases:
        for _alias in _aliases:
            if _alias and _alias not in _merged_aliases:
                _merged_aliases.append(_alias)
    _getter = getattr(_mod, "get_active_plugin", None)
    if callable(_getter):
        _facade_active_getters.append(_getter)

#: 旧私有路径（仅当门面不可用时使用；旧版本 LivingMemory 没有 ``api`` 包）。
try:
    from astrbot_plugin_livingmemory.core.passive_group_capture import (
        get_active_plugin as _lm_legacy_get_active,
    )
except Exception:  # ImportError or any plugin load error -> LivingMemory absent
    _lm_legacy_get_active = None

if callable(_lm_legacy_get_active):
    _facade_active_getters.append(_lm_legacy_get_active)


def _combined_get_active() -> Any | None:
    for getter in _facade_active_getters:
        try:
            inst = getter()
        except Exception:
            continue
        if _is_usable_plugin(inst):
            return inst
    return None


#: PRIMARY weakref reader. Dynamics Memory / LivingMemory facade first, legacy fallback.
_lm_get_active = _combined_get_active if _facade_active_getters else None

#: Plugin identities checked against ``context.get_registered_star`` and
#: against star metadata fields (name / display_name / root_dir_name /
#: module_path), case-insensitively.
_LM_ALIASES: tuple[str, ...] = tuple(_merged_aliases)


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
    for mod_name in _LM_FACADE_MODULES:
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            continue
        fn = getattr(module, "find_plugin", None)
        if not callable(fn):
            continue
        try:
            inst = fn(context)
        except Exception:
            continue
        if _is_usable_plugin(inst):
            return inst
    return None


def detect_lm(context: Any) -> Any | None:
    """Return the Dynamics Memory / LivingMemory plugin instance, or None.

    Resolution order:
    1. PRIMARY — ``_lm_get_active()`` weakref reader (supports monkeypatching).
    2. FACADE — ``astrbot_plugin_dynamics_memory.api.public`` then
       ``astrbot_plugin_livingmemory.api.public`` ``find_plugin(context)``.
    3. SHARED BRIDGE — ``bridges.memory_bridge.find_memory_plugin(context)``.
    4. FALLBACK — ``context.get_registered_star(alias)`` for ``_LM_ALIASES``.
    5. LAST RESORT — scan ``context.get_all_stars()`` (supports ``dict`` and ``list``).

    Every path validates ``not _terminating and not _closed``. Never raises.
    """
    if _lm_get_active is not None:
        try:
            plugin = _lm_get_active()
        except Exception:
            plugin = None
        if _is_usable_plugin(plugin):
            return plugin

    plugin = _facade_find_plugin(context)
    if _is_usable_plugin(plugin):
        return plugin

    if callable(_shared_find_memory_plugin) and context is not None:
        try:
            plugin = _shared_find_memory_plugin(context)
        except Exception:
            plugin = None
        if _is_usable_plugin(plugin):
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
                owner = _extract_star_owner(star)
                if owner is not None:
                    return owner

    try:
        scanner = getattr(context, "get_all_stars", None)
    except Exception:
        scanner = None
    if not callable(scanner):
        return None
    try:
        raw_stars = scanner() or []
    except Exception:
        return None

    entries = (
        list(raw_stars.items())
        if isinstance(raw_stars, dict)
        else [("", row) for row in raw_stars]
    )
    wanted = {alias.lower() for alias in _LM_ALIASES}
    for key, star in entries:
        if not _star_is_usable(star):
            continue
        names = _star_meta(star)
        if key:
            names.add(str(key).strip().lower())
        if names & wanted:
            owner = _extract_star_owner(star)
            if owner is not None:
                return owner
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
    """Most specific Chinese reason Dynamics Memory / LivingMemory is unavailable:

    1. First probe ``detect_lm(context)`` (or ``_lm_get_active()``) so setups
       with only ``astrbot_plugin_dynamics_memory`` installed are recognized
       accurately instead of misreporting "插件未加载".
    2. If a plugin instance is detected -> "群间记忆 / LivingMemory 尚未初始化"
    3. If neither facade nor active getter is importable -> "群间记忆 / LivingMemory 插件未加载"
    4. Otherwise -> "未检测到 群间记忆 / LivingMemory"
    """
    plugin = detect_lm(context) if context is not None else None
    if plugin is None and _lm_get_active is not None:
        try:
            cand = _lm_get_active()
        except Exception:
            cand = None
        if _is_usable_plugin(cand):
            plugin = cand
    if plugin is not None:
        return "群间记忆 / LivingMemory 尚未初始化"
    if _lm_get_active is None and _facade_fn("find_plugin") is None:
        return "群间记忆 / LivingMemory 插件未加载"
    return "未检测到 群间记忆 / LivingMemory"


__all__ = [
    "detect_lm",
    "ensure_ready",
    "get_engine",
    "lm_unavailable_reason",
]
