"""用户口味记忆：按人记录喜好/忌口/过敏与历史推荐。"""

from __future__ import annotations

import json
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

_OWNER_PLUGIN_ID = "astrbot_plugin_dynamics_games"
_LEGACY_PLUGIN_ID = "astrbot_plugin_what_to_eat"

# 自然语言口味抽取（轻量规则，不依赖 LLM）
_LIKE_PATTERNS = (
    re.compile(r"(?:我)?(?:很|超|特别|比较)?喜欢(?:吃|的)?\s*([^\s，,。！!？?、；;]{1,16})"),
    re.compile(r"(?:我)?爱吃\s*([^\s，,。！!？?、；;]{1,16})"),
    re.compile(r"(?:我)?想吃\s*([^\s，,。！!？?、；;]{1,16})"),
    re.compile(r"(?:我)?偏好\s*([^\s，,。！!？?、；;]{1,16})"),
)
_DISLIKE_PATTERNS = (
    re.compile(r"(?:我)?(?:不喜欢|讨厌|不想吃|别给我|别推荐)\s*([^\s，,。！!？?、；;]{1,16})"),
    re.compile(r"(?:我)?不吃\s*([^\s，,。！!？?、；;]{1,16})"),
    re.compile(r"(?:我)?忌口\s*([^\s，,。！!？?、；;]{1,16})"),
)
_ALLERGY_PATTERNS = (
    re.compile(r"(?:对)?\s*([^\s，,。！!？?、；;]{1,12})\s*过敏"),
    re.compile(r"过敏[原源]?[是为：:\s]*([^\s，,。！!？?、；;]{1,12})"),
)
_POSITIVE_FEEDBACK = re.compile(
    r"(好吃|不错|还行|可以|喜欢这个|就这个|赞同|赞|yyds|太香了|下次还吃)"
)
_NEGATIVE_FEEDBACK = re.compile(
    r"(不好吃|难吃|不想吃|换一个|别这个|不要这个|讨厌这个|呕|下次别推)"
)

_STOP_WORDS = frozenset(
    {
        "什么",
        "啥",
        "点",
        "啊",
        "呀",
        "呢",
        "吧",
        "啦",
        "的",
        "了",
        "吗",
        "这个",
        "那个",
        "东西",
        "食物",
        "饭",
        "菜",
        "一下",
        "一点",
        "一些",
        "今天",
        "明天",
        "晚上",
        "中午",
        "早上",
    }
)


def _now_ts() -> float:
    return time.time()


def _clean_tag(text: str) -> str | None:
    value = (text or "").strip().strip("“”\"'‘’《》【】[]()（）")
    value = re.sub(r"^[的地得]", "", value)
    value = value.strip()
    if not value or len(value) > 20:
        return None
    if value in _STOP_WORDS:
        return None
    # 去掉尾部语气词
    value = re.sub(r"[啊呀呢吧啦哦哟嘛]+$", "", value).strip()
    if not value or value in _STOP_WORDS:
        return None
    return value


def _normalize_food_key(food: str) -> str:
    """用于去重/匹配的菜名 key（去掉括号分类）。"""
    text = (food or "").strip()
    text = re.sub(r"[（(].*?[）)]", "", text).strip()
    return text


@dataclass
class UserTasteProfile:
    """单个用户的口味档案。"""

    user_id: str
    nickname: str = ""
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    notes: str = ""
    # food -> score（正=偏爱，负=不喜欢）
    food_scores: dict[str, float] = field(default_factory=dict)
    # 最近推荐记录
    recent: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=_now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "likes": list(self.likes),
            "dislikes": list(self.dislikes),
            "allergies": list(self.allergies),
            "notes": self.notes,
            "food_scores": dict(self.food_scores),
            "recent": list(self.recent),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], user_id: str = "") -> UserTasteProfile:
        uid = str(data.get("user_id") or user_id or "").strip()
        likes = [x for x in (data.get("likes") or []) if isinstance(x, str) and x.strip()]
        dislikes = [
            x for x in (data.get("dislikes") or []) if isinstance(x, str) and x.strip()
        ]
        allergies = [
            x for x in (data.get("allergies") or []) if isinstance(x, str) and x.strip()
        ]
        scores_raw = data.get("food_scores") or {}
        scores: dict[str, float] = {}
        if isinstance(scores_raw, dict):
            for k, v in scores_raw.items():
                if isinstance(k, str) and k.strip():
                    try:
                        scores[k.strip()] = float(v)
                    except (TypeError, ValueError):
                        continue
        recent_raw = data.get("recent") or []
        recent: list[dict[str, Any]] = []
        if isinstance(recent_raw, list):
            for item in recent_raw:
                if isinstance(item, dict) and item.get("food"):
                    recent.append(
                        {
                            "food": str(item.get("food") or "").strip(),
                            "ts": float(item.get("ts") or 0),
                            "group_id": str(item.get("group_id") or ""),
                        }
                    )
        return cls(
            user_id=uid,
            nickname=str(data.get("nickname") or ""),
            likes=list(dict.fromkeys(likes)),
            dislikes=list(dict.fromkeys(dislikes)),
            allergies=list(dict.fromkeys(allergies)),
            notes=str(data.get("notes") or ""),
            food_scores=scores,
            recent=recent,
            updated_at=float(data.get("updated_at") or _now_ts()),
        )

    def summary_text(self, *, recent_limit: int = 5) -> str:
        """生成给 LLM / 展示用的简短口味摘要。"""
        parts: list[str] = []
        name = self.nickname or self.user_id or "这位"
        parts.append(f"用户：{name}")
        if self.likes:
            parts.append("喜欢：" + "、".join(self.likes[:12]))
        if self.dislikes:
            parts.append("不吃/不喜欢：" + "、".join(self.dislikes[:12]))
        if self.allergies:
            parts.append("过敏：" + "、".join(self.allergies[:8]))
        if self.notes:
            parts.append(f"备注：{self.notes[:40]}")
        # 高分菜
        top = sorted(
            ((k, v) for k, v in self.food_scores.items() if v > 0),
            key=lambda x: x[1],
            reverse=True,
        )[:5]
        if top:
            parts.append("偏爱过：" + "、".join(k for k, _ in top))
        recent_foods = [
            str(x.get("food") or "").strip()
            for x in self.recent[-recent_limit:]
            if x.get("food")
        ]
        if recent_foods:
            parts.append("最近推荐过：" + "、".join(recent_foods))
        if len(parts) == 1:
            parts.append("口味档案为空（尚无记录）")
        return "；".join(parts)

    def is_forbidden(self, food: str) -> bool:
        """菜名是否触碰忌口/过敏。"""
        key = _normalize_food_key(food).lower()
        if not key:
            return False
        for tag in self.allergies + self.dislikes:
            t = (tag or "").strip().lower()
            if t and (t in key or key in t):
                return True
        # 历史强负分
        for fname, score in self.food_scores.items():
            if score <= -2 and _normalize_food_key(fname).lower() == key:
                return True
        return False

    def recent_foods(self, limit: int = 8) -> list[str]:
        foods = [
            str(x.get("food") or "").strip()
            for x in self.recent[-limit:]
            if x.get("food")
        ]
        return list(dict.fromkeys(foods))


class TasteMemoryStore:
    """
    持久化用户口味。

    存储文件：插件数据目录 / taste_memory.json
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        data_dir: Path | None = None,
        legacy_data_dir: Path | None = None,
        max_recent: int = 20,
        avoid_recent_count: int = 6,
        learn_from_message: bool = True,
        feedback_window_seconds: float = 1800.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_recent = max(5, min(50, int(max_recent)))
        self.avoid_recent_count = max(0, min(30, int(avoid_recent_count)))
        self.auto_learn = bool(learn_from_message)
        try:
            self.feedback_window_seconds = max(
                60.0, float(feedback_window_seconds)
            )
        except (TypeError, ValueError):
            self.feedback_window_seconds = 1800.0
        self._lock = threading.RLock()
        self._profiles: dict[str, UserTasteProfile] = {}
        self._path = self._resolve_path(data_dir, legacy_data_dir=legacy_data_dir)
        self._load()
        logger.info(
            "口味记忆初始化: enabled=%s path=%s users=%s avoid_recent=%s auto_learn=%s",
            self.enabled,
            self._path,
            len(self._profiles),
            self.avoid_recent_count,
            self.auto_learn,
        )

    @staticmethod
    def _resolve_path(
        data_dir: Path | None, *, legacy_data_dir: Path | None = None
    ) -> Path:
        legacy_base: Path | None = (
            Path(legacy_data_dir) if legacy_data_dir is not None else None
        )
        if data_dir is not None:
            base = Path(data_dir)
        else:
            base = None
            try:
                from astrbot.api.star import StarTools

                raw = StarTools.get_data_dir(_OWNER_PLUGIN_ID)
                if raw is not None:
                    base = Path(raw) / "what_to_eat"
                if legacy_base is None:
                    raw_legacy = StarTools.get_data_dir(_LEGACY_PLUGIN_ID)
                    if raw_legacy is not None:
                        legacy_base = Path(raw_legacy)
            except Exception:
                base = None
            if base is None:
                # 回退到插件旁 data/
                base = Path(__file__).resolve().parent / "data"
        base.mkdir(parents=True, exist_ok=True)
        target = base / "taste_memory.json"
        if not target.exists() and legacy_base is not None:
            legacy_file = legacy_base / "taste_memory.json"
            try:
                if (
                    legacy_file.is_file()
                    and legacy_file.resolve() != target.resolve()
                ):
                    tmp = target.with_suffix(".json.migrate.tmp")
                    shutil.copy2(legacy_file, tmp)
                    tmp.replace(target)
                    logger.info(
                        "[WhatToEat] 已自动迁移旧版口味记忆: %s -> %s",
                        legacy_file,
                        target,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[WhatToEat] 迁移旧版口味记忆失败（已保留原文件）: %s", exc
                )
        return target

    def _load(self) -> None:
        if not self._path.exists():
            self._profiles = {}
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            users = data.get("users") if isinstance(data, dict) else {}
            if not isinstance(users, dict):
                users = {}
            profiles: dict[str, UserTasteProfile] = {}
            for uid, pdata in users.items():
                if not isinstance(pdata, dict):
                    continue
                uid_s = str(uid).strip()
                if not uid_s:
                    continue
                profiles[uid_s] = UserTasteProfile.from_dict(pdata, user_id=uid_s)
            self._profiles = profiles
        except Exception as e:
            logger.warning("读取口味记忆失败，将使用空档案: %s", e)
            self._profiles = {}

    def _save(self) -> None:
        self._save_timer: threading.Timer | None
        timer = getattr(self, "_save_timer", None)
        if timer is not None:
            timer.cancel()
            self._save_timer = None
        if getattr(self, "_defer_save", True):
            self._dirty = True
            new_timer = threading.Timer(0.25, self.flush)
            new_timer.daemon = True
            self._save_timer = new_timer
            new_timer.start()
            return
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        payload = {
            "version": 1,
            "updated_at": _now_ts(),
            "users": {uid: p.to_dict() for uid, p in self._profiles.items()},
        }
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self._dirty = False
        except Exception as e:
            logger.warning("保存口味记忆失败: %s", e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    def flush(self) -> None:
        with self._lock:
            timer = getattr(self, "_save_timer", None)
            if timer is not None:
                timer.cancel()
                self._save_timer = None
            if getattr(self, "_dirty", False):
                self._write_snapshot()

    def get_profile(self, user_id: str) -> UserTasteProfile:
        uid = str(user_id or "").strip() or "unknown"
        with self._lock:
            profile = self._profiles.get(uid)
            if profile is None:
                profile = UserTasteProfile(user_id=uid)
                if self.enabled:
                    self._profiles[uid] = profile
            return profile

    def touch_nickname(self, user_id: str, nickname: str | None) -> None:
        if not self.enabled:
            return
        nick = (nickname or "").strip()
        if not nick:
            return
        with self._lock:
            profile = self.get_profile(user_id)
            if profile.nickname != nick:
                profile.nickname = nick
                profile.updated_at = _now_ts()
                self._save()

    def add_likes(self, user_id: str, items: list[str]) -> list[str]:
        return self._add_tags(user_id, items, field_name="likes")

    def add_dislikes(self, user_id: str, items: list[str]) -> list[str]:
        return self._add_tags(user_id, items, field_name="dislikes")

    def add_allergies(self, user_id: str, items: list[str]) -> list[str]:
        return self._add_tags(user_id, items, field_name="allergies")

    def remove_tags(self, user_id: str, items: list[str]) -> list[str]:
        if not self.enabled:
            return []
        cleaned = [t for t in (_clean_tag(x) for x in items) if t]
        if not cleaned:
            return []
        with self._lock:
            profile = self.get_profile(user_id)
            removed: list[str] = []
            for tag in cleaned:
                for field_name in ("likes", "dislikes", "allergies"):
                    lst: list[str] = getattr(profile, field_name)
                    # 精确或包含删除
                    keep = []
                    for x in lst:
                        if x == tag or tag in x or x in tag:
                            removed.append(x)
                        else:
                            keep.append(x)
                    setattr(profile, field_name, list(dict.fromkeys(keep)))
            if removed:
                profile.updated_at = _now_ts()
                self._save()
            return list(dict.fromkeys(removed))

    def clear_profile(self, user_id: str) -> bool:
        if not self.enabled:
            return False
        uid = str(user_id or "").strip()
        with self._lock:
            if uid in self._profiles:
                del self._profiles[uid]
                self._save()
                return True
            return False

    def _add_tags(self, user_id: str, items: list[str], *, field_name: str) -> list[str]:
        if not self.enabled:
            return []
        cleaned = [t for t in (_clean_tag(x) for x in items) if t]
        if not cleaned:
            return []
        with self._lock:
            profile = self.get_profile(user_id)
            lst: list[str] = getattr(profile, field_name)
            added: list[str] = []
            for tag in cleaned:
                # 互斥：加入 likes 时从 dislikes 移除，反之亦然
                if field_name == "likes":
                    profile.dislikes = [x for x in profile.dislikes if x != tag and tag not in x]
                elif field_name == "dislikes":
                    profile.likes = [x for x in profile.likes if x != tag and tag not in x]
                if tag not in lst:
                    lst.append(tag)
                    added.append(tag)
            setattr(profile, field_name, list(dict.fromkeys(lst)))
            if added:
                profile.updated_at = _now_ts()
                self._save()
            return added

    def record_recommendation(
        self,
        user_id: str,
        food: str,
        *,
        group_id: str = "",
        nickname: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        food_name = (food or "").strip()
        if not food_name:
            return
        with self._lock:
            profile = self.get_profile(user_id)
            if nickname and nickname.strip():
                profile.nickname = nickname.strip()
            profile.recent.append(
                {
                    "food": food_name,
                    "ts": _now_ts(),
                    "group_id": str(group_id or ""),
                }
            )
            # 截断
            if len(profile.recent) > self.max_recent:
                profile.recent = profile.recent[-self.max_recent :]
            # 轻微正向：推过的菜以后略优先（可被负反馈抵消），并封顶避免越推越偏
            key = _normalize_food_key(food_name)
            if key:
                next_score = float(profile.food_scores.get(key, 0.0)) + 0.08
                profile.food_scores[key] = min(2.5, next_score)
            profile.updated_at = _now_ts()
            self._save()

    def apply_feedback(
        self,
        user_id: str,
        *,
        positive: bool,
        group_id: str = "",
        now: float | None = None,
        window_seconds: float | None = None,
    ) -> str | None:
        """对最近一次推荐做反馈（仅在时间窗与同会话内生效），返回被反馈的菜名。"""
        if not self.enabled:
            return None
        cur_ts = float(now) if now is not None else _now_ts()
        window = (
            max(0.0, float(window_seconds))
            if window_seconds is not None
            else self.feedback_window_seconds
        )
        with self._lock:
            profile = self.get_profile(user_id)
            if not profile.recent:
                return None
            last = profile.recent[-1]
            last_ts = float(last.get("ts") or 0.0)
            if last_ts <= 0.0 or (cur_ts - last_ts) > window or (last_ts - cur_ts) > 60.0:
                return None
            last_gid = str(last.get("group_id") or "").strip()
            clean_gid = str(group_id or "").strip()
            if clean_gid and last_gid and clean_gid != last_gid:
                return None
            food = str(last.get("food") or "").strip()
            if not food:
                return None
            key = _normalize_food_key(food)
            delta = 1.2 if positive else -1.5
            profile.food_scores[key] = float(profile.food_scores.get(key, 0.0)) + delta
            if not positive:
                # 强负反馈时记入不喜欢（用归一化名）
                if key and key not in profile.dislikes:
                    profile.dislikes.append(key)
            else:
                # 正反馈记入喜欢
                if key and key not in profile.likes:
                    profile.likes.append(key)
                profile.dislikes = [x for x in profile.dislikes if x != key]
            profile.updated_at = cur_ts
            self._save()
            return food

    def learn_from_message(
        self,
        user_id: str,
        message: str,
        *,
        group_id: str = "",
        now: float | None = None,
    ) -> dict[str, list[str]]:
        """
        从用户消息里抽取口味，并处理时间窗内的「好吃/难吃」反馈。

        Returns:
            {"likes": [...], "dislikes": [...], "allergies": [...], "feedback": [...]}
        """
        result: dict[str, list[str]] = {
            "likes": [],
            "dislikes": [],
            "allergies": [],
            "feedback": [],
        }
        if not self.enabled or not self.auto_learn:
            return result
        text = (message or "").strip()
        if not text:
            return result

        likes = self._extract_by_patterns(text, _LIKE_PATTERNS)
        dislikes = self._extract_by_patterns(text, _DISLIKE_PATTERNS)
        allergies = self._extract_by_patterns(text, _ALLERGY_PATTERNS)

        if likes:
            result["likes"] = self.add_likes(user_id, likes)
        if dislikes:
            result["dislikes"] = self.add_dislikes(user_id, dislikes)
        if allergies:
            result["allergies"] = self.add_allergies(user_id, allergies)

        # 对最近推荐的反馈（仅在反馈时间窗内生效，防止旧推荐在数小时/数天后被闲聊误学）
        if len(text) <= 40:
            if _POSITIVE_FEEDBACK.search(text):
                food = self.apply_feedback(
                    user_id, positive=True, group_id=group_id, now=now
                )
                if food:
                    result["feedback"] = [f"+{food}"]
            elif _NEGATIVE_FEEDBACK.search(text):
                food = self.apply_feedback(
                    user_id, positive=False, group_id=group_id, now=now
                )
                if food:
                    result["feedback"] = [f"-{food}"]

        return result

    @staticmethod
    def _extract_by_patterns(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
        found: list[str] = []
        for pat in patterns:
            for m in pat.finditer(text):
                tag = _clean_tag(m.group(1))
                if tag:
                    found.append(tag)
        return list(dict.fromkeys(found))

    def pick_food(
        self,
        user_id: str,
        candidates: list[str],
        *,
        rng: random.Random | None = None,
    ) -> str | None:
        """
        按口味加权从候选中选菜。

        - 过滤忌口/过敏
        - 降低最近推荐权重
        - 提高 likes / 高分菜权重
        """
        foods = [f for f in candidates if isinstance(f, str) and f.strip()]
        if not foods:
            return None

        if not self.enabled:
            return (rng or random).choice(foods)

        profile = self.get_profile(user_id)
        recent_keys = {
            _normalize_food_key(f).lower()
            for f in profile.recent_foods(self.avoid_recent_count)
        }

        weighted: list[tuple[str, float]] = []
        for food in foods:
            if profile.is_forbidden(food):
                continue
            weight = 1.0
            key = _normalize_food_key(food)
            key_l = key.lower()

            # 喜好匹配
            for like in profile.likes:
                like_l = like.lower()
                if like_l and (like_l in key_l or key_l in like_l):
                    weight += 2.5
                    break

            # 历史分数
            score = float(profile.food_scores.get(key, 0.0))
            # 也尝试完整菜名（带分类括号的）
            score = max(score, float(profile.food_scores.get(food, 0.0)))
            weight += max(-1.5, min(3.0, score * 0.6))

            # 最近推荐过则强降权，尽量不连推同一道
            if key_l in recent_keys:
                weight *= 0.05

            weight = max(0.05, weight)
            weighted.append((food, weight))

        if not weighted:
            # 全部被过滤时，放宽忌口仅避开过敏
            for food in foods:
                key_l = _normalize_food_key(food).lower()
                blocked = False
                for tag in profile.allergies:
                    t = tag.lower()
                    if t and (t in key_l or key_l in t):
                        blocked = True
                        break
                if not blocked:
                    weighted.append((food, 1.0))
            if not weighted:
                return (rng or random).choice(foods)

        total = sum(w for _, w in weighted)
        r = (rng or random).random() * total
        acc = 0.0
        for food, w in weighted:
            acc += w
            if r <= acc:
                return food
        return weighted[-1][0]

    def pick_food_for_group(
        self,
        user_ids: list[str],
        candidates: list[str],
        *,
        rng: random.Random | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Pick a food safe for ALL users in ``user_ids`` (intersection of safe foods)."""
        foods = [f for f in candidates if isinstance(f, str) and f.strip()]
        clean_uids = list(dict.fromkeys(str(u).strip() for u in user_ids if str(u or "").strip()))
        meta: dict[str, Any] = {
            "participants": clean_uids,
            "avoided_allergies": [],
            "avoided_dislikes": [],
            "shared_likes": [],
            "relaxed_dislikes": False,
        }
        if not foods:
            return None, meta
        if not self.enabled or not clean_uids:
            return (rng or random).choice(foods), meta

        profiles = [self.get_profile(uid) for uid in clean_uids]
        all_allergies = list(
            dict.fromkeys(tag for p in profiles for tag in p.allergies if tag)
        )
        all_dislikes = list(
            dict.fromkeys(tag for p in profiles for tag in p.dislikes if tag)
        )
        like_counts: dict[str, int] = {}
        for p in profiles:
            for like in set(p.likes):
                like_counts[like] = like_counts.get(like, 0) + 1
        shared_likes = [k for k, cnt in like_counts.items() if cnt >= 2]
        meta["avoided_allergies"] = all_allergies
        meta["avoided_dislikes"] = all_dislikes
        meta["shared_likes"] = shared_likes

        recent_keys = {
            _normalize_food_key(f).lower()
            for p in profiles
            for f in p.recent_foods(self.avoid_recent_count)
        }

        def _hits_tags(food_key_l: str, tags: list[str]) -> bool:
            for tag in tags:
                t = tag.strip().lower()
                if t and (t in food_key_l or food_key_l in t):
                    return True
            return False

        weighted: list[tuple[str, float]] = []
        for food in foods:
            key = _normalize_food_key(food)
            key_l = key.lower()
            if any(p.is_forbidden(food) for p in profiles):
                continue
            weight = 1.0
            like_hits = 0
            for p in profiles:
                if any(
                    lk.lower() in key_l or key_l in lk.lower()
                    for lk in p.likes
                    if lk
                ):
                    like_hits += 1
            if like_hits:
                weight += 2.0 * like_hits
                if like_hits >= 2:
                    weight += 1.5
            if key_l in recent_keys:
                weight *= 0.15
            weighted.append((food, max(0.05, weight)))

        if not weighted:
            meta["relaxed_dislikes"] = True
            for food in foods:
                key_l = _normalize_food_key(food).lower()
                if not _hits_tags(key_l, all_allergies):
                    weighted.append((food, 1.0))
            if not weighted:
                return None, meta

        total = sum(w for _, w in weighted)
        r = (rng or random).random() * total
        acc = 0.0
        for food, w in weighted:
            acc += w
            if r <= acc:
                return food, meta
        return weighted[-1][0], meta
