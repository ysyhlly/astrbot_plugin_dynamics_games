"""Typed configuration accessor for the truth game plugin.

Pure dataclass + dict handling: intentionally imports NO astrbot symbols so
this module stays unit-testable without an AstrBot runtime.

The 20 fields mirror ``_conf_schema.json`` exactly. Values are coerced to
their declared types and clamped to safe ranges in ``__post_init__``, so any
``Config`` instance (from ``from_dict`` or direct construction) is guaranteed
to satisfy the plugin invariants.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

TRUE_STRINGS = {"1", "true", "yes", "on"}


def _as_str(raw: Any, default: str) -> str:
    return raw if isinstance(raw, str) else default


def _as_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in TRUE_STRINGS
    if isinstance(raw, (int, float)):
        return bool(raw)
    return default


def _as_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _as_float(raw: Any, default: float) -> float:
    if isinstance(raw, bool):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Clamped, typed view over the plugin's 20 config keys."""

    llm_provider_id: str = ""
    persona_mode: str = "off"
    persona_id: str = ""
    custom_persona_prompt: str = ""
    bot_ask_enabled: bool = True
    bot_ask_frequency: float = 0.5
    content_mode: str = "safe"
    allow_unsafe: bool = False
    question_source: str = "llm_first"
    enable_livingmemory: bool = True
    memory_k: int = 5
    recent_qs_in_prompt: int = 5
    max_players: int = 20
    min_players: int = 2
    waiting_timeout_seconds: int = 600
    round_idle_seconds: int = 300
    max_idle_rounds: int = 3
    max_questions_per_game: int = 30
    llm_timeout_seconds: int = 60
    asker_choice_timeout_seconds: int = 60

    def __post_init__(self) -> None:
        # --- strings ---
        self.llm_provider_id = _as_str(self.llm_provider_id, "")
        self.persona_mode = _as_str(self.persona_mode, "off")
        self.persona_id = _as_str(self.persona_id, "")
        self.custom_persona_prompt = _as_str(self.custom_persona_prompt, "")
        self.content_mode = _as_str(self.content_mode, "safe")
        self.question_source = _as_str(self.question_source, "llm_first")
        # --- bools ---
        self.bot_ask_enabled = _as_bool(self.bot_ask_enabled, True)
        self.allow_unsafe = _as_bool(self.allow_unsafe, False)
        self.enable_livingmemory = _as_bool(self.enable_livingmemory, True)
        # --- floats: clamp to [0.0, 1.0] ---
        self.bot_ask_frequency = min(
            1.0, max(0.0, _as_float(self.bot_ask_frequency, 0.5))
        )
        # --- ints: coerce via int() then clamp ---
        self.memory_k = max(1, _as_int(self.memory_k, 5))
        self.recent_qs_in_prompt = max(0, _as_int(self.recent_qs_in_prompt, 5))
        self.max_idle_rounds = max(1, _as_int(self.max_idle_rounds, 3))
        self.llm_timeout_seconds = max(10, _as_int(self.llm_timeout_seconds, 60))
        self.asker_choice_timeout_seconds = max(1, _as_int(self.asker_choice_timeout_seconds, 60))
        self.min_players = max(2, _as_int(self.min_players, 2))
        # max_players >= min_players (when max < min, max is raised to min)
        self.max_players = max(_as_int(self.max_players, 20), self.min_players)
        self.waiting_timeout_seconds = _as_int(self.waiting_timeout_seconds, 600)
        self.round_idle_seconds = _as_int(self.round_idle_seconds, 300)
        self.max_questions_per_game = _as_int(self.max_questions_per_game, 30)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any] | None) -> "Config":
        """Build a Config from an AstrBot config dict (unknown keys ignored)."""
        if not raw:
            return cls()
        known = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in known})


__all__ = ["Config"]
