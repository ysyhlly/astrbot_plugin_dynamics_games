"""领域模型：小猪档案、当日抽取记录与抽取结果。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import messages


@dataclass(frozen=True, slots=True)
class Pig:
    """一只小猪的档案（值对象，与资源文件中的一条记录一一对应）。"""

    id: str
    name: str
    description: str
    analysis: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Pig:
        """由 JSON 记录构造；缺字段时回退到统一占位文案。"""
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", messages.UNKNOWN_NAME)),
            description=str(raw.get("description", messages.UNKNOWN_DESCRIPTION)),
            analysis=str(raw.get("analysis", messages.UNKNOWN_ANALYSIS)),
        )

    def to_mapping(self) -> dict[str, str]:
        """序列化为与资源文件一致的 JSON 记录。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "analysis": self.analysis,
        }


@dataclass(frozen=True, slots=True)
class DrawOutcome:
    """一次抽取的完整结果（含个人图鉴收集进度与同群撞猪彩蛋）。"""

    pig: Pig
    is_new_unlock: bool = False
    collected_count: int = 0
    total_count: int = 0
    collision_peers: tuple[str, ...] = ()


@dataclass(slots=True)
class DailyBoard:
    """某一天的抽取记录（用户 ID → 小猪）及个人图鉴收集/同群撞猪索引。"""

    day: str
    records: dict[str, Pig] = field(default_factory=dict)
    group_draws: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    collections: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def empty(cls, day: str, *, collections: dict[str, list[str]] | None = None) -> DailyBoard:
        """构造某一天的空记录板（可跨日延续个人图鉴收集记录）。"""
        copied = {
            str(uid): list(dict.fromkeys(str(pid) for pid in pids if pid))
            for uid, pids in (collections or {}).items()
            if uid
        }
        return cls(day=day, collections=copied)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], day: str) -> DailyBoard:
        """由持久化结构恢复记录板，跳过结构异常的条目。"""
        records: dict[str, Pig] = {}
        stored = raw.get("records")
        if isinstance(stored, Mapping):
            for user_id, record in stored.items():
                if isinstance(record, Mapping):
                    records[str(user_id)] = Pig.from_mapping(record)
        group_draws: dict[str, dict[str, dict[str, str]]] = {}
        raw_gd = raw.get("group_draws")
        if isinstance(raw_gd, Mapping):
            for gid, users in raw_gd.items():
                if isinstance(users, Mapping):
                    group_draws[str(gid)] = {
                        str(uid): {
                            "pig_id": str(info.get("pig_id") or ""),
                            "name": str(info.get("name") or uid),
                        }
                        for uid, info in users.items()
                        if isinstance(info, Mapping) and info.get("pig_id")
                    }
        collections: dict[str, list[str]] = {}
        raw_col = raw.get("collections")
        if isinstance(raw_col, Mapping):
            for uid, pids in raw_col.items():
                if isinstance(pids, list):
                    collections[str(uid)] = list(
                        dict.fromkeys(str(p) for p in pids if p)
                    )
        return cls(
            day=day,
            records=records,
            group_draws=group_draws,
            collections=collections,
        )

    def to_mapping(self) -> dict[str, Any]:
        """序列化为持久化结构。"""
        out: dict[str, Any] = {
            "date": self.day,
            "records": {user_id: pig.to_mapping() for user_id, pig in self.records.items()},
        }
        if self.group_draws:
            out["group_draws"] = self.group_draws
        if self.collections:
            out["collections"] = self.collections
        return out

    def get(self, user_id: str) -> Pig | None:
        """读取用户当天已抽到的小猪。"""
        return self.records.get(user_id)

    def put(self, user_id: str, pig: Pig) -> None:
        """记录用户当天抽到的小猪。"""
        self.records[user_id] = pig
