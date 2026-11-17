"""Tests for read-only series shared defaults inheritance in Dynamics Games."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_games.engines.truth_game.core.persona import (
    resolve_system_prompt,
)
from astrbot_plugin_dynamics_games.engines.truth_game.core.question_gen import (
    _inherit_provider as truth_inherit_provider,
)
from astrbot_plugin_dynamics_games.engines.what_to_eat.main import (
    _inherit_provider as eat_inherit_provider,
    _series_defaults as eat_series_defaults,
)


def _chat_context(*, activated: bool = True):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": "series-reply",
                "persona_id": "series-persona",
            }

    persona_manager = SimpleNamespace(
        get_persona=AsyncMock(
            side_effect=lambda pid: SimpleNamespace(system_prompt=f"prompt:{pid}")
        ),
        get_default_persona_v3=AsyncMock(return_value={"prompt": "prompt:default"}),
    )
    return SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=activated,
                star_cls=ChatOwner(),
            )
        },
        persona_manager=persona_manager,
    )


@pytest.mark.asyncio
async def test_games_inherits_series_provider_and_persona():
    ctx = _chat_context()
    assert truth_inherit_provider("", ctx) == "series-reply"
    assert eat_inherit_provider("", ctx) == "series-reply"
    assert eat_series_defaults(ctx)["persona_id"] == "series-persona"

    prompt = await resolve_system_prompt(
        ctx, {"persona_mode": "core_persona", "persona_id": ""}
    )
    assert prompt == "prompt:series-persona"


@pytest.mark.asyncio
async def test_games_explicit_config_takes_priority():
    ctx = _chat_context()
    assert truth_inherit_provider("explicit-p", ctx) == "explicit-p"
    assert eat_inherit_provider("explicit-p", ctx) == "explicit-p"
    prompt = await resolve_system_prompt(
        ctx, {"persona_mode": "core_persona", "persona_id": "explicit-persona"}
    )
    assert prompt == "prompt:explicit-persona"


@pytest.mark.asyncio
async def test_games_safe_fallback_without_chat_dynamics():
    for ctx in (
        SimpleNamespace(
            get_all_stars=lambda: {},
            persona_manager=SimpleNamespace(get_persona=AsyncMock()),
        ),
        _chat_context(activated=False),
    ):
        assert truth_inherit_provider("", ctx) == ""
        assert eat_inherit_provider("", ctx) == ""
        assert eat_series_defaults(ctx) == {}
        prompt = await resolve_system_prompt(
            ctx, {"persona_mode": "core_persona", "persona_id": ""}
        )
        assert prompt is None
