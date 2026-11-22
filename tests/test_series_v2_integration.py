"""Focused tests for Dynamics Games shared ProviderBudget, timezone, provider fallback, exclude_groups & quiet_hours."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_games.core.store import ActivityStore
from astrbot_plugin_dynamics_games.engines.truth_game.core.llm_call import (
    LLMUnavailableError,
    call_llm,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.persona_llm import (
    PersonaLLMResponder,
)
from astrbot_plugin_dynamics_games.main import DynamicsGamesPlugin
from astrbot_plugin_dynamics_games.series_panels import (
    is_series_in_quiet_hours,
    resolve_series_timezone,
)


class FakeSharedBudget:
    supports_series_telemetry = True

    def __init__(self, *, busy: bool = False):
        self.busy = busy
        self.calls: list[dict] = []

    async def run(
        self,
        provider: str,
        purpose: str,
        invoke,
        *,
        plugin_id: str = "",
        acquire_timeout: float | None = None,
    ):
        self.calls.append(
            {
                "provider": provider,
                "purpose": purpose,
                "plugin_id": plugin_id,
                "acquire_timeout": acquire_timeout,
            }
        )
        if self.busy:
            err = RuntimeError("queue full")
            err.is_provider_budget_busy = True
            err.provider = provider
            err.purpose = purpose
            err.reason = "queue_full"
            raise err
        val = invoke()
        return await val if asyncio.iscoroutine(val) else val


def _chat_context(
    *,
    reply_provider: str = "series-reply",
    rhythm_timezone: str = "America/New_York",
    exclude_groups: list[str] | None = None,
    in_quiet_hours: bool = False,
    budget=None,
    llm_text: str = "吃拉面吧！",
):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": reply_provider,
                "rhythm_timezone": rhythm_timezone,
                "exclude_groups": exclude_groups or [],
                "quiet_hours": {
                    "daily_rhythm_enabled": True,
                    "in_quiet_hours": in_quiet_hours,
                },
            }

    prov = SimpleNamespace(
        meta=SimpleNamespace(id=reply_provider or "session-p"),
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=llm_text)),
    )
    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=True,
                star_cls=ChatOwner(),
            )
        },
        get_provider_by_id=lambda pid: prov if pid else None,
        get_current_chat_provider_id=AsyncMock(return_value="session-p"),
        get_using_provider=lambda *a, **kw: prov,
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text=llm_text)),
        persona_manager=None,
    )
    if budget is not None:
        ctx._chat_dynamics_provider_budget = budget
    return ctx


@pytest.mark.asyncio
async def test_games_llm_budget_delegation_and_busy_degradation():
    budget = FakeSharedBudget(busy=False)
    ctx = _chat_context(budget=budget, llm_text="今天吃牛肉面！")

    # 1. WhatToEat PersonaLLMResponder routes through shared budget & inherits reply_provider
    wte = PersonaLLMResponder(ctx, enabled=True, provider_id="")
    out = await wte.generate_recommend(
        umo="aiocqhttp:GroupMessage:1001", user_message="吃啥", food="牛肉面"
    )
    assert out == "今天吃牛肉面！"
    assert budget.calls[-1]["plugin_id"] == "astrbot_plugin_dynamics_games"
    assert budget.calls[-1]["provider"] == "series-reply"
    assert budget.calls[-1]["purpose"] == "games_what_to_eat"

    # 2. Truth game call_llm routes through shared budget & inherits reply_provider
    txt = await call_llm(ctx, "aiocqhttp:GroupMessage:1001", "出一道真心话")
    assert txt == "今天吃牛肉面！"
    assert budget.calls[-1]["plugin_id"] == "astrbot_plugin_dynamics_games"
    assert budget.calls[-1]["provider"] == "series-reply"
    assert budget.calls[-1]["purpose"] == "games_truth_game"

    # 3. Busy degradation: WTE returns None (template fallback), Truth raises LLMUnavailableError without retry
    busy = FakeSharedBudget(busy=True)
    ctx._chat_dynamics_provider_budget = busy
    assert (
        await wte.generate_recommend(
            umo="aiocqhttp:GroupMessage:1001", user_message="吃啥", food="牛肉面"
        )
        is None
    )
    busy_calls_before = len(busy.calls)
    with pytest.raises(LLMUnavailableError):
        await call_llm(ctx, "aiocqhttp:GroupMessage:1001", "出一道真心话")
    # Should NOT retry whole sequence on busy
    assert len(busy.calls) == busy_calls_before + 1


@pytest.mark.asyncio
async def test_games_standalone_without_chat_dynamics_and_explicit_override():
    prov = SimpleNamespace(
        meta=SimpleNamespace(id="host-p"),
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="standalone ok")),
    )
    ctx = SimpleNamespace(
        get_all_stars=lambda: {},
        get_provider_by_id=lambda pid: prov if pid in {"explicit-p", "host-p"} else None,
        get_current_chat_provider_id=AsyncMock(return_value="host-p"),
        get_using_provider=lambda *a, **kw: prov,
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="standalone ok")
        ),
        persona_manager=None,
    )

    # Standalone without Chat Dynamics falls back to session/host provider
    txt = await call_llm(ctx, "aiocqhttp:GroupMessage:1001", "prompt")
    assert txt == "standalone ok"
    assert ctx.llm_generate.await_args.kwargs["chat_provider_id"] == "host-p"

    # Explicit override always takes precedence over shared reply_provider
    shared_ctx = _chat_context(reply_provider="series-reply", llm_text="override ok")
    txt2 = await call_llm(
        shared_ctx, "aiocqhttp:GroupMessage:1001", "prompt", provider_id="explicit-p"
    )
    assert txt2 == "override ok"
    assert shared_ctx.llm_generate.await_args.kwargs["chat_provider_id"] == "explicit-p"


@pytest.mark.asyncio
async def test_games_timezone_exclude_groups_and_quiet_hours(tmp_path):
    excluded_umo = "aiocqhttp:GroupMessage:9001"
    allowed_umo = "aiocqhttp:GroupMessage:9002"
    ctx = _chat_context(
        rhythm_timezone="America/New_York",
        exclude_groups=[excluded_umo],
        in_quiet_hours=True,
    )
    plugin = DynamicsGamesPlugin.__new__(DynamicsGamesPlugin)
    plugin.context = ctx
    plugin.raw_config = {
        "enabled": True,
        "activities": {"enabled": True},
        "modules": {"enable_rollpig": True, "enable_what_to_eat": True},
    }
    plugin._ready = True
    plugin._closed = False
    plugin._legacy_conflicts = []
    plugin.store = ActivityStore(tmp_path / "games.sqlite3")
    rp_eng = SimpleNamespace(roll_pig_wake_free=AsyncMock())
    wte_eng = SimpleNamespace(on_what_to_eat=AsyncMock())
    plugin.engines = {"rollpig": rp_eng, "what_to_eat": wte_eng}

    # Timezone inheritance
    tz = resolve_series_timezone(ctx)
    assert str(tz) == "America/New_York"
    iso = plugin._iso_time(
        datetime(2026, 6, 2, 2, 0, tzinfo=timezone.utc).timestamp(), tz=plugin._series_tz()
    )
    assert iso.startswith("2026-06-01T22:00:00")

    # Quiet hours helper reflects shared state
    assert is_series_in_quiet_hours(ctx) is True

    # Scope status reflects 主控排除 for excluded group
    st_ex = plugin.get_series_scope_status(excluded_umo)
    assert st_ex["enabled"] is False
    assert st_ex["excluded_by_core"] is True
    assert "主控排除" in st_ex["summary"]

    st_ok = plugin.get_series_scope_status(allowed_umo)
    assert st_ok["enabled"] is True
    assert st_ok["excluded_by_core"] is False

    # Passive listeners skip excluded group silently
    ev_ex = SimpleNamespace(
        unified_msg_origin=excluded_umo,
        get_group_id=lambda: "9001",
    )
    await plugin.rp_roll_pig_wake_free(ev_ex)
    rp_eng.roll_pig_wake_free.assert_not_awaited()

    async def _collect():
        out = []
        async for item in plugin.wte_on_message(ev_ex):
            out.append(item)
        return out

    assert await _collect() == []
    plugin.store.close()
