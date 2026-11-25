"""High-level coordinator uniting activity lifecycle, Memory experiences, and Chronicle outcomes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from ..bridges.chronicle_bridge import chronicle_status, sync_outcome_to_chronicle
from ..bridges.memory_bridge import memory_status, sync_experience_to_memory
from .models import (
    CATEGORIES,
    build_artifact_markdown,
    evaluate_outcome_worthiness,
    resolve_category,
)
from .store import ActivityStore, check_activity_revision


class ActivityCoordinator:
    """Orchestrate collaborative group activities and ecosystem archiving."""

    def __init__(
        self, store: ActivityStore, context: Any, config: dict[str, Any]
    ) -> None:
        self.store = store
        self.context = context
        self.config = config
        self._syncing_outcomes: set[str] = set()
        self.truth_outcome_ready = asyncio.Event()

    def _act_cfg(self) -> dict[str, Any]:
        raw = self.config.get("activities")
        return raw if isinstance(raw, dict) else self.config

    def max_active_per_group(self) -> int:
        return max(1, min(30, int(self._act_cfg().get("max_active_per_group", 5))))

    def memory_sync_enabled(self) -> bool:
        return bool(self._act_cfg().get("memory_sync_enabled", True))

    def chronicle_sync_enabled(self) -> bool:
        return bool(self._act_cfg().get("chronicle_sync_enabled", True))

    def chronicle_admission_mode(self) -> str:
        val = str(self._act_cfg().get("chronicle_admission_mode", "auto") or "auto")
        return val if val in ("auto", "pending") else "auto"

    def min_chronicle_participants(self) -> int:
        return max(1, int(self._act_cfg().get("min_chronicle_participants", 2)))

    def min_chronicle_contributions(self) -> int:
        return max(1, int(self._act_cfg().get("min_chronicle_contributions", 2)))

    def stale_expire_seconds(self) -> float:
        hours = float(self._act_cfg().get("stale_expire_hours", 168) or 0)
        return max(0.0, hours * 3600.0)

    async def cleanup_stale_activities(
        self, *, umo: str = "", max_age_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        age = self.stale_expire_seconds() if max_age_seconds is None else float(max_age_seconds)
        if age <= 0:
            return []
        return await asyncio.to_thread(
            self.store.expire_stale_activities, max_age_seconds=age, umo=umo
        )

    async def enqueue_truth_outcome(self, snapshot: dict[str, Any]) -> None:
        await asyncio.to_thread(self.store.enqueue_truth_room, snapshot)
        self.truth_outcome_ready.set()

    async def flush_truth_outcomes(self) -> None:
        for room_id in await asyncio.to_thread(self.store.pending_truth_rooms):
            try:
                await asyncio.to_thread(self.store.archive_truth_room, room_id)
            except Exception:
                logger.exception("Could not materialize truth room %s", room_id)
        due = await asyncio.to_thread(
            self.store.pending_outcome_syncs,
            memory=self.memory_sync_enabled(),
            chronicle=self.chronicle_sync_enabled(),
        )
        for activity_id in due:
            try:
                await self.sync_outcome_records(activity_id)
            except Exception:
                logger.exception("Could not sync pending outcome %s", activity_id)

    async def run_truth_outcomes(self, room_manager) -> None:
        last_stale_check = 0.0
        while True:
            self.truth_outcome_ready.clear()
            try:
                await room_manager.flush_outcomes()
                await self.flush_truth_outcomes()
                mono = time.monotonic()
                if mono - last_stale_check >= 1800.0:
                    await self.cleanup_stale_activities()
                    last_stale_check = mono
            except Exception:
                logger.exception("Could not read pending truth outcomes")
            try:
                await asyncio.wait_for(self.truth_outcome_ready.wait(), 60)
            except TimeoutError:
                pass

    async def create_activity(
        self,
        *,
        umo: str,
        group_id: str,
        category_raw: str,
        title: str,
        description: str = "",
        creator_uid: str,
        creator_name: str,
        stages: list[str] | str | None = None,
        target_count: int | None = None,
        min_participants: int | None = None,
        max_participants: int = 30,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        category = resolve_category(category_raw)
        if not category or category == "mini_game":
            return (
                False,
                "未知活动类型。支持：`共读`、`约局`、`接龙`、`挑战`、`共创`。",
                None,
            )
        clean_title = str(title or "").strip()
        if not clean_title:
            return False, "请填写活动标题，例如：`/活动 发起 共读 银河铁道之夜`", None

        await self.cleanup_stale_activities(umo=umo)
        return await asyncio.to_thread(
            self.store.create_activity_checked,
            umo=umo,
            group_id=group_id,
            category=category,
            title=clean_title,
            description=description,
            creator_uid=creator_uid,
            creator_name=creator_name,
            stages=stages,
            target_count=target_count,
            min_participants=min_participants,
            max_participants=max_participants,
            max_active_per_group=self.max_active_per_group(),
        )

    async def join(
        self,
        *,
        umo: str,
        uid: str,
        name: str,
        activity_id: str = "",
        role_note: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity, umo, activity_id
        )
        if target is None:
            return (
                False,
                "当前群没有正在招募或进行中的活动。可用 `/活动 发起 <类型> <标题>` 创建一个！",
                None,
            )
        return await asyncio.to_thread(
            self.store.join_activity, target["id"], uid, name, role_note
        )

    async def leave(
        self, *, umo: str, uid: str, activity_id: str = ""
    ) -> tuple[bool, str]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity, umo, activity_id
        )
        if target is None:
            return False, "未找到对应的进行中活动。"
        return await asyncio.to_thread(self.store.leave_activity, target["id"], uid)

    async def advance_stage(
        self,
        *,
        umo: str,
        actor_uid: str,
        is_admin: bool,
        activity_id: str = "",
        start_only: bool = False,
        custom_stage_name: str = "",
        expected_revision: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity, umo, activity_id
        )
        if target is None:
            return False, "当前没有进行中的活动。", None
        if not is_admin and str(actor_uid) != str(target["creator_uid"]):
            return (
                False,
                f"仅发起人（{target['creator_name']}）或群管理员可推进阶段。",
                target,
            )

        ok, msg, updated = await asyncio.to_thread(
            self.store.update_stage,
            target["id"],
            start_only=start_only,
            custom_stage_name=custom_stage_name,
            expected_revision=expected_revision,
        )
        return ok, msg, updated

    async def submit_progress(
        self,
        *,
        umo: str,
        uid: str,
        name: str,
        content: str,
        activity_id: str = "",
        category_hint: str = "",
        kind: str = "checkin",
        message_id: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None, int]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity, umo, activity_id, category_hint
        )
        if target is None:
            hint_label = CATEGORIES.get(category_hint, {}).get("label", "协作活动")
            return (
                False,
                f"当前没有进行中的{hint_label}。可发送 `/活动 发起` 创建新活动。",
                None,
                0,
            )
        return await asyncio.to_thread(
            self.store.add_contribution,
            target["id"],
            uid=uid,
            name=name,
            content=content,
            kind=kind,
            message_id=message_id,
            auto_join=True,
        )

    async def complete_activity(
        self,
        *,
        umo: str,
        actor_uid: str,
        is_admin: bool,
        activity_id: str = "",
        closing_note: str = "",
        expected_revision: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity, umo, activity_id
        )
        if target is None and activity_id.strip():
            target = await asyncio.to_thread(
                self.store.get_activity, activity_id, umo=umo
            )
        if target is None:
            return False, "未找到可完结的活动。", None
        if not is_admin and str(actor_uid) != str(target["creator_uid"]):
            return (
                False,
                f"仅发起人（{target['creator_name']}）或管理员可以完结定稿。",
                None,
            )

        aid = target["id"]
        for _ in range(3):
            check_activity_revision(target, expected_revision)
            if target["status"] not in {"recruiting", "active"}:
                break
            if await self._save_completion(target, closing_note):
                sync_report = await self.sync_outcome_records(aid)
                outcome = await asyncio.to_thread(self.store.get_outcome, aid)
                return True, sync_report, outcome
            target = await asyncio.to_thread(self.store.get_activity, aid, umo=umo)
            if target is None:
                return False, "未找到可完结的活动。", None

        if target["status"] == "completed":
            outcome = await asyncio.to_thread(self.store.get_outcome, aid)
            return False, f"活动《{target['title']}》已经完结归档。", outcome
        if target["status"] == "cancelled":
            return False, f"活动《{target['title']}》已取消，不能标记为完结。", None
        return False, "活动记录仍在更新，请刷新后重新完结。", None

    async def _save_completion(self, target: dict[str, Any], closing_note: str) -> bool:
        participants = await asyncio.to_thread(
            self.store.get_participants, target["id"]
        )
        contributions = await asyncio.to_thread(
            self.store.get_contributions, target["id"], 500
        )
        joined = [p for p in participants if p.get("status", "joined") == "joined"]

        valuable, worthiness_reason, importance = evaluate_outcome_worthiness(
            {**target, "status": "completed"},
            participants,
            contributions,
            min_participants=self.min_chronicle_participants(),
            min_contributions=self.min_chronicle_contributions(),
        )

        artifact_md = build_artifact_markdown(
            target, participants, contributions, closing_note=closing_note
        )
        cat = CATEGORIES.get(target["category"], CATEGORIES["creation"])
        member_names = "、".join(p["name"] for p in joined[:12])
        excerpts = "；".join(
            f"{c['name']}：{str(c['content']).strip()[:80]}" for c in contributions[:6]
        )
        summary_parts = [
            f"群友共同完成{cat['label']}《{target['title']}》（编号 {target['id']}），共 {len(joined)} 人参与（{member_names}），历经 {len(target['stages'])} 个阶段、累计 {len(contributions)} 条推进记录。"
        ]
        if closing_note.strip():
            summary_parts.append(f"结项总结：{closing_note.strip()[:300]}")
        elif excerpts:
            summary_parts.append(f"内容节选：{excerpts[:450]}")
        summary = " ".join(summary_parts)

        stats = {
            "participant_count": len(joined),
            "contribution_count": len(contributions),
            "stages_total": len(target["stages"]),
        }
        return await asyncio.to_thread(
            self.store.save_completion,
            target["id"],
            expected_revision=target["revision"],
            closing_note=closing_note,
            summary=summary,
            artifact_markdown=artifact_md,
            valuable=valuable,
            worthiness_reason=worthiness_reason,
            importance=importance,
            participants_snapshot=joined,
            stats=stats,
        )

    async def sync_outcome_records(self, activity_id: str) -> str:
        """Idempotently sync a completed activity's experience to Memory and outcome to Chronicle."""
        act = await asyncio.to_thread(self.store.get_activity, activity_id)
        if act is None or act["status"] != "completed":
            return "活动尚未完结，跳过外部归档。"
        activity_id = act["id"]
        if activity_id in self._syncing_outcomes:
            return "成果归档正在同步，请稍后查看回执。"
        self._syncing_outcomes.add(activity_id)
        try:
            return await self._sync_outcome_records(act)
        finally:
            self._syncing_outcomes.remove(activity_id)

    async def _sync_outcome_records(self, act: dict[str, Any]) -> str:
        activity_id = act["id"]
        outcome = await asyncio.to_thread(self.store.get_outcome, activity_id)
        if outcome is None:
            return "未找到活动成果，跳过外部归档。"

        participants = await asyncio.to_thread(self.store.get_participants, activity_id)
        contributions = await asyncio.to_thread(
            self.store.get_contributions, activity_id, 200
        )
        notes: list[str] = []
        retryable_mem_statuses = {
            "pending",
            "unavailable",
            "error",
            "timeout",
            "not_initialized",
            "engine_unavailable",
            "source_store_unavailable",
            "memory_index_unavailable",
            "memory_write_failed",
            "source_snapshot_changed",
        }
        retryable_chr_statuses = {"pending", "unavailable", "error", "timeout"}
        retryable_failure = False
        sync_errors: list[str] = []

        # 1. Sync experience to Dynamics Memory (记忆负责保存经历)
        if self.memory_sync_enabled() and outcome["memory_status"] in {
            "synced",
            "duplicate",
        }:
            notes.append("经历已保存至「群间记忆」")
        elif self.memory_sync_enabled():
            source_refs = [
                {
                    "message_id": str(c.get("message_id") or ""),
                    "uid": str(c.get("uid") or ""),
                    "ts": float(c.get("created_at") or 0),
                }
                for c in contributions if c.get("message_id") or c.get("kind") != "game_round"
            ]
            mem_res = await sync_experience_to_memory(
                self.context,
                umo=act["umo"],
                activity_id=act["id"],
                category=act["category"],
                category_label=act["category_label"],
                title=act["title"],
                event_kind="completed",
                summary=outcome["summary"],
                participants=participants,
                source_refs=source_refs[:24],
                occurred_at=act.get("completed_at") or time.time(),
            )
            await asyncio.to_thread(
                self.store.update_sync_status,
                act["id"],
                memory_status=mem_res["status"],
                memory_doc_id=mem_res["doc_id"] if mem_res.get("doc_id") else None,
            )
            if mem_res["ok"]:
                notes.append("经历已保存至「群间记忆」")
            else:
                notes.append(f"群间记忆未写入（{mem_res['reason']}）")
                if mem_res["status"] in retryable_mem_statuses:
                    retryable_failure = True
                    sync_errors.append(f"memory:{mem_res['status']}")

        # 2. Sync valuable outcome to Group Chronicle (群史记负责收录有价值的结果)
        if self.chronicle_sync_enabled() and outcome["chronicle_event_id"]:
            notes.append(
                "待审入史"
                if outcome["chronicle_status"] == "pending"
                else "已收录入「群史记」"
            )
        elif self.chronicle_sync_enabled() and outcome["valuable"]:
            chr_res = await sync_outcome_to_chronicle(
                self.context,
                umo=act["umo"],
                activity=act,
                participants=participants,
                contributions=contributions,
                summary=outcome["summary"],
                valuable=True,
                importance=int(outcome.get("importance") or 1),
                admission_mode=self.chronicle_admission_mode(),
            )
            await asyncio.to_thread(
                self.store.update_sync_status,
                act["id"],
                chronicle_status=chr_res["status"],
                chronicle_event_id=chr_res["event_id"]
                if chr_res.get("event_id")
                else None,
            )
            if chr_res["ok"]:
                state_txt = (
                    "待审入史"
                    if chr_res["status"] == "pending"
                    else "已收录入「群史记」"
                )
                notes.append(state_txt)
            else:
                notes.append(f"群史记未收录（{chr_res['reason']}）")
                if chr_res["status"] in retryable_chr_statuses:
                    retryable_failure = True
                    sync_errors.append(f"chronicle:{chr_res['status']}")
        elif not outcome["valuable"]:
            notes.append(outcome["worthiness_reason"])

        await asyncio.to_thread(
            self.store.record_sync_attempt,
            act["id"],
            succeeded=not retryable_failure,
            error="; ".join(sync_errors),
        )

        return "；".join(notes)

    async def cancel_activity(
        self,
        *,
        umo: str,
        actor_uid: str,
        is_admin: bool,
        activity_id: str = "",
        reason: str = "",
        expected_revision: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity, umo, activity_id
        )
        if target is None:
            return False, "当前没有可取消的活动。", None
        if not is_admin and str(actor_uid) != str(target["creator_uid"]):
            return (
                False,
                f"仅发起人（{target['creator_name']}）或群管理员可取消活动。",
                target,
            )
        return await asyncio.to_thread(
            self.store.cancel_activity,
            target["id"],
            reason,
            expected_revision=expected_revision,
        )

    async def record_minigame_outcome(
        self,
        *,
        event: Any = None,
        group_id: str = "",
        umo: str = "",
        game_key: str,
        title: str,
        rounds: int,
        participants: list[dict[str, Any]],
        summary: str,
        category: str = "mini_game",
        extra_contributions: list[dict[str, Any]] | None = None,
        dedup_key: str = "",
    ) -> dict[str, Any] | None:
        """Record a completed multi-round room game or exported work into the ledger."""
        if len(participants) < 2 and rounds < 2:
            return None
        resolved_umo = str(umo or "").strip()
        if event is not None:
            if not resolved_umo:
                resolved_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not group_id:
                try:
                    group_id = str(event.get_group_id() or "")
                except Exception:
                    group_id = ""
        if not resolved_umo and group_id:
            resolved_umo = f"aiocqhttp:GroupMessage:{group_id}"
        if not resolved_umo or ":GroupMessage:" not in resolved_umo:
            return None
        umo = resolved_umo

        outcome, created = await asyncio.to_thread(
            self.store.record_minigame_outcome_atomic,
            umo=umo,
            group_id=group_id or umo.rsplit(":", 1)[-1],
            game_key=game_key,
            title=title,
            rounds=rounds,
            participants=participants,
            summary=summary,
            category=category,
            extra_contributions=extra_contributions,
            dedup_key=dedup_key,
            min_chronicle_participants=self.min_chronicle_participants(),
            min_chronicle_contributions=self.min_chronicle_contributions(),
        )
        if created and outcome is not None:
            await self.sync_outcome_records(outcome["activity_id"])
            refreshed = await asyncio.to_thread(
                self.store.get_outcome, outcome["activity_id"]
            )
            if refreshed is not None:
                outcome = refreshed
        return outcome

    def companion_health(self) -> dict[str, Any]:
        return {
            "memory": memory_status(self.context),
            "chronicle": chronicle_status(self.context),
        }

    async def get_group_activity_context(self, umo: str) -> dict[str, Any]:
        """Structured read-only snapshot for Chat Dynamics / LLM tools."""
        open_items = await asyncio.to_thread(
            self.store.list_activities, umo=umo, status="open", limit=10
        )
        recent_outcomes = await asyncio.to_thread(
            self.store.list_outcomes, umo=umo, limit=5
        )
        return {
            "schema_version": 1,
            "umo": umo,
            "active_count": len(open_items),
            "active_activities": [
                {
                    "id": item["id"],
                    "category": item["category"],
                    "category_label": item["category_label"],
                    "title": item["title"],
                    "status": item["status"],
                    "current_stage": item["current_stage"],
                    "participants": item.get("participant_count", 1),
                    "contributions": item.get("contribution_count", 0),
                    "progress_pct": item.get("progress_pct", 0),
                }
                for item in open_items
            ],
            "recent_outcomes": [
                {
                    "activity_id": o["activity_id"],
                    "category_label": o["category_label"],
                    "title": o["title"],
                    "summary": o["summary"][:240],
                    "valuable": o["valuable"],
                }
                for o in recent_outcomes
            ],
        }
