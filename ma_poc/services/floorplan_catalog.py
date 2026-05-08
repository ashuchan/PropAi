"""Per-property floor-plan catalog backed by ``Floorplan-comparisons.csv``.

Loaded once at process start. Exposes per-property canonical floor plans
and a deterministic ``snap()`` method that resolves an extracted unit's
identity to one of those plans.

Hard invariants enforced here:
    H2  sqft equality is strict (`a == b`); no buckets, no thresholds,
        no tolerance windows. The ``-1`` sentinel is treated as null.
    H6  CSV->property-id mapping is verified at module load. If the
        chosen-field mapping is missing or below the coverage threshold,
        the catalog runs in ``low_coverage_bypass`` mode: every snap
        attempt returns ``no_match``-equivalent and the prompt context
        omits the KNOWN FLOOR PLANS block.
    H8  Ambiguous matches fail closed: when the (beds, baths, sqft)
        filter still leaves multiple candidates and the
        ``floor_plan_name`` hint cannot disambiguate them, the snap
        returns ``multiple_candidates_fail_closed`` instead of guessing.
    H11 The known-plans context exposed to prompts is capped at 40
        plans per property (~4 KB / ~1,000 tokens). Truncation is sorted
        by ``beds ASC, sqft ASC`` and is logged via the structured
        logger so the per-run debug file can pick it up.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# H11: cap the number of plans exposed in the prompt context block.
KNOWN_PLANS_PROMPT_LIMIT = 40

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_FLOORPLAN_CSV = _CONFIG_DIR / "Floorplan-comparisons.csv"
_MAPPING_PATH = _CONFIG_DIR / "csv_floorplan_mapping.json"

# Tokens that contribute nothing to floor-plan-name disambiguation. These
# show up in nearly every plan ("the Aspen", "1 BR loft", etc.).
_HINT_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "and", "or",
        "br", "ba", "bd", "bath", "bed", "beds", "baths", "bedroom", "bedrooms",
        "bathroom", "bathrooms", "studio", "loft", "plan", "unit",
    }
)
_HINT_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize_token(s: str) -> str:
    return s.strip().lower()


def _tokenize_hint(s: str | None) -> set[str]:
    """Lowercase tokens with stop-words removed. Empty input → empty set."""
    if not s:
        return set()
    tokens = {t for t in _HINT_TOKEN_RE.findall(str(s).lower()) if t not in _HINT_STOPWORDS}
    return tokens


def _coerce_int(v: Any) -> int | None:
    """Best-effort int coercion. Returns None on failure or empty string."""
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _sqft_present(v: int | None) -> bool:
    """H2: sqft is considered present only when non-null AND not the -1 sentinel."""
    return v is not None and v != -1


@dataclass(frozen=True)
class CanonicalPlan:
    """A single row of ``Floorplan-comparisons.csv``."""

    apartmentid: str
    floorplannumber: int
    description: str
    beds: int
    baths: float
    sqft: int  # may be -1 when the CSV row omits area; treated as null at snap time

    @property
    def floor_plan_id(self) -> str:
        return f"{self.apartmentid}_{self.floorplannumber}"


@dataclass(frozen=True)
class SnapResult:
    """Outcome of ``FloorplanCatalog.snap()`` for a single extracted record."""

    plan: CanonicalPlan | None
    confidence: float
    reason: str  # one of the SNAP_REASON_* values below


SNAP_REASON_EXACT_THREE = "exact_three_field"
SNAP_REASON_UNIQUE_TWO = "unique_two_field"
SNAP_REASON_NAME_DISAMBIG = "name_disambiguation"
SNAP_REASON_MULTI_FAIL_CLOSED = "multiple_candidates_fail_closed"
SNAP_REASON_NO_MATCH = "no_match"
SNAP_REASON_LOW_COVERAGE = "low_coverage_bypass"


class FloorplanCatalog:
    """Read-only, in-memory index of canonical floor plans per property."""

    def __init__(
        self,
        csv_path: Path | None = None,
        mapping_path: Path | None = None,
    ) -> None:
        self._csv_path = Path(csv_path) if csv_path else _FLOORPLAN_CSV
        self._mapping_path = Path(mapping_path) if mapping_path else _MAPPING_PATH
        self._chosen_field: str | None = None
        self._by_property: dict[str, list[CanonicalPlan]] = {}
        self._load()

    # ── Loading ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        # Resolve the property-id column from the verified mapping. If the
        # mapping file is absent or `chosen_field` is null we run in
        # bypass mode (H6) — `_chosen_field` stays None and `_by_property`
        # remains empty, so every snap returns SNAP_REASON_LOW_COVERAGE.
        if not self._mapping_path.exists():
            log.warning(
                "FloorplanCatalog: mapping file %s missing; running in bypass mode",
                self._mapping_path,
            )
            return
        try:
            payload = json.loads(self._mapping_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("FloorplanCatalog: unreadable mapping file (%s); bypassing", exc)
            return

        chosen = payload.get("chosen_field")
        if not isinstance(chosen, str) or not chosen:
            log.warning("FloorplanCatalog: chosen_field is null in mapping; bypassing")
            return

        self._chosen_field = chosen

        if not self._csv_path.exists():
            log.warning(
                "FloorplanCatalog: CSV %s missing; bypassing despite valid mapping",
                self._csv_path,
            )
            self._chosen_field = None
            return

        try:
            with self._csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                required = {"apartmentid", "floorplannumber", "description", "bed", "bath", "area"}
                if not required.issubset(reader.fieldnames or []):
                    log.warning(
                        "FloorplanCatalog: %s missing columns %s; bypassing",
                        self._csv_path.name,
                        required - set(reader.fieldnames or []),
                    )
                    self._chosen_field = None
                    return
                for row in reader:
                    plan = self._row_to_plan(row)
                    if plan is None:
                        continue
                    self._by_property.setdefault(plan.apartmentid, []).append(plan)
        except OSError as exc:
            log.warning("FloorplanCatalog: failed reading %s (%s); bypassing", self._csv_path, exc)
            self._chosen_field = None
            self._by_property.clear()

    @staticmethod
    def _row_to_plan(row: dict[str, str]) -> CanonicalPlan | None:
        apartmentid = (row.get("apartmentid") or "").strip()
        if not apartmentid:
            return None
        fpn = _coerce_int(row.get("floorplannumber"))
        beds = _coerce_int(row.get("bed"))
        baths = _coerce_float(row.get("bath"))
        sqft = _coerce_int(row.get("area"))
        description = (row.get("description") or "").strip()
        # Skip rows that lack the minimum required identity fields.
        if fpn is None or beds is None or baths is None:
            return None
        return CanonicalPlan(
            apartmentid=apartmentid,
            floorplannumber=fpn,
            description=description,
            beds=beds,
            baths=baths,
            # `area` may legitimately be -1 in the source CSV (sentinel for
            # "unknown"). Persisted verbatim and treated as null at snap
            # time so H2 strict equality continues to apply.
            sqft=sqft if sqft is not None else -1,
        )

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def chosen_field(self) -> str | None:
        return self._chosen_field

    @property
    def in_bypass_mode(self) -> bool:
        """True when the catalog has no usable mapping (H6)."""
        return self._chosen_field is None

    def get_plans(self, property_id: str) -> list[CanonicalPlan]:
        """Return canonical plans for this property, or [] if unknown."""
        if self.in_bypass_mode or not property_id:
            return []
        return list(self._by_property.get(str(property_id), []))

    def has_property(self, property_id: str) -> bool:
        return bool(self.get_plans(property_id))

    def known_plans_block(self, property_id: str) -> tuple[str, dict[str, Any]]:
        """Render the KNOWN FLOOR PLANS prompt block for ``property_id``.

        Returns ``(text, meta)``. ``text`` is the empty string when the
        catalog is bypassed or the property has no plans (H6/H11). ``meta``
        captures truncation diagnostics so the caller can log them.
        """
        plans = self.get_plans(property_id)
        meta: dict[str, Any] = {
            "property_id": property_id,
            "plan_count_total": len(plans),
            "plan_count_emitted": 0,
            "truncated": False,
            "bypass": self.in_bypass_mode,
        }
        if not plans:
            return "", meta

        # H11: deterministic ordering. Sort by (beds ASC, sqft ASC, fpn).
        # `-1` (sqft sentinel) sorts before any real value, which keeps
        # truncation behaviour stable when most rows are missing area.
        ordered = sorted(plans, key=lambda p: (p.beds, p.sqft, p.floorplannumber))
        emitted = ordered[:KNOWN_PLANS_PROMPT_LIMIT]
        truncated = len(ordered) > KNOWN_PLANS_PROMPT_LIMIT
        meta["plan_count_emitted"] = len(emitted)
        meta["truncated"] = truncated

        lines = [
            "KNOWN FLOOR PLANS for this property (use these names when matching):"
        ]
        for plan in emitted:
            sqft_part = (
                f"{plan.sqft}sqft" if _sqft_present(plan.sqft) else "sqft unknown"
            )
            baths_repr = (
                f"{int(plan.baths)}" if plan.baths == int(plan.baths) else f"{plan.baths}"
            )
            desc = plan.description or f"plan {plan.floorplannumber}"
            lines.append(f'- "{desc}": {plan.beds}BR/{baths_repr}BA, {sqft_part}')
        if truncated:
            extra = len(ordered) - KNOWN_PLANS_PROMPT_LIMIT
            lines.append(f"... and {extra} more plans (omitted for length)")
            log.info(
                "floorplan_catalog: truncated known-plans for property=%s emitted=%d total=%d",
                property_id,
                len(emitted),
                len(ordered),
            )
        lines.append(
            "\nMatching rule:\n"
            "  - Prefer the name from this list when a unit on the page clearly\n"
            "    matches one of these floor plans by beds + baths + sqft.\n"
            "  - If the page shows a name not in this list, use the page's name\n"
            "    and set floor_plan_source = \"page_label\".\n"
            "  - If the page shows no name at all, derive one from the nearest\n"
            "    section heading or URL slug and set floor_plan_source =\n"
            "    \"section_heading\" or \"url_slug\".\n"
            "  - Never copy a name from this list onto a unit whose beds, baths,\n"
            "    or sqft do not match. If in doubt, use the page's literal label."
        )
        return "\n".join(lines), meta

    def snap(
        self,
        property_id: str,
        beds: int | None,
        baths: float | None,
        sqft: int | None,
        floor_plan_name_hint: str | None,
    ) -> SnapResult:
        """Resolve an extracted record to a CanonicalPlan, if possible.

        Rules (see CLAUDE_PROMPTS_MERGE_RESILIENCE.md §5):
          1. Bypass when the catalog has no mapping.
          2. Filter by (beds, baths) when both extracted values are present.
          3. H2 strict-sqft filter when extracted sqft is present and not -1.
          4. Single survivor → snap with reason ``exact_three_field``
             (or ``unique_two_field`` when sqft was null).
          5. Multiple survivors → use floor_plan_name token-overlap to
             pick a winner (≥0.6 overlap ratio, see _hint_overlap).
          6. Otherwise H8 fail-closed.
        """
        if self.in_bypass_mode:
            return SnapResult(plan=None, confidence=0.0, reason=SNAP_REASON_LOW_COVERAGE)

        plans = self.get_plans(property_id)
        if not plans:
            return SnapResult(plan=None, confidence=0.0, reason=SNAP_REASON_NO_MATCH)

        sqft_int = sqft if isinstance(sqft, int) else _coerce_int(sqft)
        beds_int = beds if isinstance(beds, int) else _coerce_int(beds)
        baths_f = baths if isinstance(baths, (int, float)) else _coerce_float(baths)

        # Filter: beds (when extracted has a real value).
        candidates = list(plans)
        if beds_int is not None:
            candidates = [p for p in candidates if p.beds == beds_int]

        # Filter: baths. Compare as float for safety; the CSV stores floats.
        if baths_f is not None:
            candidates = [p for p in candidates if float(p.baths) == float(baths_f)]

        # H2: strict sqft equality when the extracted value is present.
        sqft_present_extracted = _sqft_present(sqft_int)
        if sqft_present_extracted:
            candidates = [
                p for p in candidates if _sqft_present(p.sqft) and p.sqft == sqft_int
            ]

        if not candidates:
            return SnapResult(plan=None, confidence=0.0, reason=SNAP_REASON_NO_MATCH)

        if len(candidates) == 1:
            reason = SNAP_REASON_EXACT_THREE if sqft_present_extracted else SNAP_REASON_UNIQUE_TWO
            confidence = 0.95 if sqft_present_extracted else 0.85
            return SnapResult(plan=candidates[0], confidence=confidence, reason=reason)

        # Multiple candidates — try name disambiguation.
        winner = self._best_name_match(candidates, floor_plan_name_hint)
        if winner is not None:
            return SnapResult(plan=winner, confidence=0.75, reason=SNAP_REASON_NAME_DISAMBIG)

        # H8: fail closed.
        return SnapResult(plan=None, confidence=0.0, reason=SNAP_REASON_MULTI_FAIL_CLOSED)

    @staticmethod
    def _best_name_match(
        candidates: list[CanonicalPlan],
        hint: str | None,
    ) -> CanonicalPlan | None:
        """Return the candidate whose description best matches ``hint``.

        Uses token-overlap ratio against the hint's content words. Requires
        ratio ≥ 0.6 and a unique winner. Returns ``None`` if no candidate
        clears the bar or multiple tie.
        """
        hint_tokens = _tokenize_hint(hint)
        if not hint_tokens:
            return None
        scored: list[tuple[float, CanonicalPlan]] = []
        for plan in candidates:
            desc_tokens = _tokenize_hint(plan.description)
            if not desc_tokens:
                continue
            overlap = len(hint_tokens & desc_tokens)
            denom = max(len(hint_tokens), 1)
            ratio = overlap / denom
            if ratio >= 0.6:
                scored.append((ratio, plan))
        if not scored:
            return None
        scored.sort(key=lambda t: -t[0])
        # Require a strict winner — ties indicate ambiguity (H8).
        if len(scored) >= 2 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]


# ── Module-level singleton ──────────────────────────────────────────────────

_default_catalog: FloorplanCatalog | None = None
_default_catalog_lock = threading.Lock()


def get_default_catalog() -> FloorplanCatalog:
    """Return the lazily-initialised default catalog.

    The first call loads the CSV; subsequent calls reuse the cached
    instance. The lock makes initialisation safe under thread-pool
    concurrency (jugnu_runner / daily_runner ThreadPoolExecutor) so two
    workers don't each pay the CSV-load cost. Callers wanting a fresh
    catalog (tests, custom paths) must instantiate ``FloorplanCatalog``
    directly.
    """
    global _default_catalog
    if _default_catalog is None:
        with _default_catalog_lock:
            if _default_catalog is None:
                _default_catalog = FloorplanCatalog()
    return _default_catalog


def reset_default_catalog() -> None:
    """Force the next ``get_default_catalog()`` call to reload from disk."""
    global _default_catalog
    with _default_catalog_lock:
        _default_catalog = None
