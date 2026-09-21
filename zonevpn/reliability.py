"""How each endpoint has behaved over the last few cycles.

## Why one cycle is not enough

The tester's verdict is a snapshot: it builds a real tunnel, measures the delay
and (now) checks that the tunnel gets past the filter. That is a strong result
*at that instant*, and free public nodes are not stable at that timescale. A
node that passes now and fails in four minutes is published anyway, the app
hands it to a user, and the user sees "the server doesn't work" — which is what
prompted this file.

Measured from the app side over one evening: nodes alternated between working
and carrying nothing within minutes, and the ones that had worked before were
markedly better bets than the ones that had merely looked fast. A short history
is therefore worth more than any single measurement, and it costs nothing —
every cycle already tests everything.

## What it does

Each endpoint (`address:port`, stable across cycles even though every config is
renamed and re-encoded every run) keeps the outcome of the last
[window] cycles. From that:

  * `score`  — the fraction that passed.
  * `streak` — consecutive failures, which is what disqualifies a node outright.

A node with no history yet is never blocked: nothing new could ever be
published if it were. It has to *earn* a bad reputation over
`min_samples` cycles before the score is allowed to reject it.

The file is a plain dict on disk, pruned to what the current sources still
contain, so it cannot grow without bound.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List

from . import state

HISTORY_FILE = state.STATE_DIR / "reliability.json"

# Keep each endpoint's last N cycle outcomes.
DEFAULT_WINDOW = 6

# A node may not be rejected on its score until it has this much history.
DEFAULT_MIN_SAMPLES = 3


class Reliability:
    """The per-endpoint history, loaded once per cycle and saved at the end."""

    def __init__(self, data: Dict[str, List[int]], window: int = DEFAULT_WINDOW):
        self._data = data
        self.window = max(1, window)

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, window: int = DEFAULT_WINDOW) -> "Reliability":
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = {}
        data: Dict[str, List[int]] = {}
        if isinstance(raw, dict):
            for key, hist in raw.items():
                if isinstance(hist, list):
                    data[str(key)] = [1 if x else 0 for x in hist][-window:]
        return cls(data, window)

    def save(self) -> None:
        state.ensure_dir()
        text = json.dumps(self._data, ensure_ascii=False)
        # Reuse the collector's atomic write so the dashboard never reads a
        # half-written file.
        state._atomic_write(HISTORY_FILE, text)  # noqa: SLF001

    # -- recording ---------------------------------------------------------
    def record(self, key: str, passed: bool) -> None:
        hist = self._data.setdefault(key, [])
        hist.append(1 if passed else 0)
        if len(hist) > self.window:
            del hist[:-self.window]

    def prune(self, keep: Iterable[str]) -> None:
        """Forget endpoints the sources no longer offer."""
        keep = set(keep)
        for key in [k for k in self._data if k not in keep]:
            del self._data[key]

    # -- reading -----------------------------------------------------------
    def samples(self, key: str) -> int:
        return len(self._data.get(key, []))

    def score(self, key: str) -> float:
        """Fraction of recent cycles this endpoint passed; 1.0 if unknown.

        Unknown is deliberately optimistic — a node nobody has seen yet must be
        allowed its first chance, and it gets a real score after one cycle.
        """
        hist = self._data.get(key)
        if not hist:
            return 1.0
        return sum(hist) / len(hist)

    def fail_streak(self, key: str) -> int:
        hist = self._data.get(key, [])
        streak = 0
        for outcome in reversed(hist):
            if outcome:
                break
            streak += 1
        return streak

    def is_trusted(self, key: str, min_score: float,
                   min_samples: int = DEFAULT_MIN_SAMPLES) -> bool:
        """Whether this endpoint may be published, given it passed this cycle.

        Judged only once there is enough history to judge on; until then the
        current cycle's own verdict stands on its own.
        """
        if self.samples(key) < max(1, min_samples):
            return True
        return self.score(key) >= min_score
