"""公平的提问者/被提问者抽取（纯逻辑模块，无 AstrBot 依赖）。

每轮从房间玩家中抽取一对 ``(提问者, 被提问者)``：

- 被提问者按「被问次数」加权：被问得越少越可能被选中（缺失记 0）；
- 提问者可能是玩家，也可能是机器人（哨兵 ``__bot__``）；
- 仅剩 1 名玩家且机器人提问开启时，机器人恒为提问者（跳过概率掷骰，
  避免死锁）；仅 1 名玩家且机器人提问关闭时抛 ``ValueError``
  （开局校验应已在上游拒绝，本类不应被调用）。

本模块为纯逻辑层：不持有房间状态、不修改外部数据；随机源可注入
（``rng`` 参数），测试可用固定种子复现。
"""

from __future__ import annotations

import random

#: 机器人提问者哨兵 ID。永不作为被提问者返回。
BOT_ASKER_ID = "__bot__"

#: 玩家输入：dict 为 ``{user_id: 昵称}``，list 为 ``[user_id, ...]``。
PlayersInput = dict[str, str] | list[str]


class Lottery:
    """每轮抽取 ``(提问者, 被提问者)`` 对。

    Args:
        players: 玩家集合。dict 形式为 ``{user_id: 昵称}``（仅使用其中的
            user_id 作为玩家标识），list 形式为 ``[user_id, ...]``。
        ask_counts: 每位玩家的累计被问次数，缺失视为 0；为 ``None`` 时
            所有玩家等权重（被问次数全为 0）。
        bot_ask_enabled: 是否允许机器人担任提问者。
        bot_ask_frequency: 机器人担任提问者的概率（0.0 ~ 1.0，越界裁剪）。
        rng: 可注入的随机源。为 ``None`` 时创建新的 ``random.Random()``
            实例（独立随机源，不使用全局默认 random）。
    """

    def __init__(
        self,
        players: PlayersInput,
        ask_counts: dict[str, int] | None = None,
        bot_ask_enabled: bool = True,
        bot_ask_frequency: float = 0.5,
        rng: random.Random | None = None,
    ) -> None:
        if isinstance(players, dict):
            # dict 的键是 user_id，值是昵称；抽取只关心 user_id。
            self.player_ids: list[str] = list(players.keys())
            self.nicknames: dict[str, str] = dict(players)
        else:
            self.player_ids = list(players)
            self.nicknames = {}
        # 复制外部数据（浅拷贝），绝不对调用方的房间状态做任何修改。
        self.ask_counts: dict[str, int] = dict(ask_counts or {})
        self.bot_ask_enabled = bool(bot_ask_enabled)
        # 概率裁剪到 [0.0, 1.0]，防御异常配置。
        freq = float(bot_ask_frequency)
        self.bot_ask_frequency = min(1.0, max(0.0, freq))
        self.rng = rng if rng is not None else random.Random()

    def pick_pair(self, prev_askee_id: str | None = None) -> tuple[str, str]:
        """抽取一轮 ``(提问者, 被提问者)``。

        Args:
            prev_askee_id: 上一轮的被提问者 user_id。存在其它可选玩家时，
                本轮不再抽到 TA；仅剩 TA 一个可选时回退到 TA（例如只有
                1 名玩家的房间）。

        Returns:
            ``(asker_id, askee_id)``；``asker_id`` 可能为哨兵
            ``"__bot__"``（机器人提问）。

        Raises:
            ValueError: 玩家列表为空；或仅 1 名玩家且未启用机器人提问
                （上游开局校验应已拒绝，本方法不应被调用）。
        """
        ids = self.player_ids
        if not ids:
            raise ValueError("玩家列表为空，无法抽取提问对")

        # (a) 被提问者：按被问次数加权（被问得越少权重越高，缺失记 0），
        #     排除上一轮被提问者；没有其它可选玩家时回退到全员。
        eligible = [pid for pid in ids if pid != prev_askee_id]
        if not eligible:
            eligible = ids
        askee = self._weighted_pick(eligible)

        # (b) 提问者
        if len(ids) == 1:
            if self.bot_ask_enabled:
                # 仅 1 名玩家：强制机器人提问，跳过概率掷骰，避免死锁
                # （即使 bot_ask_frequency == 0.0 也必须是机器人）。
                return (BOT_ASKER_ID, askee)
            # (c) 仅 1 名玩家且未启用机器人提问：本类不应被调用。
            raise ValueError(
                "仅 1 名玩家且未启用机器人提问，无法抽取提问者"
            )

        if self.bot_ask_enabled and self.rng.random() < self.bot_ask_frequency:
            return (BOT_ASKER_ID, askee)

        # 在排除被提问者的剩余玩家中均匀随机（len(ids) >= 2，必然非空）。
        askers = [pid for pid in ids if pid != askee]
        asker = self.rng.choice(askers)
        return (asker, askee)

    def _weighted_pick(self, candidates: list[str]) -> str:
        """按「被问次数的倒数」加权随机选取一名玩家。

        权重 = 1 / (ask_counts.get(pid, 0) + 1)：被问次数越少，权重越大；
        被问次数为负按 0 处理（防御异常数据）。
        """
        weights = [
            1.0 / (max(0, self.ask_counts.get(pid, 0)) + 1) for pid in candidates
        ]
        return self.rng.choices(candidates, weights=weights, k=1)[0]
