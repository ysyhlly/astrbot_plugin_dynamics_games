"""抽取用例：当天记录优先，未抽过则随机发放。"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import date

from .models import DrawOutcome, Pig
from .repository import PigRepository
from .storage import BoardStore


class RollPigService:
    """「今日小猪」业务用例。

    时间源与随机源均可注入，便于测试每日刷新与抽签逻辑。
    """

    def __init__(
        self,
        store: BoardStore,
        repository: PigRepository,
        *,
        chooser: Callable[[Sequence[Pig]], Pig] | None = None,
        clock: Callable[[], date] | None = None,
    ) -> None:
        self._store = store
        self._repository = repository
        self._chooser = chooser or random.choice
        self._clock = clock or date.today

    def draw(self, user_id: str) -> Pig | None:
        """为用户抽取今日小猪；当天已抽过则直接返回原结果，资源为空时返回 ``None``。"""
        outcome = self.draw_with_social_info(user_id)
        return outcome.pig if outcome is not None else None

    def draw_with_social_info(
        self,
        user_id: str,
        *,
        group_id: str = "",
        user_name: str = "",
    ) -> DrawOutcome | None:
        """抽取今日小猪并返回图鉴收集进度与同群撞猪彩蛋名单。"""
        day = self._clock().isoformat()
        uid = str(user_id or "").strip()
        gid = str(group_id or "").strip()
        uname = str(user_name or "").strip() or uid
        total = len(self._repository.pigs) if self._repository else 0

        def _draw_on_board(board) -> DrawOutcome | None:
            pig = board.get(uid)
            if pig is None:
                if not self._repository:
                    return None
                pig = self._chooser(self._repository.pigs)
                board.put(uid, pig)

            # Personal collection progress
            col = board.collections.setdefault(uid, [])
            is_new = False
            if pig.id and pig.id not in col:
                col.append(pig.id)
                is_new = True

            # Same-group same-day pig collision detection
            peers: list[str] = []
            if gid and pig.id:
                gd = board.group_draws.setdefault(gid, {})
                for other_uid, info in gd.items():
                    if other_uid != uid and str(info.get("pig_id") or "") == pig.id:
                        peers.append(str(info.get("name") or other_uid))
                gd[uid] = {"pig_id": pig.id, "name": uname}

            return DrawOutcome(
                pig=pig,
                is_new_unlock=is_new,
                collected_count=len(col),
                total_count=max(total, len(col)),
                collision_peers=tuple(peers),
            )

        update_fn = getattr(self._store, "update", None)
        if callable(update_fn):
            return update_fn(day, _draw_on_board)

        board = self._store.load(day)
        outcome = _draw_on_board(board)
        if outcome is not None:
            self._store.save(board)
        return outcome
