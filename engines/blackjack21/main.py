from __future__ import annotations
from ...runtime import Component

import asyncio
import hashlib
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

PLUGIN_VERSION = "1.5.3"

SUITS = ("♠", "♥", "♦", "♣")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
LITTLE_JOKER = "🃏小王"
BIG_JOKER = "🃏大王"
JOKER_CARDS = {LITTLE_JOKER, BIG_JOKER}

RULES_TEXT = """📖 21点规则说明

【牌值】
· J、Q、K（花牌）：都算 10 点
· 数字牌 2-10：按牌面数字算点数
· A：可以算 1 点或 11 点，取对你最有利的数值

【目标】
手牌尽量接近 21 点，但不能超过 21 点；超过就爆牌，直接输掉本局。

【流程】
1. /创建21点 — 开房间（发起者=管理员）
2. /加入21点 — 其他人加入
3. /开始21点 — 管理员开局（至少2人）
4. /要牌 或 /停牌 — 同步回合操作
5. /21点状态 — 查看房间
6. /21点帮助 — 查看本规则说明
7. /结束21点 — 管理员结束房间

【结算】
未爆牌且点数最高为本局第一（可并列）免禁言；
其他人禁言秒数 = |与第一名点数差| × 配置值。

发送 /21点帮助 可再次查看本说明。"""

JOKER_RULES_BODY = """· 牌堆加入 🃏小王 / 🃏大王（不计点数）
· 抽牌按权重：普通牌权重恒为 1；小丑牌权重 = 当前轮数（开局第 0 轮权重为 0，必不抽到）
· 每一轮开始后，要牌时小丑出现概率随轮数升高
· 要牌摸到小王：移除除自己外每人手中点数最小的一张牌，并解除全员停牌
· 要牌摸到大王：移除除自己外每人手中点数最大的一张牌，并解除全员停牌
· 被移除的牌进入弃牌堆，本局内不会再抽到
· 停牌被解除后，可再次 /要牌"""


@dataclass
class Player:
    """One participant in a 21-point room."""

    user_id: str
    is_host: bool = False
    cards: list[str] = field(default_factory=list)
    stood: bool = False

    @property
    def busted(self) -> bool:
        return hand_value(self.cards) > 21

    @property
    def done(self) -> bool:
        """Permanently finished: stood or over 21."""
        return self.stood or self.busted


@dataclass
class Room:
    """Per-group 21-point room state."""

    group_id: str
    host_id: str
    phase: str = "waiting"  # waiting | playing | finished
    players: dict[str, Player] = field(default_factory=dict)
    deck: list[str] = field(default_factory=list)
    # Cards removed by joker effects; excluded from future draws/reshuffles.
    discard: list[str] = field(default_factory=list)
    round_no: int = 0
    # user_id -> "hit" | "stand" for the current round only
    round_actions: dict[str, str] = field(default_factory=dict)
    bot: Any = None
    reply_event: Any = None
    joker_mode: bool = False
    started_at_ns: int = 0
    # Extra salt mixed into reshuffles within the same game.
    shuffle_seq: int = 0


def is_joker(card: str) -> bool:
    """Return whether the card is a little/big joker."""
    return card in JOKER_CARDS


def build_deck(
    *,
    joker_mode: bool = False,
    entropy: str = "",
) -> list[str]:
    """Build and shuffle a deck with per-game entropy.

    Uses ``SystemRandom`` (OS entropy) plus a seed derived from session salt
    (group / player ids / start time) so consecutive games do not share a
    predictable shuffle pattern. Not a fixed global seed.

    Args:
        joker_mode: When True, append little/big jokers.
        entropy: Session salt, e.g. ``group:players:start_ns:seq``.

    Returns:
        Shuffled deck list.
    """
    deck = [f"{suit}{rank}" for suit in SUITS for rank in RANKS]
    if joker_mode:
        deck.extend([LITTLE_JOKER, BIG_JOKER])

    # Mix OS CSPRNG with game-specific salt (player ids + start time + seq).
    salt = hashlib.sha256(
        f"{entropy}|{secrets.token_hex(16)}|{time.time_ns()}".encode()
    ).digest()
    seed = int.from_bytes(salt[:16], "big") ^ secrets.randbits(128)
    rng = random.Random(seed)
    rng.shuffle(deck)
    # Second pass with SystemRandom to further scramble order.
    random.SystemRandom().shuffle(deck)
    return deck


def card_points(card: str) -> int:
    """Point value of one card (Ace counted as 11 here; jokers are 0)."""
    if is_joker(card):
        return 0
    rank = card[1:]
    if rank in {"J", "Q", "K"}:
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_value(cards: list[str]) -> int:
    """Best 21-point total for a hand (Ace 1 or 11; jokers ignored)."""
    real = [c for c in cards if not is_joker(c)]
    total = sum(card_points(c) for c in real)
    aces = sum(1 for c in real if c.endswith("A"))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_cards(cards: list[str]) -> str:
    """Human-readable card list."""
    return " ".join(cards) if cards else "（暂无）"


def extreme_card_index(cards: list[str], *, largest: bool) -> int | None:
    """Index of the lowest or highest point card; prefer normal cards over jokers.

    Args:
        cards: Current hand.
        largest: When True pick highest points; otherwise lowest.

    Returns:
        Index to remove, or None when the hand is empty.
    """
    if not cards:
        return None
    real_idxs = [i for i, c in enumerate(cards) if not is_joker(c)]
    pool = real_idxs if real_idxs else list(range(len(cards)))
    if largest:
        return max(pool, key=lambda i: (card_points(cards[i]), i))
    return min(pool, key=lambda i: (card_points(cards[i]), i))


class Blackjack21Plugin(Component):
    """群聊 21 点小游戏（同步回合制）。

    每个群独立一间房（按 group_id 隔离）。
    等人阶段超时未开局自动关房；对局中每轮限时，超时未操作视为停手。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        # group_id -> Room：一群一桌，互不影响
        self.rooms: dict[str, Room] = {}
        self.round_tasks: dict[str, asyncio.Task] = {}
        self.waiting_tasks: dict[str, asyncio.Task] = {}

    async def terminate(self):
        """Cancel pending timers on unload."""
        for gid in list(self.round_tasks):
            self._cancel_round_timer(gid)
        for gid in list(self.waiting_tasks):
            self._cancel_waiting_timer(gid)

    def _mute_per_point(self) -> int:
        return max(1, int(self.config.get("mute_per_point", 10)))

    def _max_players(self) -> int:
        return max(2, int(self.config.get("max_players", 6)))

    def _max_mute(self) -> int:
        return max(10, int(self.config.get("max_mute_seconds", 600)))

    def _round_timeout(self) -> int:
        return max(5, int(self.config.get("round_timeout_seconds", 60)))

    def _waiting_timeout(self) -> int:
        """Seconds to auto-close a room still waiting to start."""
        return max(60, int(self.config.get("waiting_timeout_seconds", 600)))

    def _joker_mode(self) -> bool:
        """Whether joker cards are enabled for new games."""
        raw = self.config.get("joker_mode", False)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    def _joker_rules_section(self, *, enabled: bool | None = None) -> str:
        """Build the joker-mode section for rule help text.

        When joker mode is on, include the full effect list. When off, only note
        that it can be enabled in plugin config.
        """
        on = self._joker_mode() if enabled is None else enabled
        if on:
            return f"\n\n【小丑模式】当前已启用\n{JOKER_RULES_BODY}"
        return "\n\n【小丑模式】当前未启用（可在插件配置中打开 joker_mode）。"

    def _help_text(self) -> str:
        """Full rules text; always includes joker-mode section with status."""
        return RULES_TEXT + self._joker_rules_section()

    def _result(self, *parts: Any) -> MessageEventResult:
        """Build a message chain; Player segments become @mentions.

        AstrBot respond stage drops Plain segments whose text is only
        whitespace, so bare ``\\n`` would vanish. Keep newlines by appending
        a zero-width space when needed, and always put a normal space after At.
        """
        flat: list[Any] = []

        def _flatten(part: Any) -> None:
            if part is None or part == "":
                return
            if isinstance(part, list):
                for item in part:
                    _flatten(item)
                return
            if isinstance(part, Player):
                flat.append(part)
                return
            text = str(part)
            if flat and isinstance(flat[-1], str):
                flat[-1] += text
            else:
                flat.append(text)

        for p in parts:
            _flatten(p)

        chain: list = []
        for item in flat:
            if isinstance(item, Player):
                chain.append(At(qq=item.user_id))
                continue
            text = item
            if chain and isinstance(chain[-1], At):
                if not text.startswith((" ", "\u3000")):
                    text = " " + text
            # 纯空白（含单独换行）会被管线 strip 掉，补零宽字符保住换行
            if text and not text.strip():
                text = text + "\u200b"
            if text:
                chain.append(Plain(text))
        result = MessageEventResult(chain=chain)
        # qq_official only resolves <@openid> in plain text, not markdown.
        result.use_markdown_ = False
        return result

    def _chain_to_onebot(self, result: MessageEventResult) -> list[dict]:
        """Convert result chain to OneBot message segments (fallback)."""
        msg: list[dict] = []
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                text = comp.text.replace("\u200b", "")
                if text:
                    msg.append({"type": "text", "data": {"text": text}})
            elif isinstance(comp, At) and comp.qq:
                msg.append({"type": "at", "data": {"qq": str(comp.qq)}})
        return msg

    def _bind_event(self, room: Room, event: AstrMessageEvent) -> None:
        """Keep the latest event so timeout broadcasts can reply on any platform.

        Args:
            room: Current 21-point room.
            event: Incoming group message event.
        """
        room.reply_event = event
        room.bot = getattr(event, "bot", None) or room.bot

    async def _send_group(self, room: Room, result: MessageEventResult) -> None:
        """Send a built result to the group via stored event or OneBot fallback.

        Args:
            room: Room whose last event / bot is used to send.
            result: Prebuilt message chain to deliver.
        """
        event = room.reply_event
        if event is not None:
            try:
                await event.send(result)
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("21点超时播报失败: %s", exc, exc_info=True)

        bot = room.bot
        gid = str(room.group_id or "")
        if bot is None or not gid.isdigit():
            logger.error("21点超时播报失败：无可用发送通道 group=%s", room.group_id)
            return
        try:
            await AiocqhttpMessageEvent.send_message(
                bot=bot,
                message_chain=result,
                is_group=True,
                session_id=gid,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("21点官方发送失败，尝试兜底: %s", exc, exc_info=True)
            try:
                await bot.send_group_msg(
                    group_id=int(gid),
                    message=self._chain_to_onebot(result),
                )
            except Exception as exc2:  # noqa: BLE001
                logger.error("21点群消息兜底发送失败: %s", exc2, exc_info=True)

    def _active_players(self, room: Room) -> list[Player]:
        return [p for p in room.players.values() if not p.done]

    def _waiting_players(self, room: Room) -> list[Player]:
        return [
            p for p in self._active_players(room) if p.user_id not in room.round_actions
        ]

    def _cancel_round_timer(self, group_id: str) -> None:
        """Cancel pending round timer.

        If called from inside the timer task itself, only detach it from the
        map — cancelling the running task would abort settle/ban/send.
        """
        task = self.round_tasks.pop(group_id, None)
        if not task or task.done():
            return
        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:
            pass
        task.cancel()

    def _cancel_waiting_timer(self, group_id: str) -> None:
        task = self.waiting_tasks.pop(group_id, None)
        if not task or task.done():
            return
        try:
            if task is asyncio.current_task():
                return
        except RuntimeError:
            pass
        task.cancel()

    def _arm_waiting_timer(self, room: Room) -> None:
        """Auto-close room if still waiting after timeout."""
        self._cancel_waiting_timer(room.group_id)
        group_id = room.group_id
        timeout = self._waiting_timeout()

        async def _on_waiting_timeout() -> None:
            try:
                await asyncio.sleep(timeout)
            except asyncio.CancelledError:
                return
            room_now = self.rooms.get(group_id)
            if not room_now or room_now.phase != "waiting":
                return
            self.rooms.pop(group_id, None)
            self.waiting_tasks.pop(group_id, None)
            result = self._result(
                f"⏰ 21点房间创建后 {timeout // 60} 分钟仍未开始，已自动关闭。"
            )
            await self._send_group(room_now, result)

        self.waiting_tasks[group_id] = asyncio.create_task(_on_waiting_timeout())

    def _arm_round_timer(self, room: Room) -> None:
        """Start / restart the per-round timeout for this room."""
        self._cancel_round_timer(room.group_id)
        group_id = room.group_id
        round_no = room.round_no
        timeout = self._round_timeout()
        bot = room.bot

        async def _on_timeout() -> None:
            try:
                await asyncio.sleep(timeout)
                room_now = self.rooms.get(group_id)
                if room_now is not None and bot is not None and room_now.bot is None:
                    room_now.bot = bot
                await self._handle_round_timeout(group_id, round_no)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("21点回合超时处理失败: %s", exc, exc_info=True)

        self.round_tasks[group_id] = asyncio.create_task(_on_timeout())

    def _begin_round(self, room: Room, bot: Any | None = None) -> None:
        room.round_no += 1
        room.round_actions = {}
        if bot is not None:
            room.bot = bot
        self._arm_round_timer(room)

    def _join_ats(self, players: list[Player], sep: str = " ") -> list[Any]:
        """Join players as @ mentions with separators."""
        if not players:
            return ["无"]
        out: list[Any] = []
        for i, p in enumerate(players):
            if i:
                out.append(sep)
            out.append(p)
        return out

    def _player_status(self, room: Room, player: Player) -> str:
        if player.busted:
            return "超过21"
        if player.stood:
            return "已停手"
        if player.user_id in room.round_actions:
            choice = "要牌" if room.round_actions[player.user_id] == "hit" else "停手"
            return f"本轮已选·{choice}"
        return "本轮未选"

    def _room_parts(self, room: Room, *, settle: bool = False) -> list[Any]:
        """Room status segments with @ mentions (no role tags).

        Args:
            room: Current room.
            settle: When True, omit "待操作" and use a compact结算 header.
        """
        if settle:
            phase = "结算"
        elif room.phase == "waiting":
            phase = "等人中"
        elif room.phase == "playing":
            phase = f"第{room.round_no}轮·限时{self._round_timeout()}秒"
        else:
            phase = "已结束"
        if room.joker_mode:
            phase += "·小丑模式"

        parts: list[Any] = [f"🃏 21点（{phase}）\n"]
        if room.phase == "playing" and not settle:
            waiting = self._waiting_players(room)
            if waiting:
                parts.append("本轮待操作：")
                parts.extend(self._join_ats(waiting))
                parts.append("\n")

        players = list(room.players.values())
        last_i = len(players) - 1
        for i, p in enumerate(players):
            parts.append(p)
            if room.phase == "waiting" and not settle:
                if i < last_i:
                    parts.append("\n")
                continue
            if settle:
                if p.busted:
                    status = "超过21"
                elif p.stood:
                    status = "已停手"
                else:
                    status = "结束"
            else:
                status = self._player_status(room, p)
            # @昵称：\n牌面=点数（状态） — 分行更易读
            line = f"：\n{format_cards(p.cards)}={hand_value(p.cards)}（{status}）"
            if i < last_i:
                line += "\n"
            parts.append(line)
        return parts

    def _deck_entropy(self, room: Room) -> str:
        """Build shuffle salt from group, players, start time, and reshuffle seq."""
        ids = ",".join(sorted(room.players.keys()))
        return f"{room.group_id}:{ids}:{room.started_at_ns}:{room.shuffle_seq}"

    def _new_deck(self, room: Room) -> list[str]:
        """Create a shuffled deck excluding held cards and the discard pile.

        Discarded joker-strip cards stay out for the rest of the game, so they
        cannot be drawn again even when the draw pile is rebuilt.
        """
        room.shuffle_seq += 1
        deck = build_deck(
            joker_mode=room.joker_mode,
            entropy=self._deck_entropy(room),
        )
        held: list[str] = []
        for p in room.players.values():
            held.extend(p.cards)
        exclude = set(held) | set(room.discard)
        return [c for c in deck if c not in exclude]

    async def _ban(self, room: Room, user_id: str, seconds: int) -> bool:
        """Mute one user via OneBot ``set_group_ban``.

        Args:
            room: Current room (provides bot and group id).
            user_id: QQ number.
            seconds: Mute duration in seconds.

        Returns:
            True if the mute API call succeeded.
        """
        bot = room.bot
        gid = str(room.group_id or "")
        uid = str(user_id or "")
        seconds = max(0, int(seconds))
        if seconds <= 0:
            return True
        if bot is None or not gid or not uid:
            return False
        if not (gid.isdigit() and uid.isdigit() and hasattr(bot, "set_group_ban")):
            return False
        try:
            await bot.set_group_ban(
                group_id=int(gid),
                user_id=int(uid),
                duration=seconds,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("21点禁言失败 user=%s err=%s", user_id, exc)
            return False

    def _card_weight(self, room: Room, card: str) -> int:
        """Draw weight for one remaining card.

        Normal cards always weigh 1. Jokers weigh ``round_no`` (0 during the
        opening deal, so they cannot appear until round 1+).
        """
        if is_joker(card):
            return max(0, int(room.round_no))
        return 1

    def _deal_one(self, room: Room, player: Player) -> str | None:
        """Deal one card by weight; returns None when nothing drawable remains."""
        if not room.deck:
            room.deck = self._new_deck(room)
        if not room.deck:
            return None

        weighted = [(card, self._card_weight(room, card)) for card in room.deck]
        choices = [(card, w) for card, w in weighted if w > 0]
        if not choices:
            return None

        cards = [c for c, _ in choices]
        weights = [w for _, w in choices]
        card = random.SystemRandom().choices(cards, weights=weights, k=1)[0]
        room.deck.remove(card)
        player.cards.append(card)
        return card

    def _apply_joker_strip(
        self,
        room: Room,
        drawer: Player,
        parts: list[Any],
        *,
        largest: bool,
    ) -> None:
        """Strip one card from each other player into the discard pile."""
        name = "大王" if largest else "小王"
        kind = "最大" if largest else "最小"
        parts.extend(["💥 ", drawer, f" 摸到{name}！移除其他人{kind}的一张牌：\n"])
        removed_any = False
        for other in room.players.values():
            if other.user_id == drawer.user_id:
                continue
            idx = extreme_card_index(other.cards, largest=largest)
            if idx is None:
                parts.extend(["· ", other, " 没有牌可移除\n"])
                continue
            removed = other.cards.pop(idx)
            room.discard.append(removed)
            removed_any = True
            parts.extend(
                [
                    "· ",
                    other,
                    f" 失去 {removed}（进弃牌堆） → {format_cards(other.cards)}"
                    f"={hand_value(other.cards)}\n",
                ]
            )
        if not removed_any:
            parts.append("（无人被移除手牌）\n")
        else:
            parts.append(f"🗑 弃牌堆 {len(room.discard)} 张\n")

    def _clear_all_stands(self, room: Room, parts: list[Any]) -> None:
        """Clear stand flags so players may hit again after a joker effect."""
        revived = [p for p in room.players.values() if p.stood]
        for p in room.players.values():
            p.stood = False
        if revived:
            parts.append("🔓 全员停牌已解除，可继续要牌：")
            parts.extend(self._join_ats(revived))
            parts.append("\n")
        else:
            parts.append("🔓 全员可继续要牌（无人处于停牌）\n")

    def _pick_winners(self, players: list[Player]) -> list[Player]:
        alive = [p for p in players if not p.busted]
        if alive:
            best = max(hand_value(p.cards) for p in alive)
            return [p for p in alive if hand_value(p.cards) == best]
        best = min(hand_value(p.cards) for p in players)
        return [p for p in players if hand_value(p.cards) == best]

    async def _resolve_round(self, room: Room) -> list[Any]:
        """Apply all choices for the current round."""
        self._cancel_round_timer(room.group_id)
        parts: list[Any] = [f"—— 第{room.round_no}轮结算 ——\n"]
        joker_drawn = False
        for uid, action in list(room.round_actions.items()):
            player = room.players.get(uid)
            if not player or player.done:
                continue
            if action == "stand":
                player.stood = True
                parts.extend(
                    [
                        "✋ ",
                        player,
                        f" 停手：{format_cards(player.cards)}={hand_value(player.cards)}\n",
                    ]
                )
                continue

            card = self._deal_one(room, player)
            if card is None:
                parts.extend(
                    [
                        "🃏 ",
                        player,
                        f" 要牌失败：牌堆已空（弃牌堆 {len(room.discard)} 张）\n",
                    ]
                )
                continue
            parts.extend(
                [
                    "🃏 ",
                    player,
                    f" 要到 {card} → {format_cards(player.cards)}"
                    f"={hand_value(player.cards)}\n",
                ]
            )

            if room.joker_mode and card == LITTLE_JOKER:
                self._apply_joker_strip(room, player, parts, largest=False)
                joker_drawn = True
                continue

            if room.joker_mode and card == BIG_JOKER:
                self._apply_joker_strip(room, player, parts, largest=True)
                joker_drawn = True
                continue

            if player.busted:
                parts.extend(["💥 ", player, " 超过21了！\n"])

        room.round_actions = {}
        if joker_drawn:
            # After any stands in this same resolve, still unlock everyone.
            self._clear_all_stands(room, parts)
        return parts

    async def _settle(self, room: Room) -> list[Any]:
        """Settle scores and mute non-first players."""
        self._cancel_round_timer(room.group_id)
        players = list(room.players.values())
        winners = self._pick_winners(players)
        winner_ids = {p.user_id for p in winners}
        winner_score = hand_value(winners[0].cards)
        per = self._mute_per_point()
        cap = self._max_mute()

        parts: list[Any] = ["🏁 21点本局结束！\n"]
        parts.extend(self._room_parts(room, settle=True))
        parts.append("\n本局第一：")
        for i, p in enumerate(winners):
            if i:
                parts.append("、")
            parts.extend([p, f"({hand_value(p.cards)}点)"])
        parts.append(" —— 不用禁言\n")

        for p in players:
            if p.user_id in winner_ids:
                continue
            score = hand_value(p.cards)
            diff = abs(winner_score - score)
            seconds = min(cap, max(per, diff * per))
            ok = await self._ban(room, p.user_id, seconds)
            parts.extend(
                [
                    p,
                    f"：{score}点，与第一相差{diff}点 → 禁言{seconds}秒"
                    + (" ✅\n" if ok else " ❌(权限不足)\n"),
                ]
            )

        room.phase = "finished"
        self._cancel_waiting_timer(room.group_id)
        self.rooms.pop(room.group_id, None)
        cb = getattr(self, 'outcome_callback', None)
        if callable(cb) and players:
            try:
                await cb(
                    event=room.reply_event,
                    group_id=str(room.group_id),
                    game_key='blackjack21',
                    title='21点对局',
                    rounds=max(1, int(room.round_no)),
                    participants=[{'uid': str(p.user_id), 'name': str(p.user_id), 'role_weight': 3 if p.user_id in winner_ids else 1} for p in players],
                    summary=f"21点对局结算（共 {max(1, int(room.round_no))} 轮，{len(players)} 人参与），本局第一点数 {winner_score} 点。",
                )
            except Exception as exc:
                logger.warning('21点结果同步失败: %s', exc)
        return parts

    async def _advance_after_full_round(
        self, room: Room, head: list[Any]
    ) -> MessageEventResult:
        """Resolve current round after everyone has chosen (or timed out)."""
        parts: list[Any] = list(head)
        parts.extend(await self._resolve_round(room))

        if not self._active_players(room):
            parts.append("\n")
            parts.extend(await self._settle(room))
            return self._result(*parts)

        self._begin_round(room)
        parts.extend(
            [
                f"\n➡️ 第{room.round_no}轮（限时 {self._round_timeout()} 秒）"
                " /要牌 或 /停牌\n",
            ]
        )
        parts.extend(self._room_parts(room))
        return self._result(*parts)

    async def _after_choice(self, room: Room, head: list[Any]) -> MessageEventResult:
        """Handle post-choice: wait for others, or resolve round / settle."""
        # Last player to choose: skip the pre-resolve room snapshot (too noisy).
        if not self._waiting_players(room):
            parts: list[Any] = list(head)
            parts.append("\n")
            return await self._advance_after_full_round(room, parts)

        parts = list(head)
        parts.append("\n")
        parts.extend(self._room_parts(room))
        parts.append("⏳ 还在等：")
        parts.extend(self._join_ats(self._waiting_players(room)))
        return self._result(*parts)

    async def _handle_round_timeout(self, group_id: str, round_no: int) -> None:
        """Auto-stand waiting players and resolve the round."""
        # Detach this timer from the map first (do not cancel self).
        self.round_tasks.pop(group_id, None)

        room = self.rooms.get(group_id)
        if not room or room.phase != "playing" or room.round_no != round_no:
            logger.info(
                "21点超时忽略：group=%s round=%s phase=%s",
                group_id,
                round_no,
                getattr(room, "phase", None),
            )
            return
        waiting = self._waiting_players(room)
        if not waiting:
            logger.info("21点超时但无人待操作 group=%s round=%s", group_id, round_no)
            return

        logger.info(
            "21点回合超时自动停手 group=%s round=%s users=%s",
            group_id,
            round_no,
            [p.user_id for p in waiting],
        )
        for p in waiting:
            room.round_actions[p.user_id] = "stand"

        head: list[Any] = ["⏰ 本轮超时，未操作视为停手："]
        head.extend(self._join_ats(waiting))
        head.append("\n")
        result = await self._advance_after_full_round(room, head)
        await self._send_group(room, result)

    async def cmd_help(self, event: AstrMessageEvent):
        """Show blackjack rules and command list."""
        yield event.plain_result(self._help_text())

    async def cmd_open(self, event: AstrMessageEvent):
        """Open a waiting room; sender becomes room admin."""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("21点只能在群里玩。")
            return

        existing = self.rooms.get(group_id)
        if existing and existing.phase != "finished":
            yield event.plain_result(
                "本群已有进行中的21点，先打完或管理员发 /结束21点。"
            )
            return

        uid = str(event.get_sender_id())
        room = Room(
            group_id=group_id,
            host_id=uid,
            joker_mode=self._joker_mode(),
        )
        self._bind_event(room, event)
        room.players[uid] = Player(user_id=uid, is_host=True)
        self.rooms[group_id] = room
        self._arm_waiting_timer(room)
        wait_min = self._waiting_timeout() // 60
        joker_tip = ""
        if room.joker_mode:
            joker_tip = " ·小丑模式已开启"
        yield self._result(
            "🃏 ",
            room.players[uid],
            " 创建了21点房间，并成为管理员！\n"
            "其他人发 /加入21点\n"
            "人齐后管理员发 /开始21点\n"
            f"规则说明：/21点帮助{joker_tip}\n"
            f"（{wait_min} 分钟内未开始将自动关闭）\n",
            *self._room_parts(room),
        )

    async def cmd_join(self, event: AstrMessageEvent):
        """Join the waiting room."""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("21点只能在群里玩。")
            return

        room = self.rooms.get(group_id)
        if not room or room.phase != "waiting":
            yield event.plain_result("当前没有可加入的21点房间，先 /创建21点。")
            return

        uid = str(event.get_sender_id())
        if uid in room.players:
            yield event.plain_result("你已经在房间里了。")
            return
        if len(room.players) >= self._max_players():
            yield event.plain_result(f"满员了（最多 {self._max_players()} 人）。")
            return

        room.players[uid] = Player(user_id=uid)
        self._bind_event(room, event)
        yield self._result(
            "✅ ",
            room.players[uid],
            " 已加入！\n",
            *self._room_parts(room),
        )

    async def cmd_deal(self, event: AstrMessageEvent):
        """Room admin starts the game and opens round 1."""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("21点只能在群里玩。")
            return

        room = self.rooms.get(group_id)
        if not room or room.phase != "waiting":
            yield event.plain_result("没有等待中的21点房间，先 /创建21点。")
            return

        uid = str(event.get_sender_id())
        if uid != room.host_id:
            yield event.plain_result("只有本局管理员能 /开始21点。")
            return
        if len(room.players) < 2:
            yield event.plain_result("至少还要再来 1 人 /加入21点 才能开始。")
            return

        self._bind_event(room, event)
        room.joker_mode = self._joker_mode()
        room.started_at_ns = time.time_ns()
        room.shuffle_seq = 0
        room.discard = []
        room.deck = self._new_deck(room)
        room.phase = "playing"
        self._cancel_waiting_timer(group_id)
        for _ in range(2):
            for p in room.players.values():
                self._deal_one(room, p)

        for p in room.players.values():
            if hand_value(p.cards) == 21:
                p.stood = True

        if not self._active_players(room):
            yield self._result(
                "🃏 开局后无人可继续操作。\n\n",
                *(await self._settle(room)),
            )
            return

        self._begin_round(room, bot=room.bot)
        joker_line = (
            "小丑模式已开启（完整规则见 /21点帮助）。\n" if room.joker_mode else ""
        )
        yield self._result(
            "🃏 开局！同步回合制：本轮所有未停手的人都要选一次，"
            "全员选完后才统一要牌并进入下一轮。\n"
            f"每轮限时 {self._round_timeout()} 秒，超时未操作视为停手。\n"
            "已停手的人不能再要牌。\n"
            f"与第一相差 1 点禁言 {self._mute_per_point()} 秒。\n"
            f"{joker_line}"
            "规则：/21点帮助\n"
            "请发 /要牌 或 /停牌\n",
            *self._room_parts(room),
        )

    async def cmd_hit(self, event: AstrMessageEvent):
        """Choose to draw one card this round (resolved when all have chosen)."""
        group_id = event.get_group_id()
        room = self.rooms.get(group_id or "")
        if not room or room.phase != "playing":
            yield event.plain_result("现在没有进行中的21点。")
            return

        self._bind_event(room, event)
        uid = str(event.get_sender_id())
        player = room.players.get(uid)
        if not player:
            yield event.plain_result("你不在这个房间。")
            return
        if player.stood:
            yield event.plain_result("你已经停手了，不能再要牌。")
            return
        if player.busted:
            yield event.plain_result("你已经超过21，不能再要牌。")
            return
        if uid in room.round_actions:
            choice = "要牌" if room.round_actions[uid] == "hit" else "停手"
            yield event.plain_result(f"你本轮已经选过了（{choice}），等其他人选完。")
            return

        room.round_actions[uid] = "hit"
        yield await self._after_choice(
            room,
            ["📝 ", player, " 本轮选择：要牌（等全员选完再发）"],
        )

    async def cmd_stand(self, event: AstrMessageEvent):
        """Choose to stand this round (permanent once the round resolves)."""
        group_id = event.get_group_id()
        room = self.rooms.get(group_id or "")
        if not room or room.phase != "playing":
            yield event.plain_result("现在没有进行中的21点。")
            return

        self._bind_event(room, event)
        uid = str(event.get_sender_id())
        player = room.players.get(uid)
        if not player:
            yield event.plain_result("你不在这个房间。")
            return
        if player.done:
            yield event.plain_result("你已经停手或超过21了。")
            return
        if uid in room.round_actions:
            choice = "要牌" if room.round_actions[uid] == "hit" else "停手"
            yield event.plain_result(f"你本轮已经选过了（{choice}），等其他人选完。")
            return

        room.round_actions[uid] = "stand"
        yield await self._after_choice(
            room,
            ["📝 ", player, " 本轮选择：停手（确认后本局不能再要）"],
        )

    async def cmd_status(self, event: AstrMessageEvent):
        """Show current room."""
        group_id = event.get_group_id()
        room = self.rooms.get(group_id or "")
        if not room:
            yield event.plain_result("本群没有21点房间。发 /创建21点 开一局。")
            return
        self._bind_event(room, event)
        yield self._result(*self._room_parts(room))

    async def cmd_end(self, event: AstrMessageEvent):
        """Room admin cancels the room."""
        group_id = event.get_group_id()
        room = self.rooms.get(group_id or "")
        if not room:
            yield event.plain_result("本群没有21点房间。")
            return
        uid = str(event.get_sender_id())
        if uid != room.host_id and not event.is_admin():
            yield event.plain_result("只有本局管理员（或机器人管理员）能结束房间。")
            return
        self._cancel_round_timer(group_id)
        self._cancel_waiting_timer(group_id)
        self.rooms.pop(group_id, None)
        yield event.plain_result("21点房间已结束。")
