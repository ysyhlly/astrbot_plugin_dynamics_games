"""Steam Monitor built-in engine core exports."""

from .migration import (
    LEGACY_CONFIG_FIELDS,
    MIGRATION_KV_KEY,
    MIGRATION_SUMMARY_KV_KEY,
    migrate_from_legacy_plugin,
)
from .state_machine import (
    ActiveSession,
    EventKind,
    SessionEvent,
    SessionState,
    aggregate_playtime_window,
    apply_poll_observation,
    day_key_for_ts,
    end_game_tip,
    format_duration,
    natural_day_bounds,
    should_skip_game,
    split_interval_by_natural_day,
    tick_session_deadline,
)
from .store import SteamMonitorStore

__all__ = [
    "ActiveSession",
    "EventKind",
    "LEGACY_CONFIG_FIELDS",
    "MIGRATION_KV_KEY",
    "MIGRATION_SUMMARY_KV_KEY",
    "SessionEvent",
    "SessionState",
    "SteamMonitorStore",
    "aggregate_playtime_window",
    "apply_poll_observation",
    "day_key_for_ts",
    "end_game_tip",
    "format_duration",
    "migrate_from_legacy_plugin",
    "natural_day_bounds",
    "should_skip_game",
    "split_interval_by_natural_day",
    "tick_session_deadline",
]
