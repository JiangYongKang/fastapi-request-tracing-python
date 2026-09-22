"""Deterministic sampling decisions.

Rules (stable across repeated runs for the same trace id):

* slow requests, failed requests (error / timeout / cancelled) are *always*
  sampled, so interesting traces are never dropped;
* everything else is sampled at ``rate`` using a SHA-256 bucket of the trace
  id — the same trace id always lands in the same bucket, so decisions are
  reproducible without any randomness or external state.
"""

from __future__ import annotations

import hashlib


class Sampler:
    def __init__(self, rate: float = 1.0) -> None:
        self.rate = min(max(float(rate), 0.0), 1.0)

    def should_sample(
        self,
        trace_id: str,
        *,
        is_slow: bool = False,
        is_failed: bool = False,
    ) -> bool:
        if is_slow or is_failed:
            return True
        if self.rate >= 1.0:
            return True
        if self.rate <= 0.0:
            return False
        return self.hash_bucket(trace_id) < self.rate

    @staticmethod
    def hash_bucket(trace_id: str) -> float:
        """Map a trace id deterministically to [0.0, 1.0)."""
        digest = hashlib.sha256(trace_id.encode("utf-8")).hexdigest()
        return int(digest[:16], 16) / float(16**16)
