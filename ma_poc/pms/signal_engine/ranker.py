"""SourceRanker — all signal scoring tables in one place.

Replaces the scattered scoring constants in scraper.py:
  _LLM_HINT_SCORE, _EMBEDDED_PORTAL_SCORE, _PMS_PRIOR_SCORE
  _LINK_ANCHOR_KEYWORDS, _LINK_PATH_KEYWORDS, _LINK_HOST_KEYWORDS
  _PMS_SUB_PATH_PRIORS, _UNIVERSAL_SUB_PATH_PRIORS

The ranker is stateless and pure — given a list of SourceSignals it returns
them sorted by composite_score descending. All signal kinds are ranked in the
same call so there is no split between "link ranker" and "API ranker".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from rapidfuzz import fuzz as _fuzz

from ma_poc.pms.signal_engine.models import SourceKind, SourceSignal


@dataclass(frozen=True)
class ScoringTables:
    """All scoring constants in one place.

    Passed to SourceRanker at construction time by the factory in defaults.py.
    Never modified after construction — adding a new signal source means adding
    an entry here and redeploying, not hunting through scraper.py.
    """

    kind_base_scores: dict[SourceKind, int]
    anchor_keywords: tuple[tuple[str, int], ...]
    path_keywords: tuple[tuple[str, int], ...]
    host_keywords: tuple[tuple[str, int], ...]
    pms_priors: dict[str, tuple[str, ...]]
    universal_priors: tuple[str, ...]


@dataclass(frozen=True)
class RankedSignal:
    """A SourceSignal with its computed composite score and reason string."""

    signal: SourceSignal
    composite_score: int
    reason: str


class SourceRanker:
    """Score and sort SourceSignals by their likelihood of yielding unit data.

    Uses ScoringTables for all numeric weights. Pure function — no I/O,
    no state modification.

    Anchor-text matching is semantic: ``rapidfuzz.fuzz.partial_ratio`` returns
    how well the keyword substring aligns with the anchor text (0–100). The
    keyword score is scaled by that ratio so an exact match keeps its full
    value and a near-match is proportionally reduced.

    URL path and host keywords use exact substring matching — those are
    structured strings where fuzzy alignment adds noise, not signal.

    ``fuzzy_threshold`` (default 80) is the minimum similarity required for an
    anchor keyword to contribute any score at all.
    """

    def __init__(self, tables: ScoringTables, fuzzy_threshold: int = 80) -> None:
        self.tables = tables
        self.fuzzy_threshold = fuzzy_threshold

    def rank(self, signals: Iterable[SourceSignal]) -> list[RankedSignal]:
        """Score all signals and return them sorted best-first."""
        return sorted(
            (self._score(s) for s in signals),
            key=lambda r: r.composite_score,
            reverse=True,
        )

    def _score(self, signal: SourceSignal) -> RankedSignal:
        # Profile override wins everything — scores above any LLM hint
        if signal.profile_score_override is not None:
            return RankedSignal(
                signal,
                signal.profile_score_override,
                f"profile_override:{signal.profile_score_override}",
            )

        base = self.tables.kind_base_scores.get(signal.kind, 1000)

        # Links: augment base score with keyword matching
        if signal.kind in (
            SourceKind.INTERNAL_LINK,
            SourceKind.PMS_PRIOR,
            SourceKind.UNIVERSAL_PRIOR,
            SourceKind.EXTERNAL_PORTAL,
        ):
            kw_score = self._score_link(signal)
            return RankedSignal(signal, base + kw_score, f"link:{kw_score}")

        # API responses: boost profile-known endpoints
        if signal.kind == SourceKind.API_RESPONSE:
            boost = 500 if signal.is_known_endpoint else 0
            return RankedSignal(
                signal, base + boost, f"api:known={signal.is_known_endpoint}"
            )

        return RankedSignal(signal, base, f"kind_base:{signal.kind}")

    def _score_link(self, signal: SourceSignal) -> int:
        """Score a link signal by anchor text, URL path, and host keywords.

        Returns the BEST individual keyword score, not a sum. This prevents
        URLs that happen to match many low-value keywords from outranking
        a URL with one strong signal (e.g. ".rentcafe.com" host suffix).

        Anchor scoring is semantic: ``partial_ratio`` aligns the keyword
        substring within the anchor text and returns a 0–100 similarity.
        The keyword's table score is multiplied by that ratio so exact
        matches retain full value and near-matches are proportionally lower.

        Path and host scoring remain exact-substring: those are structured
        tokens where fuzzy alignment introduces false positives.
        """
        best = 0
        url = (signal.url or "").lower()
        anchor = (signal.anchor_text or "").lower()

        # Anchor text — semantic fuzzy match
        if anchor:
            for kw, score in self.tables.anchor_keywords:
                ratio = _fuzz.partial_ratio(kw, anchor)
                if ratio >= self.fuzzy_threshold:
                    best = max(best, int(score * ratio // 100))

        # URL path and host — exact substring (structured strings)
        for kw, score in self.tables.path_keywords:
            if kw in url:
                best = max(best, score)
        for kw, score in self.tables.host_keywords:
            if kw in url:
                best = max(best, score)
        return best
