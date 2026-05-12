"""SourceQualifier — the single place that answers "is this a valid unit source?".

Two cleanly-separated concerns:
  1. MediaTypeFilter: hard gate on content type / URL suffix (fixes RC4).
  2. FieldCombination: declarative minimum-field rules per API shape.
  3. SourceQualifier.qualify(): integrates both with TTL-aware blocked check (fixes RC1).

Zero imports from pms/adapters/ — the factory in defaults.py bridges the
dependency by injecting _UNIT_SIGNAL_KEYS as a parameter (fixes M1 circular
import risk).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal


@dataclass(frozen=True)
class MediaTypeFilter:
    """Hard gate: reject non-data content types before any field analysis.

    Fixes RC4: JS files from cdngeneralmvc.rentcafe.com were passing to
    TIER_6_LLM which correctly returned no units, but consumed budget.
    """

    blocked_content_types: frozenset[str]
    blocked_url_suffixes: frozenset[str]

    def blocks(self, signal: SourceSignal) -> bool:
        ct = (signal.content_type or "").lower()
        if any(ct.startswith(b) for b in self.blocked_content_types):
            return True
        suffix = (signal.url_suffix or "").lower()
        return bool(suffix and suffix in self.blocked_url_suffixes)


@dataclass(frozen=True)
class FieldCombination:
    """Declarative rule: ≥ min_count of ``keys`` must appear in field_keys.

    All keys are stored lowercase; SourceSignal.__post_init__ normalises
    field_keys to lowercase before any comparison runs.

    ``required_groups`` is an optional cross-group constraint: when set, every
    group must contribute at least one matching key in addition to the
    min_count total.  This lets you express "must have ≥1 bed key AND ≥1 bath
    key AND ≥1 area key" without matching three synonyms from the same group.
    Combinations without required_groups behave exactly as before.
    """

    keys: frozenset[str]
    min_count: int
    label: str
    required_groups: tuple[frozenset[str], ...] = ()


@dataclass
class QualificationResult:
    """Outcome of SourceQualifier.qualify()."""

    qualifies: bool
    reason: str
    matched_combination: FieldCombination | None = None


class SourceQualifier:
    """Unified signal qualification — one call to decide if a signal is worth pursuing.

    Gate order (stops at first rejection):
      1. MediaTypeFilter — JS/CSS/font/image → False (fixes RC4)
      2. Blocked endpoint TTL check — re-admit expired / low-evidence blocks (fixes RC1)
      3. FieldCombination match — API_RESPONSE / EMBEDDED_JSON only (fixes RC2 via
         RentCafe unit-level combinations in defaults.py)
      4. All other SourceKinds (links, hints, DOM) → True; scored by SourceRanker
    """

    def __init__(
        self,
        combinations: list[FieldCombination],
        media_filter: MediaTypeFilter,
        blocked_ttl_days: int = 14,
        min_noise_verdicts: int = 2,
    ) -> None:
        self.combinations = combinations
        self.media_filter = media_filter
        self.blocked_ttl_days = blocked_ttl_days
        self.min_noise_verdicts = min_noise_verdicts

    def qualify(self, signal: SourceSignal) -> QualificationResult:
        # Gate 1: media type — hard block regardless of signal kind (fixes RC4)
        if self.media_filter.blocks(signal):
            return QualificationResult(
                False,
                f"media_blocked:{signal.content_type or signal.url_suffix}",
            )

        # Gate 2: blocked endpoint TTL + noise-verdict check (fixes RC1).
        # Only applies when the signal carries profile-state (API_RESPONSE).
        if signal.blocked_at is not None:
            if signal.noise_verdicts < self.min_noise_verdicts:
                # Not enough evidence to sustain the block — re-admit.
                pass
            else:
                try:
                    _now = datetime.now(timezone.utc).replace(tzinfo=None)
                    _ba = signal.blocked_at
                    if hasattr(_ba, "tzinfo") and _ba.tzinfo is not None:
                        _ba = _ba.astimezone(timezone.utc).replace(tzinfo=None)
                    age_days = (_now - _ba).days
                    if age_days < self.blocked_ttl_days:
                        return QualificationResult(
                            False,
                            f"blocked:{age_days}d/{signal.noise_verdicts}v",
                        )
                    # TTL expired → fall through to re-admit
                except Exception:
                    pass

        # Gate 3: field-combination check — only for structured data kinds
        if signal.kind in (SourceKind.API_RESPONSE, SourceKind.EMBEDDED_JSON):
            if not signal.field_keys:
                return QualificationResult(False, "no_field_keys")
            for combo in self.combinations:
                if len(combo.keys & signal.field_keys) < combo.min_count:
                    continue
                # Cross-group check: each required_group must contribute ≥1 key.
                # Prevents "3 bed/bath synonyms" from satisfying a bed+bath+area rule.
                if combo.required_groups and not all(
                    any(k in signal.field_keys for k in grp)
                    for grp in combo.required_groups
                ):
                    continue
                return QualificationResult(True, f"match:{combo.label}", combo)
            return QualificationResult(False, "no_combination_matched")

        # All other kinds (links, hints, DOM sections) qualify here.
        # Their relative importance is handled by SourceRanker.
        return QualificationResult(True, f"non_api:{signal.kind}")

    def qualify_many(
        self, signals: Iterable[SourceSignal]
    ) -> list[tuple[SourceSignal, QualificationResult]]:
        """Qualify a batch of signals, returning all results."""
        return [(s, self.qualify(s)) for s in signals]
