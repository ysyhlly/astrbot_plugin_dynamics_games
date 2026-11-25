"""Authenticated Dashboard Web Console API for Dynamics Games."""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api.web import json_response, request

from ..runtime import LEGACY_PLUGINS, PLUGIN_ID
from .models import CATEGORIES, ENGINE_CATALOG
from .store import ActivityConflict

PAGE_SIZE = 80


class GamesWebConsole:
    """Admin-authenticated routes under /astrbot_plugin_dynamics_games/page/... and root aliases."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._routes: list[tuple] = []

    @staticmethod
    def _q(key: str, default: str = "") -> str:
        query = getattr(request, "query", None)
        if query is None:
            query = getattr(request, "args", None) or {}
        return str(query.get(key, default) or "").strip()

    @staticmethod
    async def _body() -> dict[str, Any]:
        getter = getattr(request, "get_json", None)
        data = await getter() if callable(getter) else await request.json()
        if not isinstance(data, dict):
            raise ValueError("请求体无效")
        return data

    async def _require_admin(self, *, check_csrf: bool = False) -> str:
        from ..series_panels import require_series_admin

        return await require_series_admin(
            self.plugin.context, request, check_csrf=check_csrf
        )

    def _wrap(self, fn, *, check_csrf: bool = False):
        async def handler():
            try:
                await self._require_admin(check_csrf=check_csrf)
                data = await fn()
                return json_response({"status": "ok", "data": data})
            except PermissionError as exc:
                return json_response(
                    {"status": "error", "message": str(exc)}, status_code=403
                )
            except ActivityConflict as exc:
                return json_response(
                    {"status": "error", "message": str(exc)}, status_code=409
                )
            except ValueError as exc:
                return json_response(
                    {"status": "error", "message": str(exc)}, status_code=400
                )
            except Exception as exc:
                return json_response(
                    {"status": "error", "message": f"操作失败: {exc}"}, status_code=500
                )

        return handler

    def register(self) -> None:
        from ..series_panels import CSRF_PROTECTED_METHODS
        from ..series_workspace import register_panel_api

        endpoints = (
            ("overview", self.overview, ["GET"], "群间游戏总览"),
            ("activities", self.activities, ["GET"], "群间游戏活动列表"),
            ("activity_detail", self.activity_detail, ["GET"], "群间游戏活动详情"),
            ("activity_create", self.activity_create, ["POST"], "控制台创建群间活动"),
            (
                "activity_action",
                self.activity_action,
                ["POST"],
                "控制台推进/完结/取消活动",
            ),
            ("outcomes", self.outcomes, ["GET"], "群间游戏成果归档列表"),
            ("outcome_detail", self.outcome_detail, ["GET"], "群间游戏成果全文"),
            ("sync_outcome", self.sync_outcome, ["POST"], "手动同步成果至记忆与群史记"),
            ("toggle_module", self.toggle_module, ["POST"], "启停内置娱乐引擎模块"),
        )
        for name, fn, methods, desc in endpoints:
            needs_csrf = any(
                str(m).upper() in CSRF_PROTECTED_METHODS for m in methods
            )
            wrapped = self._wrap(fn, check_csrf=needs_csrf)
            route = f"/{PLUGIN_ID}/{name}"
            register_panel_api(self.plugin, self.plugin.context.register_web_api, route, wrapped, methods, desc)
            self._routes.append((route, wrapped, methods, desc))

    def close(self) -> None:
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(registered, list):
            registered[:] = [r for r in registered if r not in self._routes]
        self._routes.clear()

    def _engine_runtime_snapshot(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in ENGINE_CATALOG:
            key = spec["key"]
            enabled = self.plugin.module_enabled(key)
            engine = self.plugin.engines.get(key)
            active_rooms = 0
            runtime_summary: dict[str, Any] | None = None
            desc = spec["desc"]
            if key == "truth_game" and engine is not None:
                rm = getattr(engine, "room_manager", None)
                active_rooms = len(getattr(rm, "rooms", {}) or {})
            elif key == "blackjack21" and engine is not None:
                active_rooms = len(getattr(engine, "rooms", {}) or {})
            elif key == "steam_monitor" and engine is not None:
                getter = getattr(engine, "get_runtime_summary", None)
                if callable(getter):
                    try:
                        runtime_summary = getter()
                    except Exception:
                        runtime_summary = None
                if isinstance(runtime_summary, dict):
                    groups = int(runtime_summary.get("monitored_groups") or 0)
                    players = int(runtime_summary.get("monitored_players") or 0)
                    playing = int(runtime_summary.get("playing_count") or 0)
                    last_poll = str(runtime_summary.get("last_poll_label") or "尚未轮询")
                    active_rooms = groups
                    desc = (
                        f"{spec['desc']}（监控 {groups} 群 · {players} 人 · "
                        f"当前 {playing} 人在玩 · 上次轮询：{last_poll}）"
                    )
            row: dict[str, Any] = {
                **spec,
                "desc": desc,
                "enabled": enabled,
                "loaded": engine is not None,
                "active_rooms": active_rooms,
                "has_subpage": False,
                "subpage": None,
            }
            if runtime_summary is not None:
                row["runtime_summary"] = runtime_summary
            rows.append(row)
        return rows

    async def overview(self) -> dict[str, Any]:
        umo = self._q("umo")
        stats = await asyncio.to_thread(self.plugin.store.stats_summary, umo)
        umo_set = set(stats.get("known_umos") or [])
        for sib_name in (
            "astrbot_plugin_dynamics_learning",
            "astrbot_plugin_chat_dynamics",
            "astrbot_plugin_group_chronicle",
        ):
            sib = self.plugin.context.get_registered_star(sib_name)
            inst = getattr(sib, "star_cls", None) if sib else None
            cfg = getattr(inst, "config", None)
            if isinstance(cfg, dict):
                for k in (
                    "sessions",
                    "enabled_sessions",
                    "history_allow_groups",
                    "target_groups",
                ):
                    vals = cfg.get(k)
                    if isinstance(vals, list):
                        for v in vals:
                            if isinstance(v, str) and ":GroupMessage:" in v:
                                umo_set.add(v.strip())
        stats["known_umos"] = sorted(umo_set)
        companions = self.plugin.coordinator.companion_health()
        legacy_states = []
        for pid, title in LEGACY_PLUGINS:
            star = self.plugin.context.get_registered_star(pid)
            legacy_states.append(
                {
                    "id": pid,
                    "title": title,
                    "installed": star is not None,
                    "activated": bool(getattr(star, "activated", False))
                    if star
                    else False,
                }
            )
        return {
            "version": self.plugin.VERSION,
            "stats": stats,
            "companions": companions,
            "engines": self._engine_runtime_snapshot(),
            "categories": [
                {
                    "key": k,
                    "label": v["label"],
                    "icon": v["icon"],
                    "default_stages": v["default_stages"],
                }
                for k, v in CATEGORIES.items()
                if k != "mini_game"
            ],
            "legacy_plugins": legacy_states,
            "ownership": {
                "dynamics_games": "负责报名、阶段、进度与成果汇编，以及群聊娱乐引擎调度",
                "dynamics_memory": "负责保存群友共同参与的活动经历与来源关联",
                "group_chronicle": "负责收录有价值的完结成果（流局/取消不入史）",
            },
        }

    async def activities(self) -> dict[str, Any]:
        umo = self._q("umo")
        category = self._q("category")
        status = self._q("status")
        include_mini = self._q("include_minigames", "false").lower() == "true"
        return await self._read_page(
            self.plugin.store.list_activities,
            umo=umo,
            category=category,
            status=status,
            include_minigames=include_mini,
        )

    async def _read_page(self, read, **filters) -> dict[str, Any]:
        page = max(1, min(10000, int(self._q("page", "1"))))
        items = await asyncio.to_thread(
            read, **filters, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE
        )
        return {
            "items": items[:PAGE_SIZE],
            "page": page,
            "has_next": len(items) > PAGE_SIZE,
        }

    async def activity_detail(self) -> dict[str, Any]:
        act_id = self._q("id")
        if not act_id:
            raise ValueError("缺少活动 ID")
        act = await asyncio.to_thread(
            self.plugin.store.get_activity,
            act_id,
            umo=self._q("umo"),
        )
        if act is None:
            raise ValueError("未找到该活动")
        participants = await asyncio.to_thread(
            self.plugin.store.get_participants, act["id"]
        )
        contributions = await asyncio.to_thread(
            self.plugin.store.get_contributions, act["id"], 300
        )
        outcome = await asyncio.to_thread(self.plugin.store.get_outcome, act["id"])
        return {
            "activity": act,
            "participants": participants,
            "contributions": contributions,
            "outcome": outcome,
        }

    async def activity_create(self) -> dict[str, Any]:
        body = await self._body()
        umo = str(body.get("umo") or "").strip()
        if not umo or ":GroupMessage:" not in umo:
            raise ValueError(
                "请填写有效的群会话 UMO（例如 aiocqhttp:GroupMessage:群号）"
            )
        gid = umo.rsplit(":", 1)[-1]
        ok, msg, act = await self.plugin.coordinator.create_activity(
            umo=umo,
            group_id=gid,
            category_raw=str(body.get("category") or "creation"),
            title=str(body.get("title") or ""),
            description=str(body.get("description") or ""),
            creator_uid=str(body.get("creator_uid") or "admin"),
            creator_name=str(body.get("creator_name") or "管理员"),
            stages=body.get("stages"),
            target_count=int(body.get("target_count") or 5),
            min_participants=int(body.get("min_participants") or 2),
        )
        if not ok or act is None:
            raise ValueError(msg)
        return {"message": msg, "activity": act}

    async def activity_action(self) -> dict[str, Any]:
        body = await self._body()
        act_id = str(body.get("id") or "").strip()
        action = str(body.get("action") or "").strip()
        revision = body.get("revision")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("活动版本无效")
        act = await asyncio.to_thread(
            self.plugin.store.get_activity,
            act_id,
            umo=str(body.get("umo") or "").strip(),
        )
        if act is None:
            raise ValueError("未找到该活动")

        if action in ("start", "advance"):
            ok, msg, updated = await self.plugin.coordinator.advance_stage(
                umo=act["umo"],
                actor_uid="admin",
                is_admin=True,
                activity_id=act["id"],
                start_only=(action == "start"),
                custom_stage_name=str(body.get("stage_name") or ""),
                expected_revision=revision,
            )
            if not ok:
                raise ValueError(msg)
            return {"message": msg, "activity": updated}

        if action == "complete":
            ok, msg, outcome = await self.plugin.coordinator.complete_activity(
                umo=act["umo"],
                actor_uid="admin",
                is_admin=True,
                activity_id=act["id"],
                closing_note=str(body.get("note") or ""),
                expected_revision=revision,
            )
            if not ok:
                raise ValueError(msg)
            return {"message": msg, "outcome": outcome}

        if action == "cancel":
            ok, msg, updated = await self.plugin.coordinator.cancel_activity(
                umo=act["umo"],
                actor_uid="admin",
                is_admin=True,
                activity_id=act["id"],
                reason=str(body.get("note") or "控制台取消"),
                expected_revision=revision,
            )
            if not ok:
                raise ValueError(msg)
            return {"message": msg, "activity": updated}

        raise ValueError(f"不支持的操作: {action}")

    async def outcomes(self) -> dict[str, Any]:
        return await self._read_page(
            self.plugin.store.list_outcomes, umo=self._q("umo")
        )

    async def outcome_detail(self) -> dict[str, Any]:
        act_id = self._q("id")
        outcome = await asyncio.to_thread(self.plugin.store.get_outcome, act_id)
        umo = self._q("umo")
        if outcome is None or (umo and outcome["umo"] != umo):
            raise ValueError("未找到该成果记录")
        return outcome

    async def sync_outcome(self) -> dict[str, Any]:
        body = await self._body()
        act_id = str(body.get("id") or "").strip()
        if not act_id:
            raise ValueError("缺少活动 ID")
        umo = str(body.get("umo") or "").strip()
        if umo:
            act = await asyncio.to_thread(
                self.plugin.store.get_activity, act_id, umo=umo
            )
            if act is None:
                raise ValueError("未找到该会话中的活动")
        report = await self.plugin.coordinator.sync_outcome_records(act_id)
        outcome = await asyncio.to_thread(self.plugin.store.get_outcome, act_id)
        return {"report": report, "outcome": outcome}

    async def toggle_module(self) -> dict[str, Any]:
        body = await self._body()
        key = str(body.get("key") or "").strip()
        enabled = bool(body.get("enabled", True))
        await asyncio.to_thread(self.plugin.set_module_enabled, key, enabled)
        return {
            "key": key,
            "enabled": self.plugin.module_enabled(key),
            "engines": self._engine_runtime_snapshot(),
        }
