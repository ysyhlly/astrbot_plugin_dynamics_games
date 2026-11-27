"""Multimodal icebreaker engine: AI Telephone (AI 传话筒) & Pictionary (你画我猜).

Note: Strictly follows the Dynamics Games engine architecture — zero @register/@filter
decorators here; all commands & passive listeners are registered in host main.py.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ...bridges.visual_bridge import generate_artwork_via_visual_media
from ...core.poster import (
    render_pictionary_fallback_card,
    render_telephone_step_card,
    render_telephone_strip,
)
from ...runtime import Component

PICTIONARY_WORD_BANK: tuple[dict[str, Any], ...] = (
    {"word": "火锅", "aliases": ("涮火锅", "麻辣火锅"), "category": "美食", "hint": "热气腾腾围炉涮菜的聚餐神器", "prompt": "a steaming hotpot table with red broth, chopsticks and fresh vegetables, stylized pictionary illustration, no text"},
    {"word": "珍珠奶茶", "aliases": ("奶茶", "波霸奶茶"), "category": "美食", "hint": "透明杯子里装着黑色Q弹小圆球和吸管", "prompt": "a cute iced bubble tea cup with dark tapioca pearls and a wide straw, stylized pictionary drawing, no text"},
    {"word": "小笼包", "aliases": ("包子", "汤包"), "category": "美食", "hint": "竹蒸笼里皮薄多汁、顶上有褶子的小点心", "prompt": "steaming bamboo basket of pleated soup dumplings, stylized clean pictionary illustration, no text"},
    {"word": "草莓蛋糕", "aliases": ("蛋糕", "奶油蛋糕"), "category": "美食", "hint": "白奶油上点缀红彤彤水果的甜点", "prompt": "a slice of fluffy white cream shortcake topped with bright red strawberries, stylized pictionary drawing, no text"},
    {"word": "烤串", "aliases": ("烧烤", "羊肉串"), "category": "美食", "hint": "夜宵摊竹签穿起来滋滋冒烟", "prompt": "charcoal grilled meat skewers over glowing embers with sparks, stylized pictionary illustration, no text"},
    {"word": "寿司", "aliases": ("三文鱼寿司", "握寿司"), "category": "美食", "hint": "海苔米饭配鲜鱼片的日式料理", "prompt": "colorful nigiri and maki sushi pieces arranged on a wooden board, stylized pictionary drawing, no text"},
    {"word": "熊猫", "aliases": ("大熊猫",), "category": "动物", "hint": "黑白配色、爱啃竹子的国宝", "prompt": "a chubby black and white giant panda sitting and munching green bamboo, stylized pictionary drawing, no text"},
    {"word": "柯基", "aliases": ("柯基犬", "小短腿"), "category": "动物", "hint": "腿短短、耳朵大、屁股圆圆的狗狗", "prompt": "a happy short-legged corgi dog with big ears and fluffy butt wagging, stylized pictionary illustration, no text"},
    {"word": "企鹅", "aliases": ("小企鹅",), "category": "动物", "hint": "住在冰雪世界、穿燕尾服摇摇摆摆的鸟", "prompt": "a cute tuxedo penguin waddling on blue icebergs under aurora, stylized pictionary illustration, no text"},
    {"word": "橘猫", "aliases": ("大橘", "肥猫"), "category": "动物", "hint": "十只里有九只圆滚滚的橙色猫咪", "prompt": "a plump ginger tabby cat napping lazily on a sunny windowsill, stylized pictionary drawing, no text"},
    {"word": "章鱼", "aliases": ("八爪鱼",), "category": "动物", "hint": "长着八条带吸盘触手的海洋生物", "prompt": "a playful purple octopus waving eight curly tentacles underwater, stylized pictionary drawing, no text"},
    {"word": "独角兽", "aliases": ("独角马",), "category": "奇幻", "hint": "额头长着螺旋尖角的彩虹骏马", "prompt": "a magical white unicorn with a golden spiral horn leaping over a rainbow, stylized pictionary illustration, no text"},
    {"word": "宇航员", "aliases": ("太空人", "航天员"), "category": "科幻", "hint": "穿着白色头盔服在失重星空漂浮", "prompt": "an astronaut in a white spacesuit floating in starry space near a ringed planet, stylized pictionary illustration, no text"},
    {"word": "飞碟", "aliases": ("UFO", "外星飞船"), "category": "科幻", "hint": "夜空中发射绿光的圆盘形飞行物", "prompt": "a silver saucer spaceship beaming a glowing light beam over a quiet meadow at night, stylized pictionary drawing, no text"},
    {"word": "机器人", "aliases": ("铁皮机器人",), "category": "科幻", "hint": "头顶天线、金属身躯的机械伙伴", "prompt": "a friendly retro boxy robot with glowing eyes and an antenna waving hello, stylized pictionary illustration, no text"},
    {"word": "摩天轮", "aliases": ("观览车",), "category": "地标", "hint": "游乐园里缓缓旋转的巨大圆环座舱", "prompt": "a giant glowing ferris wheel at sunset in an amusement park, stylized pictionary illustration, no text"},
    {"word": "灯塔", "aliases": ("海边灯塔",), "category": "地标", "hint": "海岸礁石上为夜航船只指路的红白高塔", "prompt": "a red and white striped lighthouse on a cliff shining a bright beam over night waves, stylized pictionary drawing, no text"},
    {"word": "热气球", "aliases": ("气球",), "category": "旅行", "hint": "吊着藤篮、靠火焰升空的彩色大球", "prompt": "a colorful striped hot air balloon floating above soft clouds at sunrise, stylized pictionary illustration, no text"},
    {"word": "露营", "aliases": ("搭帐篷", "野营"), "category": "旅行", "hint": "星空下支起三角形帐篷和篝火", "prompt": "a cozy triangular tent beside a crackling campfire under a starry forest sky, stylized pictionary illustration, no text"},
    {"word": "耳机", "aliases": ("头戴式耳机",), "category": "日常", "hint": "戴在耳朵上沉浸听歌的装备", "prompt": "sleek over-ear headphones floating with musical notes around them, stylized pictionary drawing, no text"},
    {"word": "游戏手柄", "aliases": ("手柄",), "category": "日常", "hint": "双摇杆加十字键的居家快乐源泉", "prompt": "a classic video game controller with d-pad, joysticks and colorful buttons, stylized pictionary drawing, no text"},
    {"word": "雨伞", "aliases": ("打伞",), "category": "日常", "hint": "下雨天撑在头顶遮风挡雨的工具", "prompt": "a bright yellow umbrella opened in falling raindrops with tiny puddles below, stylized pictionary drawing, no text"},
    {"word": "咖啡", "aliases": ("拿铁", "美式咖啡"), "category": "日常", "hint": "拉花热气袅袅的提神饮品", "prompt": "a ceramic mug of steaming coffee with heart latte art and coffee beans beside it, stylized pictionary drawing, no text"},
    {"word": "吉他", "aliases": ("木吉他",), "category": "日常", "hint": "六根琴弦的弹唱乐器", "prompt": "an acoustic wooden guitar leaning against a stool with warm spotlight, stylized pictionary drawing, no text"},
    {"word": "画蛇添足", "aliases": (), "category": "成语", "hint": "四字成语：给没有脚的爬行动物硬加上四条腿", "prompt": "a funny green snake awkwardly walking on four newly drawn cartoon feet with a paintbrush nearby, stylized pictionary illustration, no text"},
    {"word": "守株待兔", "aliases": (), "category": "成语", "hint": "四字成语：农夫靠在树桩旁等着小兔子自己撞上来", "prompt": "a farmer lazily leaning against a tree stump waiting while a rabbit runs nearby, stylized pictionary illustration, no text"},
    {"word": "对牛弹琴", "aliases": (), "category": "成语", "hint": "四字成语：对着一头吃草的大牛优雅地拨动琴弦", "prompt": "a musician playing a traditional guqin serenely in front of a confused grazing cow, stylized pictionary illustration, no text"},
    {"word": "井底之蛙", "aliases": (), "category": "成语", "hint": "四字成语：小青蛙蹲在深井底下仰望小小圆形天空", "prompt": "a little green frog sitting at the bottom of a stone well looking up at a tiny circle of blue sky, stylized pictionary illustration, no text"},
)


@dataclass
class TelephoneTurn:
    step_no: int
    kind: str  # "text" | "image"
    author_uid: str
    author_name: str
    text: str = ""
    image_path: str = ""
    artwork_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "kind": self.kind,
            "author_uid": self.author_uid,
            "author_name": self.author_name,
            "text": self.text,
            "image_path": self.image_path,
            "artwork_source": self.artwork_source,
        }


@dataclass
class TelephoneRoom:
    room_key: str
    group_id: str
    host_id: str
    host_name: str
    title: str = "AI 传话筒"
    phase: str = "waiting"  # "waiting" | "playing" | "finished"
    players: list[dict[str, str]] = field(default_factory=list)
    turns: list[TelephoneTurn] = field(default_factory=list)
    current_player_index: int = 0
    max_text_turns: int = 4
    initial_prompt: str = ""
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)

    def touch(self, now: float | None = None) -> None:
        self.last_active_at = float(now) if now is not None else time.time()

    def has_player(self, uid: str) -> bool:
        return any(p["uid"] == uid for p in self.players)

    def text_turn_count(self) -> int:
        return sum(1 for t in self.turns if t.kind == "text")

    def next_player(self) -> dict[str, str] | None:
        if not self.players:
            return None
        return self.players[self.current_player_index % len(self.players)]


@dataclass
class PictionaryRoom:
    room_key: str
    group_id: str
    host_id: str
    host_name: str
    phase: str = "playing"  # "playing" | "finished"
    round_no: int = 0
    max_rounds: int = 5
    current_word: str = ""
    current_aliases: tuple[str, ...] = ()
    current_category: str = ""
    current_hint: str = ""
    current_image_path: str = ""
    artwork_source: str = ""
    hint_level: int = 0
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    used_words: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)

    def touch(self, now: float | None = None) -> None:
        self.last_active_at = float(now) if now is not None else time.time()


DEFAULT_ROOM_IDLE_TTL_SECONDS = 1800.0


def _normalize_guess(text: str) -> str:
    clean = str(text or "").strip().lower()
    return re.sub(r"[\s,，。！!？?、~～·“”\"'‘’《》【】\[\]()（）]+", "", clean)


class MultimodalPartyEngine(Component):
    """Built-in AI Telephone (Gartic Phone) & AI Pictionary engine."""

    def __init__(
        self,
        context: Context,
        config: dict[str, Any] | None = None,
        *,
        output_dir: Path | None = None,
    ) -> None:
        super().__init__(context)
        self.config = config or {}
        self._output_dir = output_dir
        self.telephone_rooms: dict[str, TelephoneRoom] = {}
        self.pictionary_rooms: dict[str, PictionaryRoom] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.state_change_callback: Any = None
        self.completion_callback: Any = None
        try:
            raw_ttl = self.config.get(
                "room_idle_ttl_seconds", DEFAULT_ROOM_IDLE_TTL_SECONDS
            )
            self._room_idle_ttl_seconds: float = max(60.0, float(raw_ttl))
        except (TypeError, ValueError):
            self._room_idle_ttl_seconds = DEFAULT_ROOM_IDLE_TTL_SECONDS

    def _lock(self, key: str) -> asyncio.Lock:
        lk = self._locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[key] = lk
        return lk

    def _release_lock_if_idle(self, key: str) -> bool:
        lk = self._locks.get(key)
        if lk is not None and not lk.locked():
            self._locks.pop(key, None)
            return True
        return False

    def cleanup_idle_rooms(self, *, now: float | None = None) -> int:
        """Remove idle/finished telephone & pictionary rooms past TTL and reclaim unused locks."""
        cur = float(now) if now is not None else time.time()
        ttl = max(0.0, float(getattr(self, "_room_idle_ttl_seconds", DEFAULT_ROOM_IDLE_TTL_SECONDS)))
        removed = 0

        for rk, room in list(self.telephone_rooms.items()):
            last_ts = max(float(getattr(room, "last_active_at", 0.0) or 0.0), float(room.created_at or 0.0))
            if room.phase == "finished" or (cur - last_ts) >= ttl:
                self.telephone_rooms.pop(rk, None)
                self._release_lock_if_idle(f"tel:{rk}")
                removed += 1

        for rk, room in list(self.pictionary_rooms.items()):
            last_ts = max(float(getattr(room, "last_active_at", 0.0) or 0.0), float(room.created_at or 0.0))
            if room.phase == "finished" or (cur - last_ts) >= ttl:
                self.pictionary_rooms.pop(rk, None)
                self._release_lock_if_idle(f"pic:{rk}")
                removed += 1

        for lock_key in list(self._locks.keys()):
            if lock_key.startswith("tel:"):
                rk = lock_key[4:]
                if rk not in self.telephone_rooms:
                    self._release_lock_if_idle(lock_key)
            elif lock_key.startswith("pic:"):
                rk = lock_key[4:]
                if rk not in self.pictionary_rooms:
                    self._release_lock_if_idle(lock_key)
        return removed

    @staticmethod
    def _event_identity(event: Any) -> tuple[str, str, str, str, bool]:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        try:
            gid = str(event.get_group_id() or "").strip()
        except Exception:
            gid = str(getattr(event, "group_id", "") or "").strip()
        try:
            uid = str(event.get_sender_id() or "").strip()
        except Exception:
            uid = str(getattr(event, "sender_id", "") or "").strip()
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            name = ""
        if not name:
            name = f"群友{uid[-4:]}" if uid else "群友"
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        room_key = umo or gid
        return room_key, gid, uid, name, is_admin

    async def _notify_state(
        self,
        *,
        umo: str,
        active: bool,
        subgame: str,
        title: str,
        phase: str,
    ) -> None:
        cb = self.state_change_callback
        if callable(cb):
            try:
                res = cb(
                    umo=umo,
                    active=active,
                    engine="multimodal_party",
                    activity_id=f"mp:{subgame}",
                    title=title,
                    phase=phase,
                )
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.debug("多模态游戏状态通知失败: %s", exc)

    async def _notify_completion(self, payload: dict[str, Any]) -> None:
        cb = self.completion_callback
        if callable(cb):
            try:
                res = cb(payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.debug("多模态游戏结项归档失败: %s", exc)

    def active_room_count(self) -> int:
        self.cleanup_idle_rooms()
        return len(self.telephone_rooms) + len(self.pictionary_rooms)

    def get_group_state(self, umo: str, group_id: str = "") -> dict[str, Any]:
        self.cleanup_idle_rooms()
        target = str(umo or "").strip()
        gid = str(group_id or "").strip()
        tel = self.telephone_rooms.get(target) or (
            self.telephone_rooms.get(gid) if gid else None
        )
        pic = self.pictionary_rooms.get(target) or (
            self.pictionary_rooms.get(gid) if gid else None
        )
        active = bool(tel or pic)
        phase = "idle"
        subgames: list[str] = []
        if tel:
            subgames.append("telephone")
            phase = tel.phase
        if pic:
            subgames.append("pictionary")
            phase = "playing"
        return {
            "active": active,
            "phase": phase,
            "subgames": subgames,
            "telephone": (
                {
                    "phase": tel.phase,
                    "players": len(tel.players),
                    "turns": len(tel.turns),
                    "title": tel.title,
                }
                if tel
                else None
            ),
            "pictionary": (
                {
                    "phase": pic.phase,
                    "round_no": pic.round_no,
                    "max_rounds": pic.max_rounds,
                    "category": pic.current_category,
                }
                if pic
                else None
            ),
        }

    # ------------------------------------------------------------------
    # Game 1: AI 传话筒 (Gartic Phone)
    # ------------------------------------------------------------------
    async def _render_telephone_visual(
        self, room: TelephoneRoom, step_no: int, prompt_text: str
    ) -> tuple[str, str]:
        art = await generate_artwork_via_visual_media(
            self.context,
            f"根据下面这段脑洞描述绘制生动有趣的插画，画面不要出现任何文字：{prompt_text[:200]}",
            umo=room.room_key,
            style="comic_strip",
            source_plugin="astrbot_plugin_dynamics_games",
        )
        if art.get("ok") and art.get("image_path"):
            return str(art["image_path"]), "visual_media"
        fallback = await asyncio.to_thread(
            render_telephone_step_card,
            room.title,
            step_no,
            prompt_text,
            output_dir=self._output_dir,
        )
        return str(fallback), "pillow_fallback"

    async def _finish_telephone_locked(
        self, room: TelephoneRoom
    ) -> tuple[str, str, dict[str, Any]]:
        room.phase = "finished"
        self.telephone_rooms.pop(room.room_key, None)
        strip_path = ""
        if room.turns:
            try:
                p = await asyncio.to_thread(
                    render_telephone_strip,
                    room.title,
                    [t.to_dict() for t in room.turns],
                    output_dir=self._output_dir,
                )
                strip_path = str(p)
            except Exception as exc:
                logger.warning("生成传话筒对比长图失败: %s", exc)

        text_turns = [t for t in room.turns if t.kind == "text"]
        first_txt = text_turns[0].text if text_turns else "（无）"
        last_txt = text_turns[-1].text if text_turns else "（无）"
        names = "、".join(p["name"] for p in room.players)
        lines = [
            f"🏁 AI 传话筒《{room.title}》圆满收官！（共 {len(room.turns)} 棒 · {len(room.players)} 人参与）",
            f"· 参与成员：{names}",
            f"· 初始画面：{first_txt[:100]}",
            f"· 最终歪楼：{last_txt[:100]}",
        ]
        if strip_path:
            lines.append("🖼️ 已将全链路图文拼合成对比长图（同步收藏至 3D 小屋照片墙）：")

        completion_payload = {
            "umo": room.room_key,
            "group_id": room.group_id,
            "subgame": "telephone",
            "title": f"AI 传话筒《{room.title}》",
            "summary": f"群友（{names}）完成 AI 传话筒《{room.title}》（共 {len(room.turns)} 棒），从「{first_txt[:50]}」一路歪楼成「{last_txt[:50]}」。",
            "participants": [{"uid": p["uid"], "name": p["name"]} for p in room.players],
            "image_path": strip_path,
            "rounds": max(1, len(text_turns)),
        }
        return "\n".join(lines), strip_path, completion_payload

    async def cmd_telephone(self, event: AstrMessageEvent, raw_args: str = ""):
        """Handle `/传话筒 [发起|加入|开始|提交|状态|结束]`."""
        room_key, gid, uid, name, is_admin = self._event_identity(event)
        if not gid:
            yield event.plain_result("AI 传话筒需要在群聊中游玩哦。")
            return

        text = str(raw_args or "").strip()
        parts = text.split(None, 1) if text else []
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if not sub or sub in ("帮助", "help", "说明"):
            yield event.plain_result(
                "📞 AI 传话筒（Gartic Phone）玩法说明：\n"
                "· `/传话筒 发起 [主题/首句]` — 开启传话筒房间\n"
                "· `/传话筒 加入` — 加入等待中的房间\n"
                "· `/传话筒 开始 [首句画面描述]` — 房主开局（文→AI生图→下家看图写描述循环）\n"
                "· `/传话筒 提交 <画面描述>` — 看图脑补写下你的描述，触发 AI 接力绘图\n"
                "· `/传话筒 状态` — 查看当前棒次与参与名单\n"
                "· `/传话筒 结束` — 提前结算并发布全链路对比长图"
            )
            return

        notify_state_args: dict[str, Any] | None = None
        completion_payload: dict[str, Any] | None = None
        out_text = ""
        out_image = ""

        self.cleanup_idle_rooms()
        lock_key = f"tel:{room_key}"
        async with self._lock(lock_key):
            room = self.telephone_rooms.get(room_key)
            if room is not None:
                room.touch()

            if sub in ("发起", "创建", "new", "create"):
                if room is not None and room.phase != "finished":
                    out_text = "本群已有进行中的 AI 传话筒房间，可发送 `/传话筒 加入` 或 `/传话筒 状态`。"
                else:
                    title = rest[:30] if rest else "群友脑洞传话"
                    new_room = TelephoneRoom(
                        room_key=room_key,
                        group_id=gid,
                        host_id=uid,
                        host_name=name,
                        title=title,
                        initial_prompt=rest,
                        players=[{"uid": uid, "name": name}],
                    )
                    self.telephone_rooms[room_key] = new_room
                    notify_state_args = {
                        "umo": room_key,
                        "active": True,
                        "subgame": "telephone",
                        "title": title,
                        "phase": "waiting",
                    }
                    out_text = (
                        f"📞 {name} 发起了 AI 传话筒《{title}》！\n"
                        "· 群友发送 `/传话筒 加入` 上车\n"
                        "· 人齐后房主发送 `/传话筒 开始 [首句画面描述]` 正式开画！"
                    )

            elif sub in ("加入", "报名", "join"):
                if room is None:
                    out_text = "本群暂无等待中的传话筒房间，发送 `/传话筒 发起` 开一局吧！"
                elif room.phase != "waiting":
                    out_text = "传话筒已经开局啦，可在当前轮次直接参与围观或等下局。"
                elif room.has_player(uid):
                    out_text = "你已经在传话筒名单里啦！"
                elif len(room.players) >= 12:
                    out_text = "房间已满（最多 12 人）。"
                else:
                    room.players.append({"uid": uid, "name": name})
                    roster = "、".join(p["name"] for p in room.players)
                    out_text = f"✅ {name} 已加入 AI 传话筒《{room.title}》！（当前 {len(room.players)} 人：{roster}）"

            elif sub in ("状态", "status"):
                if room is None:
                    out_text = "本群当前没有进行中的 AI 传话筒房间。发送 `/传话筒 发起` 开启！"
                else:
                    roster = "、".join(p["name"] for p in room.players)
                    nxt = room.next_player()
                    nxt_str = f" ｜ 下一棒建议：{nxt['name']}" if nxt and room.phase == "playing" else ""
                    out_text = (
                        f"📞 AI 传话筒《{room.title}》\n"
                        f"· 阶段：{'等待加入' if room.phase == 'waiting' else '接力进行中'}\n"
                        f"· 进度：{room.text_turn_count()}/{room.max_text_turns} 轮文本（共 {len(room.turns)} 棒）{nxt_str}\n"
                        f"· 参与成员（{len(room.players)}人）：{roster}"
                    )

            elif sub in ("开始", "start"):
                if room is None or room.phase != "waiting":
                    out_text = "没有等待中的 AI 传话筒房间，请先 `/传话筒 发起`。"
                elif uid != room.host_id and not is_admin:
                    out_text = f"只有房主（{room.host_name}）或管理员可以开始传话筒。"
                else:
                    room.phase = "playing"
                    room.max_text_turns = max(2, min(8, max(len(room.players), 3)))
                    notify_state_args = {
                        "umo": room_key,
                        "active": True,
                        "subgame": "telephone",
                        "title": room.title,
                        "phase": "playing",
                    }
                    seed_prompt = rest or room.initial_prompt
                    if seed_prompt:
                        t1 = TelephoneTurn(
                            step_no=1,
                            kind="text",
                            author_uid=uid,
                            author_name=name,
                            text=seed_prompt,
                        )
                        room.turns.append(t1)
                        img_path, src = await self._render_telephone_visual(
                            room, 2, seed_prompt
                        )
                        t2 = TelephoneTurn(
                            step_no=2,
                            kind="image",
                            author_uid="ai_artist",
                            author_name="AI 画师" if src == "visual_media" else "离线画板",
                            text=seed_prompt,
                            image_path=img_path,
                            artwork_source=src,
                        )
                        room.turns.append(t2)
                        room.current_player_index = 1 % len(room.players)
                        nxt = room.next_player()
                        out_image = img_path
                        out_text = (
                            f"🎬 AI 传话筒《{room.title}》开局！第 1 棒画面已由 AI 绘出"
                            + ("（视觉工坊降级画板）" if src != "visual_media" else "")
                            + f"：\n👉 请下一棒 {nxt['name'] if nxt else '群友'} 看图脑补，发送 `/传话筒 提交 <你的画面描述>`！"
                        )
                    else:
                        nxt = room.next_player()
                        out_text = (
                            f"🎬 AI 传话筒《{room.title}》正式开局！（共 {room.max_text_turns} 轮接力）\n"
                            f"👉 请首棒 {nxt['name'] if nxt else name} 发送 `/传话筒 提交 <初始画面描述>` 开启第一棒！"
                        )

            elif sub in ("提交", "submit", "描述", "接棒"):
                if room is None or room.phase != "playing":
                    out_text = "当前没有进行中的 AI 传话筒，请先 `/传话筒 发起` 并 `/传话筒 开始`。"
                elif not rest:
                    out_text = "请附上你的画面描述，例如：`/传话筒 提交 一只戴着墨镜的橘猫在宇宙冲浪`"
                else:
                    if not room.has_player(uid):
                        room.players.append({"uid": uid, "name": name})
                    step_text_no = len(room.turns) + 1
                    room.turns.append(
                        TelephoneTurn(
                            step_no=step_text_no,
                            kind="text",
                            author_uid=uid,
                            author_name=name,
                            text=rest[:240],
                        )
                    )
                    step_img_no = len(room.turns) + 1
                    img_path, src = await self._render_telephone_visual(
                        room, step_img_no, rest[:240]
                    )
                    room.turns.append(
                        TelephoneTurn(
                            step_no=step_img_no,
                            kind="image",
                            author_uid="ai_artist",
                            author_name="AI 画师" if src == "visual_media" else "离线画板",
                            text=rest[:240],
                            image_path=img_path,
                            artwork_source=src,
                        )
                    )
                    room.current_player_index = (room.current_player_index + 1) % len(
                        room.players
                    )
                    if room.text_turn_count() >= room.max_text_turns:
                        out_text, out_image, completion_payload = (
                            await self._finish_telephone_locked(room)
                        )
                        notify_state_args = {
                            "umo": room_key,
                            "active": False,
                            "subgame": "telephone",
                            "title": room.title,
                            "phase": "finished",
                        }
                    else:
                        nxt = room.next_player()
                        out_image = img_path
                        out_text = (
                            f"🎨 第 {room.text_turn_count()}/{room.max_text_turns} 轮接力完成（{name} 执笔 → AI 绘图）！\n"
                            f"👉 请下一棒 {nxt['name'] if nxt else '群友'} 看图发送 `/传话筒 提交 <画面描述>`！"
                        )

            elif sub in ("结束", "结算", "end", "finish"):
                if room is None:
                    out_text = "本群当前没有进行中的 AI 传话筒房间。"
                elif uid != room.host_id and not is_admin:
                    out_text = f"只有房主（{room.host_name}）或管理员可结束传话筒。"
                else:
                    out_text, out_image, completion_payload = (
                        await self._finish_telephone_locked(room)
                    )
                    notify_state_args = {
                        "umo": room_key,
                        "active": False,
                        "subgame": "telephone",
                        "title": room.title,
                        "phase": "finished",
                    }
            else:
                out_text = "未知子命令。可用：`/传话筒 发起 | 加入 | 开始 | 提交 <描述> | 状态 | 结束`。"

        if room_key not in self.telephone_rooms:
            self._release_lock_if_idle(lock_key)

        if notify_state_args:
            await self._notify_state(**notify_state_args)
        if completion_payload and len(completion_payload.get("participants") or []) >= 1:
            await self._notify_completion(completion_payload)

        if out_text:
            yield event.plain_result(out_text)
        if out_image:
            yield event.image_result(out_image)

    # ------------------------------------------------------------------
    # Game 2: 你画我猜 (Pictionary)
    # ------------------------------------------------------------------
    async def _draw_pictionary_round(self, room: PictionaryRoom) -> tuple[str, str]:
        pool = [w for w in PICTIONARY_WORD_BANK if w["word"] not in room.used_words]
        if not pool:
            room.used_words.clear()
            pool = list(PICTIONARY_WORD_BANK)
        picked = random.choice(pool)
        room.round_no += 1
        room.used_words.add(picked["word"])
        room.current_word = picked["word"]
        room.current_aliases = tuple(picked.get("aliases") or ())
        room.current_category = picked["category"]
        room.current_hint = picked["hint"]
        room.hint_level = 0

        art = await generate_artwork_via_visual_media(
            self.context,
            picked["prompt"],
            umo=room.room_key,
            style="pictionary",
            source_plugin="astrbot_plugin_dynamics_games",
        )
        if art.get("ok") and art.get("image_path"):
            room.current_image_path = str(art["image_path"])
            room.artwork_source = "visual_media"
        else:
            fallback = await asyncio.to_thread(
                render_pictionary_fallback_card,
                room.current_category,
                len(room.current_word),
                room.round_no,
                clue=room.current_hint,
                seed_text=f"{room.room_key}:{room.round_no}",
                output_dir=self._output_dir,
            )
            room.current_image_path = str(fallback)
            room.artwork_source = "pillow_fallback"
        return room.current_image_path, room.artwork_source

    def _format_pictionary_scoreboard(self, room: PictionaryRoom) -> str:
        if not room.scores:
            return "本局暂无群友答对得分。"
        ranked = sorted(
            room.scores.values(), key=lambda x: (-int(x["score"]), str(x["name"]))
        )
        lines = ["🏆 本局你画我猜积分榜："]
        for idx, row in enumerate(ranked[:10], 1):
            lines.append(f"{idx}. {row['name']} — {row['score']} 分")
        return "\n".join(lines)

    def _finish_pictionary_locked(
        self, room: PictionaryRoom, *, header: str
    ) -> tuple[str, dict[str, Any]]:
        room.phase = "finished"
        self.pictionary_rooms.pop(room.room_key, None)
        board_txt = self._format_pictionary_scoreboard(room)
        participants = [
            {"uid": uid, "name": str(row["name"]), "role_weight": 3 if idx == 0 else 1}
            for idx, (uid, row) in enumerate(
                sorted(
                    room.scores.items(),
                    key=lambda kv: -int(kv[1]["score"]),
                )
            )
        ]
        summary = (
            f"你画我猜完结（共 {room.round_no} 轮，{len(participants)} 位得分玩家）。"
            + (
                f" 冠军：{participants[0]['name']}。"
                if participants
                else ""
            )
        )
        payload = {
            "umo": room.room_key,
            "group_id": room.group_id,
            "subgame": "pictionary",
            "title": "AI 你画我猜",
            "summary": summary,
            "participants": participants,
            "image_path": "",
            "rounds": max(1, room.round_no),
        }
        return f"{header}\n{board_txt}", payload

    def _matches_answer(self, room: PictionaryRoom, guess: str) -> bool:
        g = _normalize_guess(guess)
        if not g:
            return False
        targets = [_normalize_guess(room.current_word)] + [
            _normalize_guess(a) for a in room.current_aliases
        ]
        return g in {t for t in targets if t}

    async def cmd_pictionary(self, event: AstrMessageEvent, raw_args: str = ""):
        """Handle `/你画我猜 [开始|猜 <词>|提示|跳过|结束]`."""
        room_key, gid, uid, name, is_admin = self._event_identity(event)
        if not gid:
            yield event.plain_result("你画我猜需要在群聊中游玩哦。")
            return

        text = str(raw_args or "").strip()
        parts = text.split(None, 1) if text else []
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("帮助", "help", "说明"):
            yield event.plain_result(
                "🎨 AI 你画我猜（Pictionary）玩法说明：\n"
                "· `/你画我猜 开始 [轮数]` — 开启新一局（默认 5 题，支持 1~10 题）\n"
                "· `/你画我猜 猜 <词>`（或直接在群里发答案词） — 抢答猜词得分\n"
                "· `/你画我猜 提示` — 获取渐进式提示（分类字数 → 线索 → 首字）\n"
                "· `/你画我猜 跳过` — 公布本题答案并进入下一题\n"
                "· `/你画我猜 结束` — 结束当前对局并公布积分榜"
            )
            return

        notify_state_args: dict[str, Any] | None = None
        completion_payload: dict[str, Any] | None = None
        out_text = ""
        out_image = ""

        self.cleanup_idle_rooms()
        lock_key = f"pic:{room_key}"
        async with self._lock(lock_key):
            room = self.pictionary_rooms.get(room_key)
            if room is not None:
                room.touch()

            if not sub:
                if room is None:
                    sub = "开始"
                else:
                    out_text = (
                        f"🎨 你画我猜进行中（第 {room.round_no}/{room.max_rounds} 题 · 分类：{room.current_category} · {len(room.current_word)} 字）\n"
                        "发送 `/你画我猜 猜 <词>` 或直接打字抢答！需要线索发 `/你画我猜 提示`。"
                    )

            if sub in ("开始", "start", "开局"):
                if room is not None and room.phase == "playing":
                    out_text = (
                        f"本群正在进行你画我猜第 {room.round_no}/{room.max_rounds} 题！"
                        "直接猜词或发送 `/你画我猜 结束` 重开。"
                    )
                else:
                    rounds = 5
                    if rest.isdigit():
                        rounds = max(1, min(10, int(rest)))
                    new_room = PictionaryRoom(
                        room_key=room_key,
                        group_id=gid,
                        host_id=uid,
                        host_name=name,
                        max_rounds=rounds,
                    )
                    self.pictionary_rooms[room_key] = new_room
                    img_path, src = await self._draw_pictionary_round(new_room)
                    out_image = img_path
                    fallback_tip = (
                        "（视觉工坊未就绪，已切换离线谜面卡）"
                        if src != "visual_media"
                        else ""
                    )
                    out_text = (
                        f"🎨 AI 你画我猜开局！（共 {rounds} 题）{fallback_tip}\n"
                        f"第 {new_room.round_no}/{rounds} 题：提示分类【{new_room.current_category}】，共 {len(new_room.current_word)} 个字！\n"
                        "直接发送答案或 `/你画我猜 猜 <词>` 抢答，卡壳可发 `/你画我猜 提示`。"
                    )
                    notify_state_args = {
                        "umo": room_key,
                        "active": True,
                        "subgame": "pictionary",
                        "title": "AI 你画我猜",
                        "phase": "playing",
                    }

            elif sub in ("猜", "guess"):
                if room is None or room.phase != "playing":
                    out_text = "本群当前没有进行中的你画我猜，发送 `/你画我猜 开始` 开一局！"
                elif not rest:
                    out_text = "请附上你要猜的词，例如：`/你画我猜 猜 火锅`"
                elif self._matches_answer(room, rest):
                    pts = max(1, 3 - room.hint_level)
                    entry = room.scores.setdefault(uid, {"name": name, "score": 0})
                    entry["name"] = name
                    entry["score"] = int(entry["score"]) + pts
                    answered = room.current_word
                    if room.round_no >= room.max_rounds:
                        out_text, completion_payload = self._finish_pictionary_locked(
                            room,
                            header=f"🎉 恭喜 {name} 猜中最后一题【{answered}】（+{pts} 分）！",
                        )
                        notify_state_args = {
                            "umo": room_key,
                            "active": False,
                            "subgame": "pictionary",
                            "title": "AI 你画我猜",
                            "phase": "finished",
                        }
                    else:
                        img_path, _ = await self._draw_pictionary_round(room)
                        out_image = img_path
                        out_text = (
                            f"🎉 恭喜 {name} 抢答成功！正确答案是【{answered}】（+{pts} 分，当前 {entry['score']} 分）！\n"
                            f"➡️ 第 {room.round_no}/{room.max_rounds} 题：分类【{room.current_category}】，共 {len(room.current_word)} 个字！"
                        )
                else:
                    out_text = f"🤔「{rest[:20]}」不对哦，再仔细看看图（或发 `/你画我猜 提示`）！"

            elif sub in ("提示", "hint"):
                if room is None or room.phase != "playing":
                    out_text = "本群当前没有进行中的你画我猜。"
                else:
                    room.hint_level += 1
                    if room.hint_level == 1:
                        out_text = f"💡 提示 1：分类【{room.current_category}】，共 {len(room.current_word)} 个字，线索——{room.current_hint}"
                    else:
                        first_ch = room.current_word[:1]
                        masked = first_ch + " " + " ".join("_" for _ in range(max(0, len(room.current_word) - 1)))
                        out_text = f"💡 终极提示：{masked.strip()}（{room.current_hint}）"

            elif sub in ("跳过", "skip"):
                if room is None or room.phase != "playing":
                    out_text = "本群当前没有进行中的你画我猜。"
                else:
                    missed = room.current_word
                    if room.round_no >= room.max_rounds:
                        out_text, completion_payload = self._finish_pictionary_locked(
                            room,
                            header=f"⏭️ 已跳过最后一题，答案是【{missed}】。",
                        )
                        notify_state_args = {
                            "umo": room_key,
                            "active": False,
                            "subgame": "pictionary",
                            "title": "AI 你画我猜",
                            "phase": "finished",
                        }
                    else:
                        img_path, _ = await self._draw_pictionary_round(room)
                        out_image = img_path
                        out_text = (
                            f"⏭️ 本题跳过，答案是【{missed}】！\n"
                            f"➡️ 第 {room.round_no}/{room.max_rounds} 题：分类【{room.current_category}】，共 {len(room.current_word)} 个字！"
                        )

            elif sub in ("结束", "end", "stop"):
                if room is None:
                    out_text = "本群当前没有进行中的你画我猜。"
                elif uid != room.host_id and not is_admin:
                    out_text = f"只有发起人（{room.host_name}）或管理员可提前结束你画我猜。"
                else:
                    ans = room.current_word
                    out_text, completion_payload = self._finish_pictionary_locked(
                        room,
                        header=f"🏁 你画我猜已结束（本题答案：【{ans}】）。",
                    )
                    notify_state_args = {
                        "umo": room_key,
                        "active": False,
                        "subgame": "pictionary",
                        "title": "AI 你画我猜",
                        "phase": "finished",
                    }
            elif sub:
                # Treat bare text like `/你画我猜 火锅` as a guess when a room is active
                if room is not None and room.phase == "playing":
                    if self._matches_answer(room, text):
                        pts = max(1, 3 - room.hint_level)
                        entry = room.scores.setdefault(uid, {"name": name, "score": 0})
                        entry["name"] = name
                        entry["score"] = int(entry["score"]) + pts
                        answered = room.current_word
                        if room.round_no >= room.max_rounds:
                            out_text, completion_payload = self._finish_pictionary_locked(
                                room,
                                header=f"🎉 恭喜 {name} 猜中最后一题【{answered}】（+{pts} 分）！",
                            )
                            notify_state_args = {
                                "umo": room_key,
                                "active": False,
                                "subgame": "pictionary",
                                "title": "AI 你画我猜",
                                "phase": "finished",
                            }
                        else:
                            img_path, _ = await self._draw_pictionary_round(room)
                            out_image = img_path
                            out_text = (
                                f"🎉 恭喜 {name} 抢答成功！正确答案是【{answered}】（+{pts} 分，当前 {entry['score']} 分）！\n"
                                f"➡️ 第 {room.round_no}/{room.max_rounds} 题：分类【{room.current_category}】，共 {len(room.current_word)} 个字！"
                            )
                    else:
                        out_text = f"🤔「{text[:20]}」不对哦，继续猜或发 `/你画我猜 提示`！"
                else:
                    out_text = "未知子命令。可用：`/你画我猜 开始 | 猜 <词> | 提示 | 跳过 | 结束`。"

        if room_key not in self.pictionary_rooms:
            self._release_lock_if_idle(lock_key)

        if notify_state_args:
            await self._notify_state(**notify_state_args)
        if completion_payload and completion_payload.get("participants"):
            await self._notify_completion(completion_payload)

        if out_text:
            yield event.plain_result(out_text)
        if out_image:
            yield event.image_result(out_image)

    async def on_group_message(self, event: AstrMessageEvent):
        """Passive fast-path allowing direct chat guesses when a Pictionary round is active."""
        self.cleanup_idle_rooms()
        room_key, gid, uid, name, _ = self._event_identity(event)
        if not gid or room_key not in self.pictionary_rooms:
            return
        raw_text = str(getattr(event, "message_str", "") or "").strip()
        if not raw_text or raw_text.startswith(("/", "!", "！")) or len(raw_text) > 24:
            return
        room = self.pictionary_rooms.get(room_key)
        if room is None or room.phase != "playing" or not self._matches_answer(room, raw_text):
            return
        async for res in self.cmd_pictionary(event, f"猜 {raw_text}"):
            yield res

    async def terminate(self) -> None:
        self.telephone_rooms.clear()
        self.pictionary_rooms.clear()
        self._locks.clear()
