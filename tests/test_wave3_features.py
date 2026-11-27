"""Wave 3 tests: AI Telephone, Pictionary, Dynamics Event Bus, 3D World Keepsakes & Horizons callback."""

from __future__ import annotations

import ast
import pathlib
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games.core.commands import (  # noqa: E402
    build_help_text,
    handle_cancel_command,
    handle_complete_with_media,
    handle_join_command,
    handle_stage_command,
    handle_submit_with_media,
)
from astrbot_plugin_dynamics_games.core.coordinator import ActivityCoordinator  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.engines.multimodal_party.main import (  # noqa: E402
    MultimodalPartyEngine,
)
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin  # noqa: E402


class _Event:
    def __init__(
        self,
        *,
        umo: str = "aiocqhttp:GroupMessage:9101",
        group_id: str = "9101",
        uid: str = "101",
        name: str = "甲",
        is_admin: bool = False,
        message_str: str = "",
    ) -> None:
        self.unified_msg_origin = umo
        self._gid = group_id
        self._uid = uid
        self._name = name
        self._admin = is_admin
        self.message_str = message_str
        self.message_obj = SimpleNamespace(message_id="54321", message=[])

    def get_group_id(self) -> str:
        return self._gid

    def get_sender_id(self) -> str:
        return self._uid

    def get_sender_name(self) -> str:
        return self._name

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str) -> tuple[str, str]:
        return ("text", text)

    def image_result(self, path: str) -> tuple[str, str]:
        return ("image", path)


async def _drain(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 1. AI 传话筒 (Gartic Phone): visual_media + Pillow fallback + stitched strip
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ai_telephone_full_flow_strip_keepsake_and_fallback(
    tmp_path: pathlib.Path,
):
    art_path = tmp_path / "ai_step.png"
    Image.new("RGB", (320, 240), "#5588AA").save(art_path)

    visual_calls: list[dict] = []
    bus_events: list[tuple[str, dict]] = []
    keepsakes: list[dict] = []

    class _FakeVisual:
        async def generate_series_artwork(self, prompt, **kwargs):
            visual_calls.append({"prompt": prompt, **kwargs})
            return {"ok": True, "image_path": str(art_path), "error": ""}

    class _FakeChat:
        def get_series_shared_config(self):
            return {"persona_id": "anan"}

        async def emit_dynamics_event(self, event_type, payload):
            bus_events.append((event_type, payload))
            return 1

    class _FakeWorld:
        async def register_external_keepsake(self, **kwargs):
            keepsakes.append(kwargs)
            return {"ok": True, "keepsake_id": "ks-1"}

    stars = [
        SimpleNamespace(name="astrbot_plugin_visual_media", activated=True, star_cls=_FakeVisual()),
        SimpleNamespace(name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_FakeChat()),
        SimpleNamespace(name="astrbot_plugin_dynamics_world", activated=True, star_cls=_FakeWorld()),
    ]
    ctx = SimpleNamespace(get_all_stars=lambda: stars, get_registered_star=lambda _n: None, get_config=lambda: {})

    store = ActivityStore(tmp_path / "tel.db")
    coord = ActivityCoordinator(
        store,
        ctx,
        {"activities": {"memory_sync_enabled": False, "chronicle_sync_enabled": False}},
    )
    plugin = DynamicsGamesPlugin(ctx, {"activities": {"enabled": True}})
    plugin.store = store
    plugin.coordinator = coord
    plugin._ready = True

    mp = MultimodalPartyEngine(ctx, {}, output_dir=tmp_path / "mp_out")
    mp.state_change_callback = plugin._on_engine_state_changed
    mp.completion_callback = coord.handle_multimodal_completion
    plugin.engines["multimodal_party"] = mp

    umo = "aiocqhttp:GroupMessage:9101"
    ev1 = _Event(umo=umo, group_id="9101", uid="1", name="甲")
    ev2 = _Event(umo=umo, group_id="9101", uid="2", name="乙")

    try:
        r_open = await _drain(mp.cmd_telephone(ev1, "发起 宇宙橘猫大冒险"))
        assert "发起了 AI 传话筒" in r_open[0][1]
        # Waiting room reflected in get_active_game_state!
        st_wait = plugin.get_active_game_state(umo)
        assert st_wait["active"] is True
        assert "multimodal_party" in st_wait["engines"]
        assert "room_waiting" in st_wait["companion_mode_hint"]

        r_join = await _drain(mp.cmd_telephone(ev2, "加入"))
        assert "已加入" in r_join[0][1]

        r_start = await _drain(mp.cmd_telephone(ev1, "开始 橘猫在月球吃火锅"))
        assert any(k == "image" for k, _ in r_start)
        assert visual_calls and visual_calls[0]["style"] == "comic_strip"

        # Active playing state in get_active_game_state!
        st_play = plugin.get_active_game_state(umo)
        assert "room_playing" in st_play["companion_mode_hint"]

        # Turn 2 & Turn 3 (auto-settles on max_text_turns=3)
        r_t2 = await _drain(mp.cmd_telephone(ev2, "提交 老虎在奶酪上煮拉面"))
        assert any(k == "image" for k, _ in r_t2)

        r_t3 = await _drain(mp.cmd_telephone(ev1, "提交 狮子泡温泉喝奶茶"))
        texts = [v for k, v in r_t3 if k == "text"]
        imgs = [v for k, v in r_t3 if k == "image"]
        assert texts and "圆满收官" in texts[0]
        assert imgs and pathlib.Path(imgs[0]).is_file()
        with Image.open(imgs[0]) as strip_im:
            strip_im.verify()
            assert strip_im.size[1] > 400  # stitched vertical comparison strip!

        # Event bus & 3D World keepsake received the finished telephone strip!
        assert any(et == "game_state_changed" and pl["active"] is False for et, pl in bus_events)
        assert any(et == "milestone_achieved" and pl["image_path"] == imgs[0] for et, pl in bus_events)
        assert len(keepsakes) == 1
        assert keepsakes[0]["image_path"] == imgs[0]
        assert keepsakes[0]["persona_id"] == "anan"
        assert keepsakes[0]["source_plugin"] == "astrbot_plugin_dynamics_games"

        # Pillow fallback when visual_media is absent
        mp.context = SimpleNamespace(get_all_stars=lambda: [])
        await _drain(mp.cmd_telephone(ev1, "发起 离线测试"))
        r_fb = await _drain(mp.cmd_telephone(ev1, "开始 企鹅骑自行车"))
        fb_imgs = [v for k, v in r_fb if k == "image"]
        assert fb_imgs and pathlib.Path(fb_imgs[0]).is_file()
        await _drain(mp.cmd_telephone(ev1, "结束"))
    finally:
        await mp.terminate()
        store.close()


# ---------------------------------------------------------------------------
# 2. 你画我猜 (Pictionary): style="pictionary", hints, passive guess & per-group guard
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pictionary_puzzles_hints_direct_guess_and_per_group_toggle(
    tmp_path: pathlib.Path,
):
    pic_img = tmp_path / "pictionary_art.png"
    Image.new("RGB", (320, 320), "#CC8855").save(pic_img)
    styles_requested: list[str] = []

    class _FakeVisual:
        async def generate_series_artwork(self, prompt, **kwargs):
            styles_requested.append(str(kwargs.get("style") or ""))
            return {"ok": True, "image_path": str(pic_img), "error": ""}

    ctx = SimpleNamespace(
        get_all_stars=lambda: [
            SimpleNamespace(name="astrbot_plugin_visual_media", activated=True, star_cls=_FakeVisual())
        ],
        get_registered_star=lambda _n: None,
        get_config=lambda: {},
    )
    plugin = DynamicsGamesPlugin(ctx, {"activities": {"enabled": True}})
    plugin.store = ActivityStore(tmp_path / "pic.db")
    plugin._ready = True
    mp = MultimodalPartyEngine(ctx, {}, output_dir=tmp_path / "pic_out")
    plugin.engines["multimodal_party"] = mp

    umo = "aiocqhttp:GroupMessage:9202"
    ev1 = _Event(umo=umo, group_id="9202", uid="1", name="画师甲")
    ev2 = _Event(umo=umo, group_id="9202", uid="2", name="猜词乙")

    try:
        # Start 2-round game
        r_start = await _drain(mp.cmd_pictionary(ev1, "开始 2"))
        assert "你画我猜开局" in r_start[0][1]
        assert any(k == "image" for k, _ in r_start)
        assert styles_requested == ["pictionary"]

        room = mp.pictionary_rooms[umo]
        ans1 = room.current_word

        # Hint progression
        h1 = await _drain(mp.cmd_pictionary(ev2, "提示"))
        assert "提示 1" in h1[0][1]
        h2 = await _drain(mp.cmd_pictionary(ev2, "提示"))
        assert "终极提示" in h2[0][1] and ans1[0] in h2[0][1]

        # Wrong guess
        w_res = await _drain(mp.cmd_pictionary(ev2, "猜 完全不相关乱猜词"))
        assert "不对" in w_res[0][1]

        # Passive direct chat guess hits right answer and advances to round 2!
        ev_chat = _Event(umo=umo, group_id="9202", uid="2", name="猜词乙", message_str=ans1)
        hit_res = await _drain(mp.on_group_message(ev_chat))
        assert "抢答成功" in hit_res[0][1]
        assert any(k == "image" for k, _ in hit_res)
        assert mp.pictionary_rooms[umo].round_no == 2

        # Skip final round -> finishes game and shows scoreboard
        skip_res = await _drain(mp.cmd_pictionary(ev1, "跳过"))
        assert "积分榜" in skip_res[0][1] and "猜词乙" in skip_res[0][1]
        assert umo not in mp.pictionary_rooms

        # Offline fallback card renders cleanly when visual_media is unavailable
        mp.context = SimpleNamespace(get_all_stars=lambda: [])
        fb_start = await _drain(mp.cmd_pictionary(ev1, "开始 1"))
        fb_imgs = [v for k, v in fb_start if k == "image"]
        assert fb_imgs and pathlib.Path(fb_imgs[0]).is_file()
        assert "离线谜面卡" in fb_start[0][1]
        await _drain(mp.cmd_pictionary(ev1, "结束"))

        # Per-group switch disables multimodal_party for umo cleanly
        plugin.set_group_module_enabled(umo, "multimodal_party", False)
        eng, rep = plugin._guard_event(ev1, "multimodal_party", explicit=True)
        assert eng is None and rep is not None
        assert plugin._guard_passive_event(ev1, "multimodal_party") is None
    finally:
        await mp.terminate()
        plugin.store.close()


# ---------------------------------------------------------------------------
# 3. Event Bus + 3D World Keepsake + Horizons Co-Read Callback on Activity Lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_activity_event_bus_world_keepsake_and_horizons_coread_callback(
    tmp_path: pathlib.Path, monkeypatch
):
    from astrbot_plugin_dynamics_games.core import poster as poster_mod

    monkeypatch.setattr(poster_mod, "_default_output_dir", lambda: tmp_path / "posters")

    bus_events: list[tuple[str, dict]] = []
    keepsakes: list[dict] = []
    horizons_calls: list[dict] = []

    class _FakeChat:
        def get_series_shared_config(self):
            return {"persona_id": "anan"}

        async def emit_dynamics_event(self, event_type, payload):
            bus_events.append((event_type, payload))
            return 2

    class _FakeWorld:
        async def register_external_keepsake(self, **kwargs):
            keepsakes.append(kwargs)
            return {"ok": True}

    class _FakeHorizons:
        async def mark_book_coread(self, **kwargs):
            horizons_calls.append(kwargs)
            return {"ok": True}

    stars = [
        SimpleNamespace(name="astrbot_plugin_chat_dynamics", activated=True, star_cls=_FakeChat()),
        SimpleNamespace(name="astrbot_plugin_dynamics_world", activated=True, star_cls=_FakeWorld()),
        SimpleNamespace(name="astrbot_plugin_dynamics_horizons", activated=True, star_cls=_FakeHorizons()),
    ]
    ctx = SimpleNamespace(get_all_stars=lambda: stars, get_registered_star=lambda _n: None, get_config=lambda: {})
    cfg = {
        "activities": {
            "enabled": True,
            "memory_sync_enabled": False,
            "chronicle_sync_enabled": False,
            "poster_on_complete": True,
        }
    }
    plugin = DynamicsGamesPlugin(ctx, cfg)
    plugin.store = ActivityStore(tmp_path / "eco.db")
    plugin.coordinator = ActivityCoordinator(plugin.store, ctx, cfg, plugin=plugin)
    plugin._ready = True

    umo = "aiocqhttp:GroupMessage:9303"
    ev_admin = _Event(umo=umo, group_id="9303", uid="99", name="管理员", is_admin=True)
    ev_user = _Event(umo=umo, group_id="9303", uid="102", name="书友乙")

    try:
        # 1. Create reading activity from Horizons companion -> emits game_state_changed(active=True)
        res = await plugin.create_activity_from_companion(
            umo,
            "reading",
            "三体共读计划",
            description="书架联动共读",
            creator_name="见闻书架",
            source_plugin="astrbot_plugin_dynamics_horizons",
        )
        assert res["ok"] is True
        aid = res["activity_id"]
        assert any(
            et == "game_state_changed" and pl["activity_id"] == aid and pl["active"] is True
            for et, pl in bus_events
        )

        # 2. Join, advance stage, submit, complete
        await handle_join_command(plugin.coordinator, ev_user, aid)
        await handle_stage_command(plugin.coordinator, ev_admin, aid, start_only=True)
        await handle_submit_with_media(plugin.coordinator, ev_user, f"{aid} 第一部读完非常震撼！")
        _, poster_path = await handle_complete_with_media(plugin.coordinator, ev_admin, f"{aid} 圆满共读")
        assert poster_path and pathlib.Path(poster_path).is_file()

        # Verify: game_state_changed(completed), milestone_achieved, World keepsake, and Horizons coread callback!
        assert any(
            et == "game_state_changed" and pl["activity_id"] == aid and pl["active"] is False and pl["phase"] == "completed"
            for et, pl in bus_events
        )
        milestones = [pl for et, pl in bus_events if et == "milestone_achieved"]
        assert len(milestones) == 1
        assert milestones[0]["image_path"] == poster_path
        assert f"id={aid}" in milestones[0]["deep_link"]

        assert len(keepsakes) == 1
        assert keepsakes[0]["image_path"] == poster_path
        assert "三体共读计划" in keepsakes[0]["title"]

        assert len(horizons_calls) == 1
        assert horizons_calls[0]["title"] == "三体共读计划"
        assert horizons_calls[0]["activity_id"] == aid

        # 3. Cancelled activity emits game_state_changed(cancelled) but NEVER registers keepsake or milestone
        res2 = await plugin.create_activity_from_companion(
            umo,
            "reading",
            "流局共读",
            source_plugin="astrbot_plugin_dynamics_horizons",
        )
        aid2 = res2["activity_id"]
        await handle_cancel_command(plugin.coordinator, ev_admin, f"{aid2} 改期")
        assert any(
            et == "game_state_changed" and pl["activity_id"] == aid2 and pl["phase"] == "cancelled"
            for et, pl in bus_events
        )
        assert len(keepsakes) == 1
        assert len(horizons_calls) == 1
    finally:
        plugin.store.close()


def test_multimodal_engine_architecture_and_help_text():
    # Engine under engines/ MUST NOT use @filter or @register decorators
    eng_src = (PLUGIN_ROOT / "engines" / "multimodal_party" / "main.py").read_text(encoding="utf-8")
    eng_tree = ast.parse(eng_src)
    for node in ast.walk(eng_tree):
        for dec in getattr(node, "decorator_list", []):
            dec_src = ast.unparse(dec)
            assert not dec_src.startswith(("filter.", "register")), f"Forbidden decorator: {dec_src}"

    main_src = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DynamicsGamesPlugin")
    methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert any("传话筒" in ast.unparse(d) for d in methods["mp_telephone"].decorator_list)
    assert any("你画我猜" in ast.unparse(d) for d in methods["mp_pictionary"].decorator_list)

    help_txt = build_help_text()
    assert "/传话筒" in help_txt and "/你画我猜" in help_txt


def test_conf_schema_objects_have_items_for_astrbot_config_parser():
    """Ensure every type='object' node in _conf_schema.json defines 'items' (AstrBot v4.29+ requirement)."""
    import json

    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def _walk(node: dict, path: str = "") -> dict:
        defaults: dict = {}
        for key, spec in node.items():
            assert isinstance(spec, dict), f"Schema node {path}{key} must be a dict"
            cur_path = f"{path}{key}"
            if spec.get("type") == "object":
                assert "items" in spec and isinstance(spec["items"], dict), (
                    f"Schema object {cur_path!r} is missing dict 'items' required by AstrBotConfig"
                )
                defaults[key] = _walk(spec["items"], path=f"{cur_path}.")
            else:
                defaults[key] = spec.get("default")
        return defaults

    parsed = _walk(schema)
    assert "modules" in parsed
    assert "per_group_modules" in parsed["modules"]
    assert parsed["modules"]["per_group_modules"] == {}
