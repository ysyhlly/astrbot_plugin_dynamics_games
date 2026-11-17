"""User-facing command formatting and parsing for collaborative group activities."""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from .coordinator import ActivityCoordinator
from .models import CATEGORIES, ENGINE_CATALOG, resolve_category


def extract_group_and_sender(event: Any) -> tuple[str, str, str, str, bool]:
    """Return (umo, group_id, uid, nickname, is_admin)."""
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    try:
        gid = str(event.get_group_id() or "")
    except Exception:
        gid = ""
    try:
        uid = str(event.get_sender_id() or "")
    except Exception:
        uid = str(getattr(event, "sender_id", "") or "")
    try:
        name = str(event.get_sender_name() or "").strip()
    except Exception:
        name = ""
    if not name:
        name = f"群友{uid[-4:]}" if uid else "群友"
    try:
        admin = bool(event.is_admin())
    except Exception:
        admin = False
    raw_msg = getattr(event, "message_obj", None)
    msg_id = str(getattr(raw_msg, "message_id", "") or "")
    return umo, gid, uid, name, admin


def extract_message_id(event: Any) -> str:
    raw_msg = getattr(event, "message_obj", None)
    value = getattr(raw_msg, "message_id", None)
    return str(value).strip() if type(value) in (str, int) else ""


def split_tokens(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def build_help_text(module_states: dict[str, bool] | None = None) -> str:
    lines = [
        "🎯 群间游戏 · 一起完成一件事",
        "",
        "【协作主线指令】（支持 /活动、/群间游戏、/共创）",
        "· /活动 发起 <共读|约局|接龙|挑战|共创> <标题> [说明] — 发起一件事（发起人自动报名）",
        "· /活动 列表 — 查看本群招募中/进行中与近期完结的活动",
        "· /活动 详情 [编号] — 查看报名名单、当前阶段与进度明细",
        "· /活动 报名 [编号] [分工/备注] — 报名加入（支持填写分工角色）",
        "· /活动 退出 [编号] — 退出尚未完结的活动",
        "· /活动 开始 [编号] — 结束招募进入正式阶段",
        "· /活动 阶段 [编号] [自定义阶段名] — 推进到下一阶段",
        "· /活动 打卡 <内容> / /活动 提交 <内容> — 提交共读笔记/挑战打卡/接龙下一棒/作品片段",
        "· /活动 完结 [编号] [结项总结] — 完结定稿：生成成果册、经历存入「群间记忆」、达标成果入「群史记」",
        "· /活动 成果 [编号] — 查看已完结活动的汇编作品全文",
        "· /活动 取消 [编号] [原因] — 取消或流局（流局不入群史记）",
        "",
        "【快捷入口】",
        "· /共读 <书名或笔记> · /约局 <游戏名或集合信息> · /接龙 <主题或下一棒> · /挑战 <目标或打卡>",
        "· /活动 娱乐 — 查看已整合的全部娱乐与小游戏模块（真心话 / 21点 / 今日小猪 / 吃什么）",
    ]
    return "\n".join(lines)


def build_arcade_text(module_states: dict[str, bool]) -> str:
    lines = ["🕹️ 群间游戏 · 已集成娱乐模块一览", ""]
    for item in ENGINE_CATALOG:
        enabled = module_states.get(item["key"], True)
        badge = "✅启用" if enabled else "⏸️已关"
        lines.append(
            f"· 【{item['label']}】({badge} · {item['category']})\n"
            f"  入口：{item['entry']}\n"
            f"  说明：{item['desc']}"
        )
    lines.append("")
    lines.append("提示：多人对局完结后会自动同步共同经历至群间记忆。")
    return "\n".join(lines)


def format_activity_card(
    act: dict[str, Any],
    participants: list[dict[str, Any]],
    contributions: list[dict[str, Any]],
) -> str:
    joined = [p for p in participants if p.get("status", "joined") == "joined"]
    stages = act.get("stages") or []
    idx = int(act.get("stage_index") or 0)
    stage_trail = " → ".join(
        f"【{s}】" if i == idx and act["status"] not in ("completed", "cancelled") else s
        for i, s in enumerate(stages)
    )
    target = max(1, int(act.get("target_count") or 1))
    pct = min(100, round(len(contributions) * 100 / target))
    roster = "、".join(
        f"{p['name']}" + (f"({p['role_note']})" if p.get("role_note") else "")
        for p in joined[:20]
    )

    lines = [
        f"{act['category_icon']} 【{act['category_label']}】{act['title']}  [{act['id']}]",
        f"状态：{act['status_label']} ｜ 当前阶段：{act['current_stage']}",
        f"阶段路线：{stage_trail}",
        f"进度：{len(contributions)}/{target}（{pct}%）｜ 参与人数：{len(joined)}/{act['max_participants']}（最少 {act['min_participants']} 人）",
        f"报名成员：{roster or '暂无'}",
    ]
    if act.get("description"):
        lines.append(f"说明：{act['description']}")
    if contributions:
        lines.append("最近记录：")
        for c in contributions[-5:]:
            snippet = str(c["content"]).replace("\n", " ")[:70]
            lines.append(f"  · {c['name']}：{snippet}")
    if act["status"] in ("recruiting", "active"):
        lines.append(
            f"操作：`/活动 报名 {act['id']}` ｜ `/活动 提交 {act['id']} <内容>` ｜ `/活动 完结 {act['id']}`"
        )
    return "\n".join(lines)


async def handle_create_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str
) -> str:
    umo, gid, uid, name, _ = extract_group_and_sender(event)
    if not gid:
        return "群间协作活动需要在群聊中发起哦。"
    tokens = split_tokens(raw_args)
    if len(tokens) < 2:
        return (
            "用法：`/活动 发起 <类型> <标题> [说明]`\n"
            "类型支持：`共读`、`约局`、`接龙`、`挑战`、`共创`\n"
            "示例：\n"
            "· `/活动 发起 共读 银河铁道之夜 每晚打卡一章`\n"
            "· `/活动 发起 约局 今晚九点MC开荒 满4人发车`\n"
            "· `/活动 发起 接龙 雨夜便利店奇遇 每人续写一段`\n"
            "· `/活动 发起 挑战 连续7天早睡打卡`\n"
            "· `/活动 发起 共创 本群年度黑话图鉴 征集词条与配图`"
        )
    category_raw = tokens[0]
    title = tokens[1]
    rest = " ".join(tokens[2:]).strip()
    stages = None
    target_count = None
    description = rest
    # Optional inline metadata: 阶段=A|B|C 目标=10
    clean_desc_parts: list[str] = []
    for part in tokens[2:]:
        if part.startswith(("阶段=", "阶段:")):
            stages = part.split("=", 1)[-1] if "=" in part else part.split(":", 1)[-1]
        elif part.startswith(("目标=", "目标:")):
            val = part.split("=", 1)[-1] if "=" in part else part.split(":", 1)[-1]
            if val.isdigit():
                target_count = int(val)
        else:
            clean_desc_parts.append(part)
    description = " ".join(clean_desc_parts).strip()

    ok, msg, act = await coordinator.create_activity(
        umo=umo,
        group_id=gid,
        category_raw=category_raw,
        title=title,
        description=description,
        creator_uid=uid,
        creator_name=name,
        stages=stages,
        target_count=target_count,
    )
    if not ok or act is None:
        return msg
    cat = CATEGORIES[act["category"]]
    return (
        f"{cat['icon']} 已发起{cat['label']}：《{act['title']}》 [编号 `{act['id']}`]\n"
        f"发起人：{name}（已自动报名）\n"
        f"阶段规划：{' → '.join(act['stages'])}\n"
        + (f"说明：{act['description']}\n" if act.get("description") else "")
        + f"\n群友发送 `/活动 报名 {act['id']}` 即可加入，发送 `/活动 提交 <内容>` 推进进度！"
    )


async def handle_list_command(coordinator: ActivityCoordinator, event: Any) -> str:
    umo, gid, _, _, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中查看本群活动列表。"
    items = await asyncio.to_thread(
        coordinator.store.list_activities, umo=umo, limit=12
    )
    if not items:
        return (
            "本群当前还没有发起过协作活动。\n"
            "试试发送：`/活动 发起 共读 <书名>` 或 `/活动 发起 挑战 <目标>` 一起完成一件事吧！"
        )
    lines = ["📋 本群活动一览：", ""]
    for item in items:
        lines.append(
            f"· `{item['id']}` {item['category_icon']}【{item['category_label']}】《{item['title']}》"
            f" — {item['status_label']}（{item['current_stage']} · {item.get('participant_count', 1)}人 · 进度 {item.get('progress_pct', 0)}%）"
        )
    lines.append("")
    lines.append("查看详情：`/活动 详情 <编号>` ｜ 查看成果：`/活动 成果 <编号>`")
    return "\n".join(lines)


async def handle_detail_command(
    coordinator: ActivityCoordinator, event: Any, activity_id: str = ""
) -> str:
    umo, gid, _, _, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中使用该指令。"
    clean_id = activity_id.strip()
    if clean_id:
        act = await asyncio.to_thread(
            coordinator.store.get_activity, clean_id, umo=umo
        )
    else:
        act = await asyncio.to_thread(
            coordinator.store.resolve_active_activity, umo
        )
    if act is None:
        return "未找到对应活动。可发送 `/活动 列表` 查看全部编号。"
    participants = await asyncio.to_thread(
        coordinator.store.get_participants, act["id"]
    )
    contributions = await asyncio.to_thread(
        coordinator.store.get_contributions, act["id"], 50
    )
    return format_activity_card(act, participants, contributions)


def _split_optional_id(raw: str) -> tuple[str, str]:
    """If the first token looks like G101 / #101 / 101, treat it as activity_id."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    parts = text.split(None, 1)
    first = parts[0].lstrip("#")
    rest = parts[1].strip() if len(parts) > 1 else ""
    if (
        (first.upper().startswith(("G", "M")) and first[1:].isdigit())
        or first.isdigit()
    ):
        return first, rest
    return "", text


async def handle_join_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, name, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中报名活动。"
    act_id, role_note = _split_optional_id(raw_args)
    ok, msg, act = await coordinator.join(
        umo=umo, uid=uid, name=name, activity_id=act_id, role_note=role_note
    )
    if not ok or act is None:
        return msg
    participants = await asyncio.to_thread(
        coordinator.store.get_participants, act["id"]
    )
    joined_cnt = sum(1 for p in participants if p.get("status") == "joined")
    role_str = f"（分工/备注：{role_note}）" if role_note else ""
    return (
        f"✅ {name} 已报名{act['category_label']}《{act['title']}》[{act['id']}]{role_str}！\n"
        f"当前已集结 {joined_cnt} 人（当前阶段：{act['current_stage']}）。"
    )


async def handle_leave_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, _, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中使用该指令。"
    act_id, _ = _split_optional_id(raw_args)
    _, msg = await coordinator.leave(umo=umo, uid=uid, activity_id=act_id)
    return msg


async def handle_stage_command(
    coordinator: ActivityCoordinator,
    event: Any,
    raw_args: str = "",
    *,
    start_only: bool = False,
) -> str:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中使用该指令。"
    act_id, stage_name = _split_optional_id(raw_args)
    ok, msg, act = await coordinator.advance_stage(
        umo=umo,
        actor_uid=uid,
        is_admin=is_admin,
        activity_id=act_id,
        start_only=start_only,
        custom_stage_name=stage_name,
    )
    if not ok or act is None:
        return msg
    return (
        f"🚀 《{act['title']}》[{act['id']}] {msg}\n"
        f"阶段进度：{' → '.join('【' + s + '】' if i == act['stage_index'] else s for i, s in enumerate(act['stages']))}"
    )


async def handle_submit_command(
    coordinator: ActivityCoordinator,
    event: Any,
    raw_args: str = "",
    *,
    category_hint: str = "",
    kind: str = "checkin",
) -> str:
    umo, gid, uid, name, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中提交活动进度。"
    act_id, content = _split_optional_id(raw_args)
    if not content and act_id and not act_id.upper().startswith(("G", "M")):
        content = raw_args.strip()
        act_id = ""
    if not content:
        return "请附上打卡或提交内容，例如：`/活动 提交 第一章读完，印象最深的是星际列车那段`"

    ok, msg, act, total = await coordinator.submit_progress(
        umo=umo,
        uid=uid,
        name=name,
        content=content,
        activity_id=act_id,
        category_hint=category_hint,
        kind=kind,
        message_id=extract_message_id(event),
    )
    if not ok or act is None:
        return msg
    target = max(1, int(act["target_count"]))
    pct = min(100, round(total * 100 / target))
    milestone = (
        "\n🎉 已达到预设进度目标！发起人可随时发送 `/活动 完结` 定稿生成成果册。"
        if total >= target
        else ""
    )
    return (
        f"📝 已记录 {name} 在《{act['title']}》[{act['id']} · {act['current_stage']}] 的第 {total} 条内容（总进度 {total}/{target}，{pct}%）！{milestone}"
    )


async def handle_complete_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中完结活动。"
    act_id, closing_note = _split_optional_id(raw_args)
    ok, sync_report, outcome = await coordinator.complete_activity(
        umo=umo,
        actor_uid=uid,
        is_admin=is_admin,
        activity_id=act_id,
        closing_note=closing_note,
    )
    if not ok or outcome is None:
        return sync_report
    return (
        f"🎊 恭喜完结！{outcome['category_icon']}《{outcome['title']}》[{outcome['activity_id']}] 已正式定稿！\n"
        f"· 参与人数：{outcome['stats'].get('participant_count', 1)} 人 ｜ 累计贡献：{outcome['stats'].get('contribution_count', 0)} 条\n"
        f"· 归档联动：{sync_report}\n"
        f"· 发送 `/活动 成果 {outcome['activity_id']}` 可随时阅览完整汇编作品。"
    )


async def handle_cancel_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中使用该指令。"
    act_id, reason = _split_optional_id(raw_args)
    _, msg, _ = await coordinator.cancel_activity(
        umo=umo,
        actor_uid=uid,
        is_admin=is_admin,
        activity_id=act_id,
        reason=reason,
    )
    return msg


async def handle_outcome_command(
    coordinator: ActivityCoordinator, event: Any, activity_id: str = ""
) -> str:
    umo, gid, _, _, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中查看活动成果。"
    clean_id = activity_id.strip()
    if clean_id:
        outcome = await asyncio.to_thread(coordinator.store.get_outcome, clean_id)
        if outcome and outcome["umo"] != umo:
            outcome = None
    else:
        recent = await asyncio.to_thread(
            coordinator.store.list_outcomes, umo=umo, limit=1
        )
        outcome = (
            await asyncio.to_thread(
                coordinator.store.get_outcome, recent[0]["activity_id"]
            )
            if recent
            else None
        )
    if outcome is None:
        return "尚未找到对应的已完结成果。可发送 `/活动 列表` 查看活动编号。"
    return str(outcome.get("artifact_markdown") or outcome.get("summary") or "")[:3600]


async def handle_category_shortcut(
    coordinator: ActivityCoordinator,
    event: Any,
    category_key: str,
    raw_args: str = "",
) -> str:
    """Smart shortcut for /共读, /约局, /接龙, /挑战."""
    umo, gid, _, _, _ = extract_group_and_sender(event)
    cat = CATEGORIES[category_key]
    if not gid:
        return f"请在群聊中使用 `/{cat['label'][:2]}` 指令。"

    text = str(raw_args or "").strip()
    active = await asyncio.to_thread(
        coordinator.store.resolve_active_activity, umo, "", category_key
    )
    if not text:
        if active:
            return await handle_detail_command(coordinator, event, active["id"])
        return (
            f"{cat['icon']} 当前没有进行中的{cat['label']}。\n"
            f"· 发起：`/{cat['label'][:2] if category_key != 'creation' else '共创'} 发起 <标题> [说明]`（或直接 `/活动 发起 {cat['label'][:2]} <标题>`）\n"
            f"· 报名：`/活动 报名`\n"
            f"· 打卡/续写：`/{cat['label'][:2] if category_key != 'creation' else '共创'} <内容>`"
        )

    parts = text.split(None, 1)
    sub = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if sub in ("发起", "创建", "开始新", "new", "create"):
        return await handle_create_command(
            coordinator, event, f"{category_key} {rest}"
        )
    if sub in ("报名", "加入", "上车", "join"):
        target_id = rest.strip() or (active["id"] if active else "")
        return await handle_join_command(coordinator, event, target_id)
    if sub in ("状态", "详情", "进度", "status"):
        return await handle_detail_command(
            coordinator, event, rest.strip() or (active["id"] if active else "")
        )
    if sub in ("完结", "定稿", "结算", "finish"):
        target_arg = f"{active['id']} {rest}".strip() if active and not rest.startswith(("G", "#")) else rest
        return await handle_complete_command(coordinator, event, target_arg)
    if sub in ("打卡", "提交", "续写", "下一棒"):
        if not active:
            return f"当前没有进行中的{cat['label']}，请先发送 `/活动 发起 {cat['label'][:2]} <标题>` 创建。"
        return await handle_submit_command(
            coordinator,
            event,
            f"{active['id']} {rest}",
            category_hint=category_key,
            kind="relay_turn" if category_key == "relay" else "checkin",
        )

    # If there is already an active activity of this category, treat bare text as a contribution/turn;
    # otherwise treat bare text as creating a new activity!
    if active:
        return await handle_submit_command(
            coordinator,
            event,
            f"{active['id']} {text}",
            category_hint=category_key,
            kind="relay_turn" if category_key == "relay" else "checkin",
        )
    return await handle_create_command(
        coordinator, event, f"{category_key} {text}"
    )
