"""High-level coordinator uniting activity lifecycle, Memory experiences, and Chronicle outcomes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .. import series_panels
from ..bridges.chronicle_bridge import chronicle_status, sync_outcome_to_chronicle
from ..bridges.ecosystem_bridge import (
    emit_game_state_changed,
    emit_milestone_achieved,
    notify_horizons_coread_completed,
    register_world_keepsake,
)
from ..bridges.memory_bridge import memory_status, sync_experience_to_memory
from ..bridges.social_bridge import emit_entertainment_signal
from ..bridges.visual_bridge import generate_artwork_via_visual_media, visual_status
from .models import (
    CATEGORIES,
    build_artifact_markdown,
    evaluate_outcome_worthiness,
    resolve_category,
)
from .poster import render_activity_poster, render_relay_illustration_card
from .store import ActivityStore, check_activity_revision

CHALLENGE_SOFT_PENALTIES: tuple[str, ...] = (
    "发一句真诚检讨 + 一个委屈表情包",
    "群内夸奖本期打卡最多的冠军一句彩虹屁",
    "立下一个明日 5 分钟微目标并拍照打卡",
    "在群里安利一首好歌作为迟到补偿",
)


class ActivityCoordinator:
    """Orchestrate collaborative group activities and ecosystem archiving."""

    def __init__(
        self,
        store: ActivityStore,
        context: Any,
        config: dict[str, Any],
        *,
        plugin: Any = None,
    ) -> None:
        self.store = store
        self.context = context
        self.config = config
        self.plugin = plugin
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

    def stale_reminder_seconds(self) -> float:
        hours = float(self._act_cfg().get("stale_reminder_hours", 24) or 0)
        return max(0.0, hours * 3600.0)

    def poster_on_complete(self) -> bool:
        return bool(self._act_cfg().get("poster_on_complete", True))

    def relay_illustration_interval(self) -> int:
        try:
            return max(0, int(self._act_cfg().get("relay_illustration_interval", 3) or 0))
        except (TypeError, ValueError):
            return 3

    async def cleanup_stale_activities(
        self, *, umo: str = "", max_age_seconds: float | None = None
    ) -> list[dict[str, Any]]:
        age = self.stale_expire_seconds() if max_age_seconds is None else float(max_age_seconds)
        if age <= 0:
            return []
        return await asyncio.to_thread(
            self.store.expire_stale_activities, max_age_seconds=age, umo=umo
        )

    async def check_and_send_stale_reminders(
        self,
        *,
        umo: str = "",
        send_fn: Any = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Send one-time 24h pre-expiry reminders for stale activities, respecting quiet_hours."""
        expire_sec = self.stale_expire_seconds()
        lead_sec = self.stale_reminder_seconds()
        if expire_sec <= 0 or lead_sec <= 0:
            return []
        if series_panels.is_series_in_quiet_hours(self.context):
            return []
        due = await asyncio.to_thread(
            self.store.list_activities_needing_reminder,
            stale_expire_seconds=expire_sec,
            reminder_lead_seconds=lead_sec,
            umo=umo,
            now=now,
        )
        reminded: list[dict[str, Any]] = []
        for act in due:
            target_umo = str(act.get("umo") or "").strip()
            if not target_umo:
                continue
            if series_panels.is_series_group_excluded(self.context, umo=target_umo):
                continue
            if self.plugin is not None and callable(getattr(self.plugin, "_is_scope_disabled", None)):
                if self.plugin._is_scope_disabled(target_umo):
                    continue
            remain_hours = max(
                1,
                int(
                    round(
                        max(
                            0.0,
                            expire_sec
                            - ((time.time() if now is None else float(now)) - float(act.get("updated_at") or 0)),
                        )
                        / 3600.0
                    )
                ),
            )
            text = (
                f"⏰ 活动临期提醒：{act['category_icon']}【{act['category_label']}】《{act['title']}》[{act['id']}] "
                f"已有一段时间未推进，距超时流局清理约剩 {remain_hours} 小时。\n"
                f"· 发送 `/活动 打卡 {act['id']} <内容>` 可续期推进\n"
                f"· 发送 `/活动 完结 {act['id']}` 定稿结项，或 `/活动 取消 {act['id']}` 手动结束"
            )
            sent = False
            try:
                if callable(send_fn):
                    res = send_fn(target_umo, text)
                    if asyncio.iscoroutine(res):
                        await res
                    sent = True
                elif callable(getattr(self.context, "send_message", None)):
                    from astrbot.api.event import MessageChain

                    await self.context.send_message(
                        target_umo, MessageChain().message(text)
                    )
                    sent = True
            except Exception as exc:
                logger.warning("发送活动临期提醒失败 id=%s err=%s", act["id"], exc)
            if sent:
                await asyncio.to_thread(
                    self.store.mark_stale_reminded, act["id"], now=now
                )
                reminded.append(act)
        return reminded

    async def generate_activity_poster(
        self,
        activity_id: str,
        *,
        umo: str = "",
        output_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Generate a shareable activity poster (Visual Media artwork + Pillow fallback)."""
        act = await asyncio.to_thread(
            self.store.get_activity, activity_id, umo=umo or None
        )
        if act is None:
            return {"ok": False, "image_path": "", "error": "未找到该活动", "activity": None}
        participants = await asyncio.to_thread(
            self.store.get_participants, act["id"]
        )
        contributions = await asyncio.to_thread(
            self.store.get_contributions, act["id"], 120
        )
        outcome = await asyncio.to_thread(self.store.get_outcome, act["id"])

        excerpts = "；".join(
            str(c.get("content") or "").strip()[:48] for c in contributions[-3:]
        )
        prompt = (
            f"为群友共同参与的{act['category_label']}《{act['title']}》绘制一张温暖精致的纪念封面插画。"
            + (f"主题说明：{str(act.get('description') or '')[:100]}。" if act.get("description") else "")
            + (f"精彩片段：{excerpts}" if excerpts else "")
        )
        art_res = await generate_artwork_via_visual_media(
            self.context,
            prompt,
            umo=act["umo"],
            style="editorial_poster",
            source_plugin="astrbot_plugin_dynamics_games",
        )
        cover_path = art_res.get("image_path", "") if art_res.get("ok") else ""
        try:
            poster_path = await asyncio.to_thread(
                render_activity_poster,
                act,
                participants,
                contributions,
                outcome=outcome,
                cover_image_path=cover_path,
                output_dir=output_dir,
            )
            if outcome is not None:
                await asyncio.to_thread(
                    self.store.update_outcome_poster, act["id"], str(poster_path)
                )
            return {
                "ok": True,
                "image_path": str(poster_path),
                "artwork_source": "visual_media" if cover_path else "pillow_fallback",
                "activity": act,
                "error": "",
            }
        except Exception as exc:
            logger.warning("生成活动画报失败 id=%s err=%s", act["id"], exc)
            return {
                "ok": False,
                "image_path": "",
                "artwork_source": "error",
                "activity": act,
                "error": str(exc)[:200],
            }

    async def maybe_generate_relay_illustration(
        self,
        act: dict[str, Any],
        total_turns: int,
        author_name: str,
        content: str,
        *,
        output_dir: Path | None = None,
    ) -> dict[str, Any] | None:
        """Auto-illustrate every Nth turn of a story relay (Visual Media + Pillow comic card)."""
        if act.get("category") != "relay":
            return None
        interval = self.relay_illustration_interval()
        if interval <= 0 or total_turns <= 0 or total_turns % interval != 0:
            return None
        prompt = (
            f"故事接龙《{act['title']}》第 {total_turns} 棒分镜插画（绘本连环画风格）："
            f"{str(content or '').strip()[:160]}"
        )
        art_res = await generate_artwork_via_visual_media(
            self.context,
            prompt,
            umo=act["umo"],
            style="comic_panel",
            source_plugin="astrbot_plugin_dynamics_games",
        )
        art_path = art_res.get("image_path", "") if art_res.get("ok") else ""
        try:
            card_path = await asyncio.to_thread(
                render_relay_illustration_card,
                act,
                total_turns,
                author_name,
                content,
                artwork_path=art_path,
                output_dir=output_dir,
            )
            return {
                "image_path": str(card_path),
                "artwork_source": "visual_media" if art_path else "pillow_fallback",
                "turn": total_turns,
            }
        except Exception as exc:
            logger.warning("生成接龙连环画插画失败 id=%s err=%s", act.get("id"), exc)
            return None

    def _get_steam_engine(self) -> Any | None:
        if self.plugin is None:
            return None
        engines = getattr(self.plugin, "engines", None)
        if not isinstance(engines, dict):
            return None
        if callable(getattr(self.plugin, "module_enabled", None)) and not self.plugin.module_enabled("steam_monitor"):
            return None
        return engines.get("steam_monitor")

    async def find_steam_coop_players(
        self, umo: str, group_id: str, game_query: str = ""
    ) -> dict[str, Any]:
        """Query Steam Monitor for online/recent players matching a matchmaking game."""
        eng = self._get_steam_engine()
        if eng is None:
            return {"available": False, "playing_now": [], "recent_players": [], "coop_groups": {}}
        finder = getattr(eng, "get_group_coop_candidates", None)
        if not callable(finder):
            return {"available": False, "playing_now": [], "recent_players": [], "coop_groups": {}}
        try:
            res = await asyncio.to_thread(finder, group_id, game_query)
            return res if isinstance(res, dict) else {"available": False}
        except Exception as exc:
            logger.debug("查询 Steam 开黑同好失败: %s", exc)
            return {"available": False, "playing_now": [], "recent_players": [], "coop_groups": {}}

    async def collect_steam_battle_report(self, act: dict[str, Any]) -> dict[str, Any] | None:
        """Build a post-match Steam battle report for a completed matchmaking activity."""
        if act.get("category") != "matchmaking":
            return None
        eng = self._get_steam_engine()
        if eng is None:
            return None
        builder = getattr(eng, "build_party_battle_report", None)
        if not callable(builder):
            return None
        gid = str(act.get("group_id") or "")
        since_ts = max(0.0, float(act.get("created_at") or 0.0) - 1800.0)
        try:
            report = await asyncio.to_thread(
                builder, gid, str(act.get("title") or ""), since_ts=since_ts
            )
            if isinstance(report, dict) and report.get("has_data"):
                return report
        except Exception as exc:
            logger.debug("生成 Steam 局后战报失败: %s", exc)
        return None

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
                    await self.check_and_send_stale_reminders()
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
        ok, msg, act = await asyncio.to_thread(
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
        if ok and act is not None:
            await emit_game_state_changed(
                self.context,
                umo=umo,
                active=True,
                engine="activities",
                activity_id=act["id"],
                title=act["title"],
                phase=act["status"],
            )
        return ok, msg, act

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
        if ok and updated is not None:
            await emit_game_state_changed(
                self.context,
                umo=umo,
                active=True,
                engine="activities",
                activity_id=updated["id"],
                title=updated["title"],
                phase=updated["current_stage"],
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
            self.store.resolve_active_activity,
            umo,
            activity_id,
            category_hint,
            preferred_uid=uid,
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

    async def withdraw_contribution(
        self,
        *,
        umo: str,
        uid: str,
        activity_id: str = "",
        is_admin: bool = False,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        return await asyncio.to_thread(
            self.store.withdraw_last_contribution,
            umo,
            uid,
            activity_id=activity_id,
            is_admin=is_admin,
        )

    async def rebind_contribution(
        self,
        *,
        umo: str,
        uid: str,
        target_activity_id: str,
        is_admin: bool = False,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        return await asyncio.to_thread(
            self.store.rebind_contribution,
            umo,
            uid,
            target_activity_id,
            is_admin=is_admin,
        )

    async def transfer_creator(
        self,
        *,
        umo: str,
        actor_uid: str,
        is_admin: bool,
        new_creator_uid: str,
        new_creator_name: str = "",
        activity_id: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        target = await asyncio.to_thread(
            self.store.resolve_active_activity,
            umo,
            activity_id,
            preferred_uid=actor_uid,
        )
        if target is None:
            return False, "未找到可移交的进行中活动。", None
        if not is_admin and str(actor_uid) != str(target["creator_uid"]):
            return (
                False,
                f"仅当前发起人（{target['creator_name']}）或群管理员可移交发起人。",
                target,
            )
        return await asyncio.to_thread(
            self.store.transfer_creator,
            target["id"],
            new_creator_uid=new_creator_uid,
            new_creator_name=new_creator_name,
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
        effective_note = str(closing_note or "").strip()
        steam_report = await self.collect_steam_battle_report(target)
        if steam_report and steam_report.get("summary"):
            effective_note = (
                f"{effective_note}\n{steam_report['summary']}".strip()
                if effective_note
                else str(steam_report["summary"])
            )
        if target.get("category") == "challenge":
            parts_now = await asyncio.to_thread(self.store.get_participants, aid)
            missed = [
                p
                for p in parts_now
                if p.get("status", "joined") == "joined"
                and int(p.get("contribution_count") or 0) <= 0
            ]
            if missed:
                names = "、".join(str(p.get("name") or p.get("uid")) for p in missed[:8])
                dare = CHALLENGE_SOFT_PENALTIES[len(missed) % len(CHALLENGE_SOFT_PENALTIES)]
                soft_line = f"趣味软惩罚（未打卡成员：{names}）：{dare}"
                effective_note = (
                    f"{effective_note}\n{soft_line}".strip() if effective_note else soft_line
                )

        for _ in range(3):
            check_activity_revision(target, expected_revision)
            if target["status"] not in {"recruiting", "active"}:
                break
            if await self._save_completion(target, effective_note):
                poster_path = ""
                if self.poster_on_complete() and target.get("category") != "mini_game":
                    poster_res = await self.generate_activity_poster(aid, umo=umo)
                    if poster_res.get("ok"):
                        poster_path = str(poster_res.get("image_path") or "")
                sync_report = await self.sync_outcome_records(aid, image_path=poster_path)
                outcome = await asyncio.to_thread(self.store.get_outcome, aid)
                if outcome is not None:
                    if poster_path:
                        outcome["poster_path"] = poster_path
                    if steam_report:
                        outcome["steam_battle_report"] = steam_report
                    parts_final = await asyncio.to_thread(self.store.get_participants, aid)
                    joined_final = [
                        p for p in parts_final if p.get("status", "joined") == "joined"
                    ]
                    await emit_game_state_changed(
                        self.context,
                        umo=umo,
                        active=False,
                        engine="activities",
                        activity_id=aid,
                        title=target["title"],
                        phase="completed",
                    )
                    await emit_entertainment_signal(
                        self.context,
                        umo=umo,
                        signal_type="steam_party" if steam_report else "activity_completed",
                        summary=str(outcome.get("summary") or ""),
                        participants=joined_final,
                        metadata={
                            "activity_id": aid,
                            "category": target.get("category", ""),
                            "poster_path": poster_path,
                        },
                        mood_hint="celebratory",
                    )
                    if target.get("category") != "mini_game":
                        deep_link = f"games?tab=outcomes&id={aid}"
                        ks_title = f"【{target['category_label']}】{target['title']}"
                        keepsake_res = await register_world_keepsake(
                            self.context,
                            umo=umo,
                            title=ks_title,
                            summary=str(outcome.get("summary") or ""),
                            sender_name=str(target.get("creator_name") or "群友们"),
                            image_path=poster_path,
                            deep_link=deep_link,
                            kind="activity_poster",
                        )
                        outcome["world_keepsake"] = keepsake_res
                        await emit_milestone_achieved(
                            self.context,
                            umo=umo,
                            title=ks_title,
                            summary=str(outcome.get("summary") or ""),
                            participants=joined_final,
                            image_path=poster_path,
                            deep_link=deep_link,
                            created_at=float(target.get("completed_at") or time.time()),
                        )
                        horizons_res = await notify_horizons_coread_completed(
                            self.context,
                            umo=umo,
                            activity=target,
                            outcome=outcome,
                            participants=parts_final,
                        )
                        outcome["horizons_callback"] = horizons_res
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

    async def sync_outcome_records(
        self, activity_id: str, *, image_path: str = ""
    ) -> str:
        """Idempotently sync a completed activity's experience to Memory and outcome to Chronicle."""
        act = await asyncio.to_thread(self.store.get_activity, activity_id)
        if act is None or act["status"] != "completed":
            return "活动尚未完结，跳过外部归档。"
        activity_id = act["id"]
        if activity_id in self._syncing_outcomes:
            return "成果归档正在同步，请稍后查看回执。"
        self._syncing_outcomes.add(activity_id)
        try:
            return await self._sync_outcome_records(act, image_path=image_path)
        finally:
            self._syncing_outcomes.remove(activity_id)

    async def _sync_outcome_records(
        self, act: dict[str, Any], *, image_path: str = ""
    ) -> str:
        activity_id = act["id"]
        outcome = await asyncio.to_thread(self.store.get_outcome, activity_id)
        if outcome is None:
            return "未找到活动成果，跳过外部归档。"
        effective_img = str(image_path or outcome.get("poster_path") or "").strip()

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
                image_path=effective_img,
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
                image_path=effective_img,
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
        ok, msg, updated = await asyncio.to_thread(
            self.store.cancel_activity,
            target["id"],
            reason,
            expected_revision=expected_revision,
        )
        if ok:
            await emit_game_state_changed(
                self.context,
                umo=umo,
                active=False,
                engine="activities",
                activity_id=target["id"],
                title=target["title"],
                phase="cancelled",
            )
        return ok, msg, updated

    async def handle_multimodal_completion(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Archive AI Telephone / Pictionary completion to Memory, World keepsakes & Event Bus."""
        umo = str(payload.get("umo") or "").strip()
        gid = str(payload.get("group_id") or "").strip()
        subgame = str(payload.get("subgame") or "multimodal_party")
        title = str(payload.get("title") or "多模态破冰小游戏")
        summary = str(payload.get("summary") or "")
        participants = list(payload.get("participants") or [])
        image_path = str(payload.get("image_path") or "").strip()
        rounds = max(1, int(payload.get("rounds") or 1))

        outcome = await self.record_minigame_outcome(
            umo=umo,
            group_id=gid,
            game_key=f"multimodal_{subgame}",
            title=title,
            rounds=rounds,
            participants=participants,
            summary=summary,
        )
        act_id = str((outcome or {}).get("activity_id") or "")
        if act_id and image_path:
            await asyncio.to_thread(
                self.store.update_outcome_poster, act_id, image_path
            )
        deep_link = f"games?tab=outcomes&id={act_id}" if act_id else "games?tab=engines"
        keepsake_res: dict[str, Any] = {"ok": False, "status": "skipped"}
        milestone_res: dict[str, Any] = {"ok": False, "status": "skipped"}
        if subgame == "telephone" or image_path:
            keepsake_res = await register_world_keepsake(
                self.context,
                umo=umo,
                title=title,
                summary=summary,
                sender_name="、".join(
                    str(p.get("name") or p.get("uid")) for p in participants[:4]
                )
                or "群友们",
                image_path=image_path,
                deep_link=deep_link,
                kind="telephone_strip" if subgame == "telephone" else "activity_poster",
            )
            milestone_res = await emit_milestone_achieved(
                self.context,
                umo=umo,
                title=title,
                summary=summary,
                participants=participants,
                image_path=image_path,
                deep_link=deep_link,
            )
        return {
            "outcome": outcome,
            "keepsake": keepsake_res,
            "milestone": milestone_res,
        }

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
            await emit_entertainment_signal(
                self.context,
                umo=umo,
                signal_type=str(game_key or "mini_game"),
                summary=summary,
                participants=participants,
                metadata={
                    "activity_id": outcome["activity_id"],
                    "game_key": game_key,
                    "rounds": int(rounds),
                },
                mood_hint="playful",
            )
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
            "visual": visual_status(self.context),
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
