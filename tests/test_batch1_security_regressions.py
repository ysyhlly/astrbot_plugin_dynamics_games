"""Batch 1 security regressions for Dynamics Games.

Covers:
- SteamMonitorEngine `/steam set` hardening: forbids secret/api_base/ssl_ca_file/proxy_url
  updates in group chat, enforces URL whitelist and CA file path restrictions in private chat.
- SteamClient blocks cross-origin redirects on API-keyed requests (ITAD/Steam) so keys
  cannot leak to third-party origins.
- Strict protocol/domain/SSRF validation on `avatar_frame_url` before Playwright rendering.
- GamesWebConsole unified `require_series_admin` + CSRF enforcement on write endpoints.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot_plugin_dynamics_games.core.web_console import GamesWebConsole
from astrbot_plugin_dynamics_games.engines.steam_monitor.renderer import SteamCardRenderer
from astrbot_plugin_dynamics_games.engines.steam_monitor.steam_client import (
    SteamClient,
    sanitize_avatar_frame_url,
    validate_api_base_url,
)
from astrbot_plugin_dynamics_games.tests.test_steam_monitor import (
    FakeEvent,
    _collect,
    _make_engine,
    _reply_text,
)


# ---------------------------------------------------------------------------
# 1. /steam set hardening (group chat blocked, private chat validated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam_set_blocks_sensitive_keys_in_group_chat(tmp_path: pathlib.Path):
    engine, _ = await _make_engine(tmp_path)
    try:
        ev_group_admin = FakeEvent(group_id="10086", sender_id="1", is_admin=True)
        for key, val in (
            ("steam_api_key", "NEW_KEY"),
            ("sgdb_api_key", "SGDB_KEY"),
            ("itad_api_key", "ITAD_KEY"),
            ("steam_api_base", "https://api.steampowered.com"),
            ("steam_store_base", "https://store.steampowered.com"),
            ("sgdb_api_base", "https://www.steamgriddb.com/api/v2"),
            ("itad_api_base", "https://api.isthereanydeal.com"),
            ("ssl_ca_file", "/etc/ssl/certs/ca-certificates.crt"),
            ("proxy_url", "http://127.0.0.1:7890"),
        ):
            before = engine.config.get(key)
            text = _reply_text(await _collect(engine.cmd_set(ev_group_admin, key, val)))
            assert "禁止在群聊中修改敏感配置项" in text
            assert engine.config.get(key) == before
    finally:
        await engine.terminate()


@pytest.mark.asyncio
async def test_steam_set_private_chat_enforces_url_whitelist_and_ca_path(tmp_path: pathlib.Path):
    engine, _ = await _make_engine(tmp_path)
    try:
        ev_priv_admin = FakeEvent(group_id="", sender_id="1", is_admin=True)

        # Valid whitelisted HTTPS base allowed in private chat
        ok_text = _reply_text(
            await _collect(
                engine.cmd_set(ev_priv_admin, "steam_api_base", "https://api.steampowered.com")
            )
        )
        assert "已更新 steam_api_base" in ok_text
        assert engine.config["steam_api_base"] == "https://api.steampowered.com"

        # Untrusted/non-HTTPS/private/metadata/credentialed bases rejected
        for bad_url in (
            "http://api.steampowered.com",
            "https://evil.example.com",
            "https://127.0.0.1",
            "https://169.254.169.254",
            "https://user:pass@api.steampowered.com",
            "file:///etc/passwd",
        ):
            res = _reply_text(
                await _collect(engine.cmd_set(ev_priv_admin, "steam_api_base", bad_url))
            )
            assert "参数值无效" in res

        # ssl_ca_file traversal / wrong extension / outside data_dir rejected
        outside = tmp_path.parent / "outside_ca.pem"
        outside.write_text("dummy", encoding="utf-8")
        for bad_ca in (
            "../../etc/passwd",
            str(tmp_path / "not_cert.txt"),
            str(outside),
        ):
            res = _reply_text(
                await _collect(engine.cmd_set(ev_priv_admin, "ssl_ca_file", bad_ca))
            )
            assert "参数值无效" in res

        # Clearing ssl_ca_file to empty string succeeds
        clr = _reply_text(
            await _collect(engine.cmd_set(ev_priv_admin, "ssl_ca_file", ""))
        )
        assert "已更新 ssl_ca_file" in clr
        assert engine.config["ssl_ca_file"] == ""
    finally:
        await engine.terminate()


def test_validate_api_base_url_enforces_per_service_domains():
    assert validate_api_base_url("itad_api_base", "https://api.isthereanydeal.com/") == "https://api.isthereanydeal.com"
    assert validate_api_base_url("sgdb_api_base", "https://www.steamgriddb.com/api/v2") == "https://www.steamgriddb.com/api/v2"
    with pytest.raises(ValueError):
        validate_api_base_url("itad_api_base", "https://api.steampowered.com")


# ---------------------------------------------------------------------------
# 2. HTTP API-keyed requests never follow cross-origin redirects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_itad_requests_block_cross_origin_redirects_with_api_key(tmp_path: pathlib.Path):
    seen_requests: list[tuple[str, str, bool]] = []

    async def fake_get(url, **kwargs):
        seen_requests.append(("GET", str(url), bool(kwargs.get("follow_redirects"))))
        if "api.isthereanydeal.com" in str(url):
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.example/steal?leak=1"},
                request=httpx.Request("GET", url),
            )
        raise AssertionError("Must NEVER follow cross-origin redirect!")

    async def fake_post(url, **kwargs):
        seen_requests.append(("POST", str(url), bool(kwargs.get("follow_redirects"))))
        if "api.isthereanydeal.com" in str(url):
            return httpx.Response(
                307,
                headers={"Location": "https://attacker.example/steal_post"},
                request=httpx.Request("POST", url),
            )
        raise AssertionError("Must NEVER follow cross-origin redirect!")

    fake_client = SimpleNamespace(get=fake_get, post=fake_post)
    client = SteamClient(
        config_getter=lambda: {"itad_api_key": "SUPER_SECRET_ITAD_KEY"},
        data_dir=tmp_path,
    )
    client._get_http_client = AsyncMock(return_value=fake_client)

    assert await client.itad_lookup_appid("730") is None
    assert await client.itad_search_games("Counter-Strike") == []
    assert await client.itad_get_price_summary("game-123") is None

    assert len(seen_requests) == 3
    assert all(follow is False for _, _, follow in seen_requests)
    assert all("attacker.example" not in url for _, url, _ in seen_requests)


# ---------------------------------------------------------------------------
# 3. Strict avatar_frame_url SSRF/protocol/domain validation before Playwright
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "file://C:/Windows/win.ini",
        "http://127.0.0.1:6185/secret",
        "https://127.0.0.1/frame.png",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/frame.png",
        "https://localhost/frame.png",
        "https://10.0.0.1/frame.png",
        "http://cdn.akamai.steamstatic.com/frame.png",
        "https://evil.example.com/frame.png",
        "https://user:pass@cdn.akamai.steamstatic.com/frame.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
    ],
)
def test_sanitize_avatar_frame_url_rejects_unsafe_urls(bad_url: str):
    assert sanitize_avatar_frame_url(bad_url) == ""


def test_sanitize_avatar_frame_url_accepts_official_steam_cdn():
    valid = "https://shared.fastly.steamstatic.com/community_assets/images/items/123/frame.png"
    assert sanitize_avatar_frame_url(valid) == valid


@pytest.mark.asyncio
async def test_renderer_strips_unsafe_avatar_frame_url_before_playwright(tmp_path: pathlib.Path):
    captured_html: list[str] = []

    async def fake_html_render(html: str, data: dict, options: dict | None = None):
        captured_html.append(html)
        return str(tmp_path / "card.png")

    renderer = SteamCardRenderer(html_render_fn=fake_html_render)
    await renderer.render_game_start(
        {
            "player_name": "Alice",
            "game_name": "CS2",
            "appid": "730",
            "avatar_url": "file:///etc/shadow",
            "avatar_frame_url": "http://169.254.169.254/latest/meta-data/",
            "cover_url": "http://127.0.0.1:6185/internal",
        }
    )
    assert len(captured_html) == 1
    rendered = captured_html[0]
    assert "file:///etc/shadow" not in rendered
    assert "169.254.169.254" not in rendered
    assert "127.0.0.1:6185" not in rendered


@pytest.mark.asyncio
async def test_renderer_sanitizes_all_card_image_urls_before_playwright(tmp_path: pathlib.Path):
    captured_html: list[str] = []

    async def fake_html_render(html: str, data: dict, options: dict | None = None):
        captured_html.append(html)
        return str(tmp_path / "card.png")

    renderer = SteamCardRenderer(html_render_fn=fake_html_render)

    # 1) Achievement card: ach.icon
    await renderer.render_achievement(
        {
            "player_name": "Alice",
            "game_name": "CS2",
            "appid": "730",
            "achievements": [
                {
                    "display_name": "First Blood",
                    "icon": "file:///etc/passwd",
                    "percent": 5.0,
                }
            ],
        }
    )
    # 2) Rank card: entry.avatar_url and entry.top_cover_url
    await renderer.render_rank(
        {
            "entries": [
                {
                    "player_name": "Alice",
                    "total_seconds": 3600,
                    "avatar_url": "http://127.0.0.1:6185/avatar.png",
                    "top_cover_url": "http://169.254.169.254/latest/meta-data/",
                }
            ]
        }
    )
    # 3) Game detail card: header_url
    await renderer.render_game_detail(
        {
            "appid": "730",
            "name": "CS2",
            "header_url": "file:///C:/Windows/win.ini",
        }
    )
    # 4) OpenBox card: recent_games[].cover_url and top_games[].cover_url
    await renderer.render_openbox(
        {
            "steam_id": "76561198000000001",
            "player_name": "Alice",
            "recent_games": [
                {"appid": "730", "name": "CS2", "cover_url": "http://10.0.0.1/recent.jpg"}
            ],
            "top_games": [
                {"appid": "570", "name": "Dota 2", "cover_url": "file:///etc/hosts"}
            ],
        }
    )

    assert len(captured_html) == 4
    joined = "\n".join(captured_html)
    for forbidden in (
        "file:///etc/passwd",
        "127.0.0.1:6185",
        "169.254.169.254",
        "file:///C:/Windows/win.ini",
        "10.0.0.1/recent.jpg",
        "file:///etc/hosts",
    ):
        assert forbidden not in joined


@pytest.mark.asyncio
async def test_store_search_blocks_cross_origin_redirects(tmp_path: pathlib.Path):
    seen: list[tuple[str, bool]] = []

    async def fake_get(url, **kwargs):
        seen.append((str(url), bool(kwargs.get("follow_redirects"))))
        if "store.steampowered.com" in str(url):
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=httpx.Request("GET", url),
            )
        raise AssertionError("Must NEVER follow cross-origin redirect!")

    client = SteamClient(config_getter=lambda: {}, data_dir=tmp_path)
    client._get_http_client = AsyncMock(return_value=SimpleNamespace(get=fake_get))
    assert await client.search_steam_store("Counter-Strike") == []
    assert len(seen) == 2
    assert all(follow is False for _, follow in seen)
    assert all("169.254.169.254" not in url for url, _ in seen)


@pytest.mark.asyncio
async def test_fetch_avatar_frame_url_rejects_malicious_html_src(tmp_path: pathlib.Path):
    async def fake_get(url, **kwargs):
        return httpx.Response(
            200,
            text='<div class="profile_avatar_frame"><img src="file:///etc/passwd"></div>',
            request=httpx.Request("GET", url),
        )

    client = SteamClient(config_getter=lambda: {}, data_dir=tmp_path)
    client._get_http_client = AsyncMock(return_value=SimpleNamespace(get=fake_get))
    assert await client.fetch_avatar_frame_url("76561198000000001") is None


# ---------------------------------------------------------------------------
# 4. GamesWebConsole unified require_series_admin + CSRF enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_games_web_console_enforces_series_admin_and_csrf(monkeypatch):
    import astrbot.dashboard.api.auth as auth_mod
    import astrbot_plugin_dynamics_games.core.web_console as console_mod

    state = {
        "user": "admin",
        "method": "POST",
        "headers": {},
        "body": {"module": "truth_game", "enabled": True},
    }

    class FakeRawRequest:
        @property
        def method(self):
            return state["method"]

        @property
        def headers(self):
            return state["headers"]

    class FakeReq:
        _request = FakeRawRequest()
        username = None
        query = {}

        async def get_json(self):
            return state["body"]

    async def fake_require_user(_raw):
        if not state["user"]:
            raise PermissionError("需要 Dashboard 管理员登录")
        return state["user"]

    captured: list[tuple[int, dict]] = []

    def fake_json_response(payload, status_code: int = 200):
        captured.append((status_code, payload))
        return (status_code, payload)

    monkeypatch.setattr(auth_mod, "require_dashboard_user", fake_require_user)
    monkeypatch.setattr(console_mod, "request", FakeReq())
    monkeypatch.setattr(console_mod, "json_response", fake_json_response)

    registered = []
    fake_plugin = SimpleNamespace(
        VERSION="0.1.2-rc.1",
        config={},
        engines={},
        context=SimpleNamespace(
            get_config=lambda: {"dashboard": {"username": "admin"}},
            get_registered_star=lambda _name: None,
            register_web_api=lambda path, handler, methods, desc: registered.append(
                (path, handler, methods, desc)
            ),
            registered_web_apis=[],
        ),
        store=SimpleNamespace(
            stats_summary=lambda _umo="": {"known_umos": []},
        ),
        coordinator=SimpleNamespace(companion_health=lambda: {}),
        module_enabled=lambda _k: True,
        set_module_enabled=lambda _k, _v: True,
    )
    console = GamesWebConsole(fake_plugin)
    console.register()

    routes = {(p, tuple(m)): h for p, h, m, _ in registered}
    overview_handler = routes[("/astrbot_plugin_dynamics_games/overview", ("GET",))]
    toggle_handler = routes[("/astrbot_plugin_dynamics_games/toggle_module", ("POST",))]

    # 1) GET overview succeeds for valid admin
    state["method"] = "GET"
    status, data = await overview_handler()
    assert status == 200 and data["status"] == "ok"

    # 2) api_key: rejected on GET
    state["user"] = "api_key:leak"
    status, _ = await overview_handler()
    assert status == 403

    # 3) POST without CSRF/Origin headers fails closed with 403
    state["user"] = "admin"
    state["method"] = "POST"
    state["headers"] = {"Host": "dashboard.local"}
    status, _ = await toggle_handler()
    assert status == 403

    # 4) POST with cross-site Sec-Fetch-Site rejected with 403
    state["headers"] = {
        "Host": "dashboard.local",
        "Origin": "https://dashboard.local",
        "Sec-Fetch-Site": "cross-site",
    }
    status, _ = await toggle_handler()
    assert status == 403

    # 5) POST with same-origin headers succeeds
    state["headers"] = {
        "Host": "dashboard.local",
        "Origin": "https://dashboard.local",
        "Sec-Fetch-Site": "same-origin",
    }
    status, data = await toggle_handler()
    assert status == 200 and data["status"] == "ok"
