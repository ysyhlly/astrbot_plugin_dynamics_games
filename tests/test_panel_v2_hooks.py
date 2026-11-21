"""Tests for Dynamics Games v2.0 backend hooks, presence badges, and UI contracts."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrbot_plugin_dynamics_games import series_panels  # noqa: E402
from astrbot_plugin_dynamics_games.core.store import ActivityStore  # noqa: E402
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin  # noqa: E402


def _make_plugin(tmp_path: pathlib.Path, *, ready: bool = True, enabled: bool = True):
    ctx = SimpleNamespace(
        get_all_stars=lambda: [],
        get_registered_star=lambda _name: None,
        get_config=lambda: {},
    )
    plugin = DynamicsGamesPlugin(ctx, {"activities": {"enabled": enabled}})
    plugin.store = ActivityStore(tmp_path / "games_v2.db")
    plugin._ready = ready
    return plugin


def test_series_summary_states_and_counts(tmp_path: pathlib.Path):
    plugin = _make_plugin(tmp_path, ready=False)
    try:
        unready = plugin.get_series_summary()
        assert unready["badge"]["count"] == 0
        assert unready["badge"]["tone"] == "muted"
        assert unready["pulse"]["updated_at"]

        plugin._legacy_conflicts = ["astrbot_plugin_rollpig"]
        conflict = plugin.get_series_summary()
        assert conflict["badge"]["count"] == 1
        assert conflict["badge"]["tone"] == "danger"
        plugin._legacy_conflicts = []

        plugin._ready = True
        idle = plugin.get_series_summary()
        assert idle["badge"]["count"] == 0
        assert idle["badge"]["tone"] == "accent"

        umo = "aiocqhttp:GroupMessage:12345"
        plugin.store.create_activity(
            umo=umo,
            group_id="12345",
            category="reading",
            title="银河铁道之夜周末共读",
            creator_uid="u1",
            creator_name="安安",
        )
        active = plugin.get_series_summary()
        assert active["badge"]["count"] == 1
        assert active["badge"]["tone"] == "info"
        assert "1" in active["badge"]["label"]
        assert "1" in active["pulse"]["headline"]
    finally:
        plugin.store.close()


def test_series_scope_status_search_and_journal_events(tmp_path: pathlib.Path):
    plugin = _make_plugin(tmp_path, ready=True)
    try:
        umo_a = "aiocqhttp:GroupMessage:10001"
        umo_b = "aiocqhttp:GroupMessage:20002"
        act_a = plugin.store.create_activity(
            umo=umo_a,
            group_id="10001",
            category="reading",
            title="森林手账共读会",
            description="周末一起读绘本",
            creator_uid="u1",
            creator_name="甲",
            target_count=3,
        )
        plugin.store.join_activity(act_a["id"], "u2", "乙")
        plugin.store.add_contribution(
            act_a["id"], uid="u1", name="甲", content="第一章读书笔记"
        )
        plugin.store.create_activity(
            umo=umo_b,
            group_id="20002",
            category="relay",
            title="深夜怪谈接龙",
            creator_uid="u3",
            creator_name="丙",
        )

        # Scope status
        st = plugin.get_series_scope_status(umo_a)
        assert st["supported"] is True
        assert st["enabled"] is True
        assert isinstance(st["activity_today"], int) and st["activity_today"] >= 2
        assert "1" in st["summary"]
        assert st["version"]

        # Federated search
        hits_all = plugin.series_search_items("共读", scope="", limit=8)
        assert len(hits_all) == 1
        assert hits_all[0]["plugin_id"] == "astrbot_plugin_dynamics_games"
        assert hits_all[0]["page"] == "games"
        assert hits_all[0]["deep_link"]["id"] == act_a["id"]

        hits_scoped_miss = plugin.series_search_items("共读", scope=umo_b, limit=8)
        assert hits_scoped_miss == []

        hits_engine = plugin.series_search_items("21点", scope="", limit=8)
        assert any(item["kind"] == "娱乐引擎" for item in hits_engine)

        # Journal events
        events = plugin.get_journal_events(persona="anan", scope=umo_a, date="today", limit=10)
        assert len(events) == 1
        ev = events[0]
        assert ev["category"] == "games"
        assert ev["source_label"] == "群间游戏"
        assert ev["target_plugin"] == "astrbot_plugin_dynamics_games"
        assert ev["target_id"] == act_a["id"]
        assert "森林手账共读会" in ev["title"]
        assert ev["occurred_at"]

        old_events = plugin.get_journal_events(scope=umo_a, date="2020-01-01", limit=10)
        assert old_events == []
    finally:
        plugin.store.close()


def test_presence_includes_badge_pulse_and_degrades_safely(tmp_path: pathlib.Path):
    series_panels.clear_summary_cache()
    calls = {"good": 0}

    class GoodGames:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            calls["good"] += 1
            return {
                "badge": {"count": 2, "tone": "info", "label": "2 局进行中"},
                "pulse": {"headline": "2 项协作进行中", "updated_at": "2026-11-21T02:00:00+08:00"},
            }

    class BrokenSibling:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            raise RuntimeError("boom")

    class AsyncSibling:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        async def get_series_summary(self):
            return {"badge": {"count": 99, "tone": "warm", "label": "async"}}

    good_owner = GoodGames()
    broken_owner = BrokenSibling()
    async_owner = AsyncSibling()
    stars = [
        SimpleNamespace(
            name="astrbot_plugin_dynamics_games",
            activated=True,
            star_cls=good_owner,
        ),
        SimpleNamespace(
            name="astrbot_plugin_visual_media",
            activated=True,
            star_cls=broken_owner,
        ),
        SimpleNamespace(
            name="astrbot_plugin_group_chronicle",
            activated=True,
            star_cls=async_owner,
        ),
    ]
    ctx = SimpleNamespace(get_all_stars=lambda: stars)

    p1 = series_panels.presence(ctx, "astrbot_plugin_dynamics_games", root=tmp_path)
    by_id = {row["id"]: row for row in p1["plugins"]}
    assert by_id["astrbot_plugin_chat_dynamics"]["label"] == "对话主控"
    assert by_id["astrbot_plugin_group_chronicle"]["label"] == "群文化沿革"
    assert by_id["astrbot_plugin_dynamics_games"]["badge"] == {
        "count": 2,
        "tone": "info",
        "label": "2 局进行中",
    }
    assert by_id["astrbot_plugin_dynamics_games"]["pulse"]["headline"] == "2 项协作进行中"
    assert by_id["astrbot_plugin_visual_media"]["badge"] is None
    assert by_id["astrbot_plugin_group_chronicle"]["badge"] is None

    # Second call within 10s TTL hits cache
    series_panels.presence(ctx, "astrbot_plugin_dynamics_games", root=tmp_path)
    assert calls["good"] == 1


def test_games_v2_frontend_structure_and_offline_safety():
    html = (PLUGIN_ROOT / "pages" / "games" / "index.html").read_text(encoding="utf-8")
    css = (PLUGIN_ROOT / "pages" / "games" / "style.css").read_text(encoding="utf-8")
    js = (PLUGIN_ROOT / "pages" / "games" / "app.js").read_text(encoding="utf-8")

    # Unified v2 dual-layer shell, zero legacy side-rail
    assert "side-rail" not in html
    assert "side-rail" not in css
    assert "series-workspace" in html
    assert "series-nav" in html
    assert "series-tabs" in html

    # Hoisted primary CTA appears before #tab-home
    assert html.index('id="goto-create-btn"') < html.index('id="tab-home"')
    # Active activities appear before collapsible contract details
    assert html.index('id="home-open-list"') < html.index('id="contract-details"')
    assert "<details id=\"contract-details\"" in html

    # Visual stage stepper & 320px modal fixed footer preserved
    assert "buildStageStepper" in js
    assert ".card-stepper" in css
    assert "grid-template-rows: minmax(0, 1fr) auto" in css

    # Offline safety: zero external CDN links
    for text in (html, css, js):
        assert "https://" not in text and "http://" not in text
