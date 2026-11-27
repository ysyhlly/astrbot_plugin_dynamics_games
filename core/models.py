"""Domain constants, category definitions, stage templates, and worthiness rules."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CATEGORIES: dict[str, dict[str, Any]] = {
    "reading": {
        "key": "reading",
        "label": "组织共读",
        "icon": "📚",
        "chronicle_kind": "culture",
        "default_stages": ["招募报名", "共读推进", "共读复盘"],
        "action_verb": "共读打卡",
        "default_target": 6,
        "min_participants": 2,
    },
    "matchmaking": {
        "key": "matchmaking",
        "label": "游戏约局",
        "icon": "🎮",
        "chronicle_kind": "activity",
        "default_stages": ["招募组队", "集合发车", "战报结算"],
        "action_verb": "约局进展",
        "default_target": 4,
        "min_participants": 2,
    },
    "relay": {
        "key": "relay",
        "label": "接龙创作",
        "icon": "✍️",
        "chronicle_kind": "project",
        "default_stages": ["招募立题", "接龙续写", "合稿定篇"],
        "action_verb": "接龙续写",
        "default_target": 6,
        "min_participants": 2,
    },
    "challenge": {
        "key": "challenge",
        "label": "小型挑战",
        "icon": "🏆",
        "chronicle_kind": "achievement",
        "default_stages": ["报名立约", "打卡冲刺", "挑战达成"],
        "action_verb": "挑战打卡",
        "default_target": 7,
        "min_participants": 2,
    },
    "creation": {
        "key": "creation",
        "label": "共同制作作品",
        "icon": "🎨",
        "chronicle_kind": "project",
        "default_stages": ["筹备分工", "素材共创", "打磨定稿"],
        "action_verb": "作品提交",
        "default_target": 5,
        "min_participants": 2,
    },
    "mini_game": {
        "key": "mini_game",
        "label": "局内对局",
        "icon": "🎲",
        "chronicle_kind": "activity",
        "default_stages": ["对局进行", "对局结算"],
        "action_verb": "对局记录",
        "default_target": 1,
        "min_participants": 2,
    },
}

CATEGORY_ALIASES: dict[str, str] = {
    "reading": "reading",
    "共读": "reading",
    "组织共读": "reading",
    "读书": "reading",
    "阅读": "reading",
    "matchmaking": "matchmaking",
    "约局": "matchmaking",
    "游戏约局": "matchmaking",
    "开黑": "matchmaking",
    "组队": "matchmaking",
    "车队": "matchmaking",
    "发车": "matchmaking",
    "relay": "relay",
    "接龙": "relay",
    "接龙创作": "relay",
    "故事接龙": "relay",
    "续写": "relay",
    "challenge": "challenge",
    "挑战": "challenge",
    "小型挑战": "challenge",
    "打卡挑战": "challenge",
    "立flag": "challenge",
    "creation": "creation",
    "共创": "creation",
    "共同制作": "creation",
    "共同制作作品": "creation",
    "作品": "creation",
    "合集": "creation",
    "制作": "creation",
    "mini_game": "mini_game",
    "对局": "mini_game",
}

STATUS_LABELS: dict[str, str] = {
    "recruiting": "招募报名中",
    "active": "进行中",
    "completed": "已完结",
    "cancelled": "已取消/流局",
}

ENGINE_CATALOG: tuple[dict[str, str], ...] = (
    {
        "key": "truth_game",
        "config_flag": "enable_truth_game",
        "label": "真心话",
        "category": "局内互动",
        "entry": "/真心话 · /报名真心话 · /开始真心话",
        "desc": "群成员报名抽人提问，结合群间记忆智能出题与多轮总结",
    },
    {
        "key": "blackjack21",
        "config_flag": "enable_blackjack21",
        "label": "21点",
        "category": "局内竞技",
        "entry": "/创建21点 · /加入21点 · /退出21点 · /开始21点",
        "desc": "同步回合要牌/停牌博弈，支持大小王小丑模式、点数差惩罚与非 QQ 趣味软惩罚",
    },
    {
        "key": "rollpig",
        "config_flag": "enable_rollpig",
        "label": "今日小猪",
        "category": "日常娱乐",
        "entry": "/今日小猪 · /抽小猪",
        "desc": "每日抽取专属主题小猪图鉴卡片",
    },
    {
        "key": "what_to_eat",
        "config_flag": "enable_what_to_eat",
        "label": "今天吃什么",
        "category": "日常助手",
        "entry": "@机器人 吃什么 · /口味 · /喜欢吃 · /忌口",
        "desc": "按群友口味档案、避重记忆与菜谱库推荐三餐",
    },
    {
        "key": "steam_monitor",
        "config_flag": "enable_steam_monitor",
        "label": "Steam 状态监控",
        "category": "开黑监控",
        "entry": "/steam list · /steamwho · /steam rank · /steam price",
        "desc": "高频精准轮询群友 Steam 游戏状态、成就解锁、自然日时长排行与商店查价",
    },
    {
        "key": "multimodal_party",
        "config_flag": "enable_multimodal_party",
        "label": "AI 传话筒 & 你画我猜",
        "category": "多模态破冰",
        "entry": "/传话筒 · /你画我猜",
        "desc": "联动视觉工坊的图文歪楼传话接龙（含对比长图）与 AI 绘图猜词抢答",
    },
)

RISK_WORDS: tuple[str, ...] = (
    "指控",
    "传闻",
    "据说",
    "疑似",
    "诈骗",
    "违法",
    "出轨",
    "处分",
    "封禁",
    "踢出",
    "争执",
    "冲突",
    "举报",
    "辱骂",
    "霸凌",
    "住址",
    "电话号码",
    "真实姓名",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def resolve_category(raw: str) -> str | None:
    clean = str(raw or "").strip().lower()
    return CATEGORY_ALIASES.get(clean)


def parse_stages(raw: str | list[str] | None, category: str) -> list[str]:
    spec = CATEGORIES.get(category, CATEGORIES["creation"])
    if isinstance(raw, list):
        items = [str(x).strip()[:40] for x in raw if str(x).strip()]
        return items[:12] if items else list(spec["default_stages"])
    if isinstance(raw, str) and raw.strip():
        sep = "|" if "|" in raw else ("->" if "->" in raw else (">" if ">" in raw else ("→" if "→" in raw else "/")))
        items = [part.strip()[:40] for part in raw.split(sep) if part.strip()]
        if len(items) >= 2:
            return items[:12]
    return list(spec["default_stages"])


def evaluate_outcome_worthiness(
    activity: dict[str, Any],
    participants: list[dict[str, Any]],
    contributions: list[dict[str, Any]],
    *,
    min_participants: int = 2,
    min_contributions: int = 2,
) -> tuple[bool, str, int]:
    """Determine whether an activity outcome merits admission into Group Chronicle.

    Returns:
        (valuable, reason, importance_1_to_3)
    """
    status = activity.get("status")
    if status != "completed":
        return False, "活动未正常完结（流局或进行中不收录入群史记）", 1

    active_people = [p for p in participants if p.get("status", "joined") == "joined"]
    contributor_uids = {str(c.get("uid") or "") for c in contributions if c.get("uid")}
    effective_people_count = max(len(active_people), len(contributor_uids))
    category = activity.get("category", "creation")

    if category == "mini_game":
        rounds = int(activity.get("target_count") or len(contributions) or 0)
        if effective_people_count >= 3 and rounds >= 6:
            return True, f"多人深度对局（{effective_people_count}人参与，共{rounds}轮）", 1
        return False, "常规短局仅存入群间记忆，不单独收录进群史记", 1

    if effective_people_count < max(1, int(min_participants)):
        return (
            False,
            f"有效参与人数不足 {min_participants} 人（仅存入群间记忆，不入群史记）",
            1,
        )

    total_chars = sum(len(str(c.get("content") or "").strip()) for c in contributions)
    if len(contributions) < max(1, int(min_contributions)) and total_chars < 120:
        return (
            False,
            f"阶段推进或实质贡献记录不足 {min_contributions} 条（仅存入群间记忆，不入群史记）",
            1,
        )

    # Importance 1..3
    if category in ("creation", "relay", "reading") and (
        effective_people_count >= 4 or len(contributions) >= 8 or total_chars >= 800
    ):
        importance = 3 if (effective_people_count >= 5 and total_chars >= 1500) else 2
    elif effective_people_count >= 4 or len(contributions) >= 6:
        importance = 2
    else:
        importance = 1

    cat_label = CATEGORIES.get(category, {}).get("label", category)
    return (
        True,
        f"{cat_label}达成：{effective_people_count} 人协作完成，累计 {len(contributions)} 条进度/成果记录",
        importance,
    )


def build_artifact_markdown(
    activity: dict[str, Any],
    participants: list[dict[str, Any]],
    contributions: list[dict[str, Any]],
    closing_note: str = "",
) -> str:
    """Compile a structured, human-readable Markdown outcome document."""
    cat = CATEGORIES.get(activity["category"], CATEGORIES["creation"])
    joined = [p for p in participants if p.get("status", "joined") == "joined"]
    names = "、".join(
        f"{p['name']}" + (f"（{p['role_note']}）" if p.get("role_note") else "")
        for p in joined
    ) or activity.get("creator_name", "群成员")

    lines = [
        f"# {cat['icon']} {activity['title']}",
        "",
        f"- **活动编号**：`{activity['id']}`",
        f"- **活动类型**：{cat['label']}",
        f"- **发起成员**：{activity.get('creator_name') or activity.get('creator_uid')}",
        f"- **参与成员（{len(joined)}人）**：{names}",
        f"- **累计记录**：{len(contributions)} 条",
    ]
    if activity.get("description"):
        lines.append(f"- **活动说明**：{activity['description']}")
    if closing_note.strip():
        lines.append(f"- **结项总结**：{closing_note.strip()}")
    lines.append("")

    category = activity["category"]
    if category == "relay" and contributions:
        lines.append("## 接龙合稿正文")
        lines.append("")
        for idx, item in enumerate(contributions, 1):
            lines.append(f"### 第 {idx} 棒 · {item['name']}")
            lines.append(str(item["content"]).strip())
            lines.append("")
    elif category == "creation" and contributions:
        lines.append("## 共创作品汇编")
        lines.append("")
        by_stage: dict[str, list[dict[str, Any]]] = {}
        for item in contributions:
            sname = item.get("stage_name") or "共创片段"
            by_stage.setdefault(sname, []).append(item)
        for sname, rows in by_stage.items():
            lines.append(f"### 【{sname}】")
            for row in rows:
                lines.append(f"- **{row['name']}**：{str(row['content']).strip()}")
            lines.append("")
    elif category == "reading" and contributions:
        lines.append("## 共读笔记与阶段记录")
        lines.append("")
        for item in contributions:
            stage_prefix = f"[{item['stage_name']}] " if item.get("stage_name") else ""
            lines.append(f"- **{item['name']}** {stage_prefix}：{str(item['content']).strip()}")
        lines.append("")
    elif category == "challenge":
        lines.append("## 挑战达成榜")
        lines.append("")
        ranked = sorted(
            joined,
            key=lambda p: (-int(p.get("contribution_count") or 0), p.get("joined_at") or 0),
        )
        for rank, person in enumerate(ranked, 1):
            lines.append(
                f"{rank}. **{person['name']}** — 打卡 {int(person.get('contribution_count') or 0)} 次"
            )
        if contributions:
            lines.append("")
            lines.append("### 打卡节选")
            for item in contributions[-12:]:
                lines.append(f"- **{item['name']}**：{str(item['content']).strip()}")
        lines.append("")
    else:
        if contributions:
            lines.append("## 阶段与过程记录")
            lines.append("")
            for item in contributions:
                lines.append(f"- **{item['name']}**：{str(item['content']).strip()}")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def activity_fingerprint(umo: str, activity_id: str, kind: str) -> str:
    return hashlib.sha256(
        canonical_json(
            {"source": "dynamics_games", "umo": umo, "activity_id": activity_id, "kind": kind}
        ).encode("utf-8")
    ).hexdigest()
