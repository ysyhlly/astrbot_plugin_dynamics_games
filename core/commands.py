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
        "【协作主线指令】（支持 /活动、/群间游戏；/共创 等快捷入口同步兼容常用子指令）",
        "· /活动 发起 <共读|约局|接龙|挑战|共创> <标题> [说明] [阶段=A|B|C] [目标=10] — 发起一件事（发起人自动报名，支持自定义阶段与目标数）",
        "· /活动 列表 — 查看本群招募中/进行中与近期完结的活动",
        "· /活动 详情 [编号] — 查看报名名单、当前阶段与进度明细",
        "· /活动 报名 [编号] [分工/备注] — 报名加入（支持填写分工角色）",
        "· /活动 退出 [编号] — 退出尚未完结的活动",
        "· /活动 开始 [编号] — 结束招募进入正式阶段",
        "· /活动 阶段 [编号] [自定义阶段名] — 推进到下一阶段",
        "· /活动 打卡 <内容> / /活动 提交 <内容> — 提交共读笔记/挑战打卡/接龙下一棒/作品片段",
        "· /活动 撤回 [编号] / /活动 改绑 <目标编号> — 误投后撤回最近一条记录或改绑至目标活动",
        "· /活动 移交 [编号] <@新发起人|昵称> — 发起人或管理员移交活动主持权",
        "· /活动 配图 [编号] — 为活动生成图文画报（联动视觉工坊，无配图引擎自动降级精致文字画报）",
        "· /活动 完结 [编号] [结项总结] — 完结定稿：生成图文画报与成果册、经历存「群间记忆」、达标入「群史记」",
        "· /活动 成果 [编号] — 查看已完结活动的汇编作品全文",
        "· /活动 取消 [编号] [原因] — 取消或流局（流局不入群史记）",
        "· /活动 清理 [小时] — 群管理员手动清理超时未推进的滞留活动",
        "",
        "【快捷入口】",
        "· /共读 <书名或笔记> · /约局 <游戏名或集合信息>（支持 `/约局 摇人 [游戏名]` 联动 Steam） · /接龙 <主题或下一棒> · /挑战 <目标或打卡> · /共创 <作品名或片段>",
        "· 以上快捷入口均支持 `发起/报名/退出/开始/阶段/打卡/撤回/改绑/移交/配图/列表/详情/完结/取消/成果/帮助` 子指令（如 `/共创 发起 <标题>`）",
        "· /活动 娱乐 — 查看已整合的全部娱乐与小游戏模块（真心话 / 21点 / 今日小猪 / 吃什么 / Steam 监控）",
        "· /steam help — 查看 Steam 状态监控、自然日时长排行、成就推送与商店查价指令",
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
    steam_rally_hint = ""
    if act["category"] == "matchmaking":
        coop = await coordinator.find_steam_coop_players(umo, gid, title)
        playing_now = coop.get("playing_now") or []
        recent_players = coop.get("recent_players") or []
        hints: list[str] = []
        if playing_now:
            names = "、".join(
                f"{p['name']}({p['game_name']})" if p.get("game_name") else str(p["name"])
                for p in playing_now[:6]
            )
            hints.append(f"在线游玩中：{names}")
        if recent_players:
            names = "、".join(str(p["name"]) for p in recent_players[:6])
            hints.append(f"近期玩过：{names}")
        if hints:
            steam_rally_hint = f"\n🎮 Steam 同好可摇人：{'；'.join(hints)}（发送 `/约局 摇人` 查看详情）"
    return (
        f"{cat['icon']} 已发起{cat['label']}：《{act['title']}》 [编号 `{act['id']}`]\n"
        f"发起人：{name}（已自动报名）\n"
        f"阶段规划：{' → '.join(act['stages'])}\n"
        + (f"说明：{act['description']}\n" if act.get("description") else "")
        + f"\n群友发送 `/活动 报名 {act['id']}` 即可加入，发送 `/活动 提交 <内容>` 推进进度！"
        + steam_rally_hint
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


async def handle_submit_with_media(
    coordinator: ActivityCoordinator,
    event: Any,
    raw_args: str = "",
    *,
    category_hint: str = "",
    kind: str = "checkin",
) -> tuple[str, str]:
    umo, gid, uid, name, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中提交活动进度。", ""
    act_id, content = _split_optional_id(raw_args)
    explicit_id = bool(act_id)
    if not content and act_id and not act_id.upper().startswith(("G", "M")):
        content = raw_args.strip()
        act_id = ""
        explicit_id = False
    if not content:
        return "请附上打卡或提交内容，例如：`/活动 提交 第一章读完，印象最深的是星际列车那段`", ""

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
        return msg, ""

    multi_tip = ""
    if not explicit_id:
        open_acts = await asyncio.to_thread(
            coordinator.store.list_activities,
            umo=umo,
            category=category_hint,
            status="open",
            limit=3,
        )
        if len(open_acts) > 1:
            multi_tip = (
                f"\n💡 本群同时有多个进行中活动，已优先归入 `{act['id']}`《{act['title']}》；"
                f"若投错可发 `/活动 撤回` 或 `/活动 改绑 <目标编号>`。"
            )

    target = max(1, int(act["target_count"]))
    pct = min(100, round(total * 100 / target))
    milestone = (
        "\n🎉 已达到预设进度目标！发起人可随时发送 `/活动 完结` 定稿生成成果册。"
        if total >= target
        else ""
    )
    illust_path = ""
    illust_note = ""
    if act.get("category") == "relay":
        illust = await coordinator.maybe_generate_relay_illustration(
            act, total, name, content
        )
        if illust and illust.get("image_path"):
            illust_path = str(illust["image_path"])
            illust_note = f"\n🎨 已为第 {total} 棒自动生成接龙连环画插画！"

    text = (
        f"📝 已记录 {name} 在《{act['title']}》[{act['id']} · {act['current_stage']}] 的第 {total} 条内容（总进度 {total}/{target}，{pct}%）！"
        f"{milestone}{illust_note}{multi_tip}"
    )
    return text, illust_path


async def handle_submit_command(
    coordinator: ActivityCoordinator,
    event: Any,
    raw_args: str = "",
    *,
    category_hint: str = "",
    kind: str = "checkin",
) -> str:
    text, _ = await handle_submit_with_media(
        coordinator,
        event,
        raw_args,
        category_hint=category_hint,
        kind=kind,
    )
    return text


async def handle_complete_with_media(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> tuple[str, str]:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中完结活动。", ""
    act_id, closing_note = _split_optional_id(raw_args)
    ok, sync_report, outcome = await coordinator.complete_activity(
        umo=umo,
        actor_uid=uid,
        is_admin=is_admin,
        activity_id=act_id,
        closing_note=closing_note,
    )
    if not ok or outcome is None:
        return sync_report, ""
    poster_path = str(outcome.get("poster_path") or "").strip()
    poster_line = "\n· 图文画报：已生成纪念画报卡片并附带归档。" if poster_path else ""
    text = (
        f"🎊 恭喜完结！{outcome['category_icon']}《{outcome['title']}》[{outcome['activity_id']}] 已正式定稿！\n"
        f"· 参与人数：{outcome['stats'].get('participant_count', 1)} 人 ｜ 累计贡献：{outcome['stats'].get('contribution_count', 0)} 条\n"
        f"· 归档联动：{sync_report}{poster_line}\n"
        f"· 发送 `/活动 成果 {outcome['activity_id']}` 可随时阅览完整汇编作品。"
    )
    return text, poster_path


async def handle_complete_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    text, _ = await handle_complete_with_media(coordinator, event, raw_args)
    return text


async def handle_poster_command(
    coordinator: ActivityCoordinator, event: Any, activity_id: str = ""
) -> tuple[str, str]:
    umo, gid, uid, _, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中生成活动配图画报。", ""
    clean_id = activity_id.strip()
    if not clean_id:
        act = await asyncio.to_thread(
            coordinator.store.resolve_active_activity, umo, "", "", preferred_uid=uid
        )
        if act is not None:
            clean_id = act["id"]
        else:
            recent = await asyncio.to_thread(
                coordinator.store.list_outcomes, umo=umo, limit=1
            )
            if recent:
                clean_id = recent[0]["activity_id"]
    if not clean_id:
        return "未找到可生成画报的活动。可发送 `/活动 列表` 查看活动编号。", ""
    res = await coordinator.generate_activity_poster(clean_id, umo=umo)
    if not res.get("ok") or not res.get("image_path"):
        return f"画报生成失败：{res.get('error') or '请稍后再试'}", ""
    act = res["activity"]
    src_note = (
        "视觉工坊 AI 封面 + 精致画报"
        if res.get("artwork_source") == "visual_media"
        else "群间精致文本画报"
    )
    return (
        f"🖼️ 已为 {act['category_icon']}《{act['title']}》[{act['id']}] 生成画报（{src_note}）：",
        str(res["image_path"]),
    )


async def handle_withdraw_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中撤回活动记录。"
    act_id, _ = _split_optional_id(raw_args)
    _, msg, _ = await coordinator.withdraw_contribution(
        umo=umo, uid=uid, activity_id=act_id, is_admin=is_admin
    )
    return msg


async def handle_rebind_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中改绑活动记录。"
    target_id, rest = _split_optional_id(raw_args)
    if not target_id:
        target_id = rest.strip()
    _, msg, _ = await coordinator.rebind_contribution(
        umo=umo, uid=uid, target_activity_id=target_id, is_admin=is_admin
    )
    return msg


def _extract_first_at_uid(event: Any) -> str:
    try:
        messages = event.get_messages() or []
    except Exception:
        msg_obj = getattr(event, "message_obj", None)
        messages = getattr(msg_obj, "message", None) or []
    bot_id = ""
    try:
        bot_id = str(event.get_self_id() or "").strip()
    except Exception:
        bot_id = ""
    for comp in messages:
        qq = str(getattr(comp, "qq", "") or getattr(comp, "user_id", "") or "").strip()
        if qq and qq not in ("all", "0") and (not bot_id or qq != bot_id):
            return qq
    return ""


async def handle_transfer_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, uid, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中移交活动发起人。"
    act_id, rest = _split_optional_id(raw_args)
    at_uid = _extract_first_at_uid(event)
    clean_rest = rest.strip()
    # If act_id looked numeric (e.g. a QQ UID when no activity ID was passed), treat it as target UID
    if act_id and act_id.isdigit() and not clean_rest and not at_uid:
        if await asyncio.to_thread(coordinator.store.get_activity, act_id, umo=umo) is None:
            clean_rest = act_id
            act_id = ""
    target_uid = at_uid or clean_rest.lstrip("@").strip()
    target_name = ""
    if at_uid and clean_rest and clean_rest.lstrip("@").strip() != at_uid:
        target_name = clean_rest.lstrip("@").strip()
    if not target_uid:
        return "用法：`/活动 移交 [活动编号] <@新发起人|UID|已报名昵称>`"
    _, msg, _ = await coordinator.transfer_creator(
        umo=umo,
        actor_uid=uid,
        is_admin=is_admin,
        new_creator_uid=target_uid,
        new_creator_name=target_name,
        activity_id=act_id,
    )
    return msg


async def handle_steam_rally_command(
    coordinator: ActivityCoordinator, event: Any, game_query: str = ""
) -> str:
    umo, gid, _, _, _ = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中使用 `/约局 摇人`。"
    coop = await coordinator.find_steam_coop_players(umo, gid, game_query.strip())
    if not coop.get("available"):
        return "Steam 状态监控模块当前未启用或暂无本群监控数据。"
    playing_now = coop.get("playing_now") or []
    recent_players = coop.get("recent_players") or []
    coop_groups = coop.get("coop_groups") or {}

    lines = [
        f"🎮 Steam 开黑摇人名单" + (f"（筛选：{game_query.strip()}）" if game_query.strip() else "")
    ]
    if coop_groups:
        lines.append("【当前多人同玩热点】")
        for gname, members in list(coop_groups.items())[:5]:
            names = "、".join(str(m["name"]) for m in members)
            lines.append(f"· 《{gname}》（{len(members)}人在线）：{names}")
    if playing_now:
        lines.append("【当前在线游玩中】")
        for p in playing_now[:10]:
            mins = max(1, int(round(float(p.get("duration_sec") or 0) / 60)))
            lines.append(f"· {p['name']} — 正在玩《{p['game_name']}》（已玩 {mins} 分钟）")
    if recent_players:
        names = "、".join(str(p["name"]) for p in recent_players[:10])
        lines.append(f"【近期玩过同游戏】{names}")
    if len(lines) == 1:
        return "本群当前暂无匹配的 Steam 在线或近期游玩玩家，可直接发送 `/约局 发起 <游戏名>` 公开招募！"
    lines.append("发送 `/约局 发起 <游戏名>` 即可一键开团并在局后自动汇总 Steam 战报！")
    return "\n".join(lines)


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


async def handle_cleanup_command(
    coordinator: ActivityCoordinator, event: Any, raw_args: str = ""
) -> str:
    umo, gid, _, _, is_admin = extract_group_and_sender(event)
    if not gid:
        return "请在群聊中使用该指令。"
    if not is_admin:
        return "仅群管理员可使用 `/活动 清理` 清理超时滞留活动。"
    raw = str(raw_args or "").strip()
    max_age_seconds: float | None = None
    if raw:
        try:
            hours = float(raw)
        except ValueError:
            return "用法：`/活动 清理 [超时小时数]`，例如 `/活动 清理 72`（留空则按默认配置）。"
        if hours <= 0:
            return "超时小时数必须大于 0。"
        max_age_seconds = hours * 3600.0
    expired = await coordinator.cleanup_stale_activities(
        umo=umo, max_age_seconds=max_age_seconds
    )
    if not expired:
        return "本群暂无超过时限未推进的滞留活动。"
    titles = "、".join(f"`{a['id']}`《{a['title']}》" for a in expired[:8])
    extra = f" 等共 {len(expired)} 个" if len(expired) > 8 else f"（共 {len(expired)} 个）"
    return f"🧹 已自动清理超时滞留活动{extra}：{titles}（已流局，未入群史记）。"


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
    full_text = str(outcome.get("artifact_markdown") or outcome.get("summary") or "")
    limit = 3600
    if len(full_text) > limit:
        notice = (
            f"\n\n…（成果全文共 {len(full_text)} 字，群聊展示已截断；"
            f"请前往「群间游戏 Web 控制台 → 成果归档」查看/导出完整全文，编号 `{outcome['activity_id']}`）"
        )
        cut = max(0, limit - len(notice))
        return full_text[:cut].rstrip() + notice
    return full_text


def _shortcut_cmd_name(category_key: str) -> str:
    cat = CATEGORIES[category_key]
    return "共创" if category_key == "creation" else cat["label"][:2]


def _build_shortcut_help_text(category_key: str) -> str:
    cat = CATEGORIES[category_key]
    cmd = _shortcut_cmd_name(category_key)
    return (
        f"{cat['icon']} /{cmd}（{cat['label']}）快捷指令说明：\n"
        f"· `/{cmd}` — 查看当前进行中的{cat['label']}（无则展示引导）\n"
        f"· `/{cmd} 发起 <标题> [说明] [阶段=A|B|C] [目标=10]` — 发起新{cat['label']}\n"
        f"· `/{cmd} 报名 [分工/备注]` ｜ `/{cmd} 退出` — 报名加入或退出\n"
        f"· `/{cmd} 开始` ｜ `/{cmd} 阶段 [阶段名]` — 结束招募或推进阶段\n"
        f"· `/{cmd} 打卡 <内容>`（或直接 `/{cmd} <内容>`） — 提交进度/片段\n"
        f"· `/{cmd} 列表` ｜ `/{cmd} 详情 [编号]` ｜ `/{cmd} 成果 [编号]`\n"
        f"· `/{cmd} 完结 [总结]` ｜ `/{cmd} 取消 [原因]`\n"
        f"完整指令请发送 `/活动 帮助`。"
    )


async def handle_category_shortcut_with_media(
    coordinator: ActivityCoordinator,
    event: Any,
    category_key: str,
    raw_args: str = "",
    *,
    module_states: dict[str, bool] | None = None,
) -> tuple[str, str]:
    """Smart shortcut returning ``(text, optional_image_path)`` for /共读, /约局, /接龙, /挑战, /共创."""
    umo, gid, uid, _, _ = extract_group_and_sender(event)
    cat = CATEGORIES[category_key]
    cmd = _shortcut_cmd_name(category_key)
    if not gid:
        return f"请在群聊中使用 `/{cmd}` 指令。", ""

    text = str(raw_args or "").strip()
    active = await asyncio.to_thread(
        coordinator.store.resolve_active_activity,
        umo,
        "",
        category_key,
        preferred_uid=uid,
    )
    if not text:
        if active:
            return await handle_detail_command(coordinator, event, active["id"]), ""
        return (
            f"{cat['icon']} 当前没有进行中的{cat['label']}。\n"
            f"· 发起：`/{cmd} 发起 <标题> [说明]`（或 `/活动 发起 {cmd} <标题>`）\n"
            f"· 报名：`/{cmd} 报名` ｜ 列表：`/{cmd} 列表` ｜ 帮助：`/{cmd} 帮助`\n"
            f"· 打卡/续写：`/{cmd} <内容>`"
        ), ""

    parts = text.split(None, 1)
    sub = parts[0]
    sub_lower = sub.lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    def _target_for_active(arg_text: str) -> str | None:
        explicit_id, _ = _split_optional_id(arg_text)
        if explicit_id:
            return arg_text
        if active:
            return f"{active['id']} {arg_text}".strip()
        return None

    if sub_lower in ("帮助", "help", "说明", "?", "？"):
        return _build_shortcut_help_text(category_key), ""

    if sub_lower in ("娱乐", "arcade", "modules", "模块", "小游戏"):
        states = (
            dict(module_states)
            if module_states is not None
            else {item["key"]: True for item in ENGINE_CATALOG}
        )
        return build_arcade_text(states), ""

    if sub_lower in ("列表", "list", "全部"):
        return await handle_list_command(coordinator, event), ""

    if sub_lower in ("成果", "导出", "outcome", "result", "export", "查看成果"):
        return await handle_outcome_command(coordinator, event, rest), ""

    if sub_lower in ("配图", "画报", "poster", "封面"):
        target_id = rest or (active["id"] if active else "")
        return await handle_poster_command(coordinator, event, target_id)

    if sub_lower in ("撤回", "withdraw", "undo", "撤销"):
        target_arg = _target_for_active(rest) if rest or active else ""
        return await handle_withdraw_command(coordinator, event, target_arg or ""), ""

    if sub_lower in ("改绑", "rebind", "转移打卡", "换绑"):
        return await handle_rebind_command(coordinator, event, rest), ""

    if sub_lower in ("移交", "转让", "transfer", "换房主"):
        target_arg = _target_for_active(rest) if active and not _split_optional_id(rest)[0] else rest
        return await handle_transfer_command(coordinator, event, target_arg or rest), ""

    if sub_lower in ("摇人", "拉人", "rally", "同好"):
        query = rest or (active["title"] if active and category_key == "matchmaking" else "")
        return await handle_steam_rally_command(coordinator, event, query), ""

    if sub_lower in ("清理", "cleanup", "prune"):
        return await handle_cleanup_command(coordinator, event, rest), ""

    if sub_lower in ("发起", "创建", "新建", "开始新", "new", "create"):
        tokens = split_tokens(rest)
        if len(tokens) >= 2:
            resolved_first = resolve_category(tokens[0])
            if resolved_first and resolved_first != "mini_game" and (
                category_key == "creation" or resolved_first == category_key
            ):
                return await handle_create_command(coordinator, event, rest), ""
        return (
            await handle_create_command(
                coordinator, event, f"{category_key} {rest}".strip()
            ),
            "",
        )

    if sub_lower in ("报名", "加入", "上车", "join", "signup"):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return (
                f"当前没有正在招募或进行中的{cat['label']}。"
                f"可先发送 `/{cmd} 发起 <标题>` 创建，或使用 `/活动 报名 <编号>`。",
                "",
            )
        return await handle_join_command(coordinator, event, target_arg), ""

    if sub_lower in ("退出", "leave", "跳车"):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return (
                f"当前没有可退出的进行中{cat['label']}。如需退出其他活动请使用 `/活动 退出 <编号>`。",
                "",
            )
        return await handle_leave_command(coordinator, event, target_arg), ""

    if sub_lower in ("开始", "start", "发车"):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return (
                f"当前没有待开始的{cat['label']}，请先发送 `/{cmd} 发起 <标题>` 创建。",
                "",
            )
        return (
            await handle_stage_command(
                coordinator, event, target_arg, start_only=True
            ),
            "",
        )

    if sub_lower in ("阶段", "推进", "下一阶段", "advance", "next", "stage"):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return f"当前没有进行中的{cat['label']}，无法推进阶段。", ""
        return (
            await handle_stage_command(
                coordinator, event, target_arg, start_only=False
            ),
            "",
        )

    if sub_lower in ("状态", "详情", "进度", "status", "info"):
        if rest:
            return await handle_detail_command(coordinator, event, rest), ""
        if active:
            return await handle_detail_command(coordinator, event, active["id"]), ""
        return (
            f"{cat['icon']} 当前没有进行中的{cat['label']}。可发送 `/活动 列表` 查看本群全部活动。",
            "",
        )

    if sub_lower in ("完结", "定稿", "结算", "finish", "complete", "done"):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return (
                f"当前没有可完结的进行中{cat['label']}。如需完结其他活动请使用 `/活动 完结 <编号>`。",
                "",
            )
        return await handle_complete_with_media(coordinator, event, target_arg)

    if sub_lower in ("取消", "流局", "cancel", "abort"):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return (
                f"当前没有可取消的进行中{cat['label']}。如需取消其他活动请使用 `/活动 取消 <编号>`。",
                "",
            )
        return await handle_cancel_command(coordinator, event, target_arg), ""

    if sub_lower in (
        "打卡",
        "提交",
        "续写",
        "下一棒",
        "checkin",
        "submit",
        "contribute",
        "progress",
        "记录",
    ):
        target_arg = _target_for_active(rest)
        if target_arg is None:
            return (
                f"当前没有进行中的{cat['label']}，请先发送 `/{cmd} 发起 <标题>` 创建。",
                "",
            )
        submit_kind = (
            "relay_turn"
            if category_key == "relay"
            else (
                "submission"
                if sub_lower in ("提交", "续写", "submit", "contribute", "下一棒")
                else "checkin"
            )
        )
        return await handle_submit_with_media(
            coordinator,
            event,
            target_arg,
            category_hint=category_key,
            kind=submit_kind,
        )

    # If there is already an active activity of this category, treat bare text as a contribution/turn;
    # otherwise treat bare text as creating a new activity!
    if active:
        return await handle_submit_with_media(
            coordinator,
            event,
            f"{active['id']} {text}",
            category_hint=category_key,
            kind="relay_turn" if category_key == "relay" else "checkin",
        )
    return (
        await handle_create_command(coordinator, event, f"{category_key} {text}"),
        "",
    )


async def handle_category_shortcut(
    coordinator: ActivityCoordinator,
    event: Any,
    category_key: str,
    raw_args: str = "",
    *,
    module_states: dict[str, bool] | None = None,
) -> str:
    """Smart shortcut for /共读, /约局, /接龙, /挑战, /共创."""
    text, _ = await handle_category_shortcut_with_media(
        coordinator,
        event,
        category_key,
        raw_args,
        module_states=module_states,
    )
    return text
