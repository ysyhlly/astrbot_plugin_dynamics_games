"""Question bank loading and unused-index cycling (T6).

Pure stdlib (``json``, ``pathlib``, ``random``, ``typing``) — no astrbot
imports, so this module is fully unit-testable without an AstrBot runtime.

File schema (pinned by the plan): each bank file is a UTF-8 JSON **array of
plain strings**, e.g. ``["问题1", "问题2", ...]``. The ``{puzzle, answer}``
object shape used by other plugins is NOT used here.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import List, Optional, Set

#: Valid ``load_bank`` kinds. Kept as a tuple so callers can validate input.
BANK_KINDS: tuple = ("safe", "unsafe")

#: Resolved relative to THIS module's file, so it works regardless of the
#: process cwd: ``core/banks.py`` -> plugin root -> ``data/banks``.
BANKS_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "banks"


def load_bank(kind: str) -> List[str]:
    """Load the question bank for ``kind`` as a list of strings.

    - ``kind`` must be ``"safe"`` or ``"unsafe"``; anything else -> ``[]``.
    - Missing or malformed files -> ``[]`` (NEVER raises).
    - The JSON must be an array; non-string entries (dicts, numbers, nulls,
      whitespace-only strings, ...) are skipped.
    """
    if kind not in BANK_KINDS:
        return []
    path = BANKS_DIR / f"{kind}_questions.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # File missing/unreadable, or JSON malformed (JSONDecodeError is a
        # ValueError subclass) — treat as an empty bank, never raise.
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str) and item.strip()]


def pick_unused(
    bank: List[str],
    used_indexes: Set[int],
    rng: random.Random,
) -> Optional[str]:
    """Pick a random question whose index is not in ``used_indexes``.

    - When every index of the bank has been used, resets ``used_indexes`` and
      picks again from the full bank (cycling) — so a fresh cycle never
      repeats until the bank is exhausted again.
    - Returns ``None`` only when the bank is empty.
    - Does NOT mutate ``bank``; DOES mutate ``used_indexes`` (the caller owns
      it and persists it per-room).
    """
    if not bank:
        return None
    if len(used_indexes) >= len(bank):
        # Cycle exhausted — start a fresh round.
        used_indexes.clear()
    remaining = [i for i in range(len(bank)) if i not in used_indexes]
    if not remaining:
        # Defensive: stale indices beyond the bank length could otherwise
        # starve the pool; recover by resetting instead of raising.
        used_indexes.clear()
        remaining = list(range(len(bank)))
    idx = rng.choice(remaining)
    used_indexes.add(idx)
    return bank[idx]
