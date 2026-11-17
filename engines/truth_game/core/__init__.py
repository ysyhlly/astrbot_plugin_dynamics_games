"""Core logic package for astrbot_plugin_truth_game."""

from .config import Config
from .flow import (
    announce,
    announce_choice,
    announce_round,
    build_advance_hook,
    complete_and_announce,
    maybe_auto_end,
    resolve_bot_question,
    round_plain_text,
    submit_human_question,
)
from .llm_call import LLMUnavailableError, call_llm
from .persona import (
    PERSONA_MODE_CORE,
    PERSONA_MODE_CUSTOM,
    PERSONA_MODE_OFF,
    resolve_system_prompt,
)
from .question_gen import (
    FALLBACK_QUESTIONS,
    NORMALIZE,
    SAFE_BLOCKLIST,
    build_question,
)

__all__ = [
    "Config",
    "announce",
    "announce_choice",
    "announce_round",
    "build_advance_hook",
    "complete_and_announce",
    "maybe_auto_end",
    "resolve_bot_question",
    "round_plain_text",
    "submit_human_question",
    "LLMUnavailableError",
    "call_llm",
    "PERSONA_MODE_OFF",
    "PERSONA_MODE_CORE",
    "PERSONA_MODE_CUSTOM",
    "resolve_system_prompt",
    "FALLBACK_QUESTIONS",
    "NORMALIZE",
    "SAFE_BLOCKLIST",
    "build_question",
]
