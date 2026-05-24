"""Shared data-quality guards called at every adapter's ``try_dom`` unit-emit
boundary.

This is the single source of truth for DQ checks that previously lived
duplicated (or absent) across 21 PMS adapters. Every adapter that
implements the optional ``try_dom`` Protocol method (see
``ma_poc.pms.adapters.base.PmsAdapter``) MUST route its unit records
through ``apply_unit_guards`` (or call the individual guards) before
returning.

Defect cohort coverage (run 2026-05-23 baseline numbers):

  * **A** rent < $500 deposit/fee leak — :func:`is_rent_in_fee_context`
    (90 rows TIER_3_DOM + 161 rows TIER_4_LLM_DOM)
  * **B** same-rent ≥3 plans concession leak — :func:`detect_same_rent_leak`
    (9 props T3 + 32 props LLM_DOM at strict <$1000 gate)
  * **C** non-date strings in ``available_date`` — gated by
    :func:`ma_poc.extraction.dates.looks_date_like` (separate module;
    re-exported here for adapter convenience)
  * **D** ``availability_status=WAITLIST/COMING_SOON/...`` — :func:`canonicalize_status`
    (30 rows LLM_DOM + cross-tier)
  * **E** ``unit_id == floor_plan_name`` + junk header tokens —
    :func:`normalize_unit_id` (94 rows total)
  * **F** ``floor_plan_name`` long+joined > 35 chars — :func:`emit_fpn_long`
    (telemetry only per playbook §T2.B)
  * **G** ``beds=0`` + no "studio" in fpn — :func:`emit_beds_zero_no_studio`
    (telemetry only per playbook §T2.C)
  * sqft = unit-number digits / out-of-bounds — :func:`is_valid_sqft`
    (247 rows / 52 props)

Pure-function module. No side effects except telemetry emits. Never
raises on malformed input.

See ``docs/dom_quality_and_llm_reduction_playbook.md`` Phase 1 for the
full defect taxonomy and ``docs/2026_05_23_root_cause_synthesis`` (in
memory) for the run-level evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ma_poc.extraction.canonical import (
    AVAIL_STATUS_KEYS,
    FP_NAME_KEYS,
    RENT_HI_KEYS,
    RENT_LO_KEYS,
    SQFT_KEYS,
    UID_KEYS,
    get_numeric,
    get_str,
)

# ── Defect A — rent vs deposit/fee context ─────────────────────────────────

#: Words that, when in the immediate context preceding a ``$NNN`` rent
#: match in the source HTML, identify the dollar amount as a fee or
#: deposit rather than rent. The match window is 60 characters before the
#: dollar sign — empirically large enough to catch "<dt>Application Fee</dt>
#: <dd>$50</dd>" shapes and small enough to avoid cross-talk with adjacent
#: real rent values on the same card.
#:
#: Verified canonical: udr.com/savoye (PID 40989) — 21 of 33 units shipped
#: with rent values $200-$500 because the generic dom_scan picked numbers
#: from the "Fees & Deposits" panel. The application_fee / pet_fee /
#: admin_fee / amenity_fee classes are stable across most PMS templates;
#: the words below are the dictionary the cloud-run forensic on PID 40989
#: confirmed.
_FEE_CONTEXT_RE: re.Pattern[str] = re.compile(
    r"(?i)\b("
    r"deposit|deposits|"
    r"fee|fees|admin|administrative|administration|"
    r"application|app[\s\-_]?fee|"
    r"pet|pet[\s\-_]?fee|pet[\s\-_]?deposit|pet[\s\-_]?rent|"
    r"move[\s\-_]?in|move[\s\-_]?out|"
    r"amenity|amenities|"
    r"parking|garage|storage|"
    r"utility|utilities|"
    r"security|reservation|holding|"
    r"trash|valet|cable|internet|water|sewer|electric"
    r")\b"
)


def is_rent_in_fee_context(
    rent: int | float | None,
    source_html: str | None,
    *,
    window: int = 60,
) -> bool:
    """``True`` when the rent value appears in HTML context that
    identifies it as a fee or deposit, not rent.

    Used by both:
      - ``_html_extract._container_yields_unit`` (generic DOM scan) to
        reject `$NNN` matches whose preceding window contains fee-context
        terms.
      - Every adapter's ``try_dom`` that lifts dollar amounts from HTML
        snippets (passed as ``source_html``) — guards against the same
        defect class across all PMS templates.

    Args:
        rent: The candidate rent value. ``None`` returns False (nothing
            to reject).
        source_html: The HTML context the value was extracted from. Pass
            the immediate ancestor element's outerHTML or the card's
            innerHTML. ``None`` returns False (no context to check).
        window: Characters before the dollar sign to inspect. Default 60
            is empirically calibrated; widen with caution as long
            windows pick up cross-talk from adjacent fields.

    Returns:
        ``True`` iff the source_html contains a fee-context term within
        ``window`` characters preceding any occurrence of ``$<rent>`` or
        the unformatted digit string. Returns False when context is
        ambiguous (no surrounding text) so adapters that already
        validated the rent via a stable selector aren't double-blocked.

    Failure modes considered:
      * Legitimate $250 rent in low-income/Section-8 housing where the
        word "rent" appears near a fee panel: not affected — the guard
        looks for fee-context terms ahead of the value, not the rent
        word.
      * "Rent $1450 / Application Fee $50" on one line: the rent
        appearance precedes the fee word, so no false-block.
      * Both fee AND rent on the same line with rent first: rent value
        gets the fee-context check, fee context word appears AFTER the
        rent. The window check only looks BEFORE — so the rent passes.
    """
    if rent is None or source_html is None:
        return False
    try:
        rent_int = int(rent)
    except (ValueError, TypeError):
        return False
    # Try both formatted ($1,450) and plain (1450) since adapter source
    # snippets may have either form.
    formatted = f"${rent_int:,}"
    plain = str(rent_int)
    for needle in (formatted, plain):
        for m in re.finditer(re.escape(needle), source_html):
            start = max(0, m.start() - window)
            context = source_html[start:m.start()]
            if _FEE_CONTEXT_RE.search(context):
                return True
    return False


# ── Defect E + junk-token unit_id normalisation ─────────────────────────────

#: Stop-token set: producer header rows, UI fragments, navigation copy
#: leaking into ``unit_id``. Compared case-insensitively against the
#: stripped value. The list comes from forensic on PIDs 10979 ("Number"),
#: 17586 ("to"), 39782 (junk header). Keep narrow — adding too many
#: tokens risks rejecting legitimate plan codes (e.g. "A" alone is
#: ambiguous and stays admitted).
_JUNK_UNIT_TOKENS: frozenset[str] = frozenset(
    s.lower() for s in (
        # Table headers
        "Number", "Type", "Unit", "Apartment", "Apartment Type",
        "Floor Plan", "Floorplan", "Floor", "Bed", "Beds", "Bath", "Baths",
        "Sqft", "Sq Ft", "Square Feet", "Rent", "Price", "Available",
        "Availability", "Move-In", "Move In", "Lease",
        # UI / nav fragments
        "to", "from", "of", "and", "or", "with",
        "Sign Waitlist", "Add to Waitlist", "Check Availability",
        "Apply Now", "Lease Now", "Tour Now", "Call Now",
        "Click Here", "Click Here for Prices", "Inquire",
        # Single-char stop tokens (a hyphen / hash on its own is junk)
        "#", "-", "—", "–", "/", "|",
    )
)


def normalize_unit_id(
    unit_id: str | None,
    floor_plan_name: str | None = None,
) -> str | None:
    """Return a clean unit_id, or ``None`` if the input is junk.

    Three rejection paths:
      1. The value is a known stop-token (table header or UI fragment) —
         see :data:`_JUNK_UNIT_TOKENS`.
      2. The value (case-insensitively) equals ``floor_plan_name``. This
         catches PIDs like 229986 theadleylife (uid=fpn=A1/A2/A3/B2/C1)
         and 254187 residecrosbyhill (uid=fpn=SCH1/SCH2/...). The plan
         code as unit identity is degenerate per-unit identity.
      3. The value is whitespace, empty, or a single-character
         punctuation token.

    Returns ``None`` on rejection; the caller should route the row
    through ``assign_fallback_unit_id`` (existing ``inferred_*`` SHA256
    path) to get a stable hash identity.

    Args:
        unit_id: Raw unit-id candidate from the adapter.
        floor_plan_name: Sibling floor_plan_name on the same row. Pass
            ``None`` to skip the equality check.

    Returns:
        Stripped non-junk unit_id, or ``None`` if junk.

    Failure modes considered:
      * Property with one unit per plan that legitimately uses plan code
        as unit identity (e.g. "Penthouse A" is both the plan and the
        unit): rare; falls back to inferred id which is correct.
      * Plan code "A" alone: too short to confidently classify as junk
        without context — admitted. Caller's plan-codes-frozenset (in
        ``classify._has_natural_unit_identity``) still demotes it.
    """
    if unit_id is None:
        return None
    s = str(unit_id).strip()
    if not s:
        return None
    if s.lower() in _JUNK_UNIT_TOKENS:
        return None
    if floor_plan_name is not None:
        fpn = str(floor_plan_name).strip().lower()
        if fpn and s.lower() == fpn:
            return None
    # Single-char punctuation
    if len(s) == 1 and not s.isalnum():
        return None
    return s


# ── Defect D — availability_status canonicalisation ────────────────────────

#: Canonical status enum: ``AVAILABLE / UNAVAILABLE / UNKNOWN``. Anything
#: outside is mapped to one of these three with the original variant
#: preserved in a sibling ``_avail_subtype`` field.
#:
#: Map keys are stored uppercase and matched case-insensitively against
#: the stripped raw value. Order doesn't matter (a dict, not a list)
#: because each raw value has exactly one canonical mapping.
_STATUS_CANONICAL_MAP: dict[str, tuple[str, str | None]] = {
    "AVAILABLE": ("AVAILABLE", None),
    "AVAIL": ("AVAILABLE", None),
    "OPEN": ("AVAILABLE", None),
    "VACANT": ("AVAILABLE", None),
    "READY": ("AVAILABLE", None),
    "TRUE": ("AVAILABLE", None),
    "YES": ("AVAILABLE", None),
    "1": ("AVAILABLE", None),
    "UNAVAILABLE": ("UNAVAILABLE", None),
    "UNAVAIL": ("UNAVAILABLE", None),
    "NOT AVAILABLE": ("UNAVAILABLE", None),
    "FALSE": ("UNAVAILABLE", None),
    "NO": ("UNAVAILABLE", None),
    "0": ("UNAVAILABLE", None),
    "WAITLIST": ("UNAVAILABLE", "WAITLIST"),
    "WAIT LIST": ("UNAVAILABLE", "WAITLIST"),
    "WAIT_LIST": ("UNAVAILABLE", "WAITLIST"),
    "WAITLISTED": ("UNAVAILABLE", "WAITLIST"),
    "SIGN WAITLIST": ("UNAVAILABLE", "WAITLIST"),
    "ADD TO WAITLIST": ("UNAVAILABLE", "WAITLIST"),
    "COMING SOON": ("UNAVAILABLE", "COMING_SOON"),
    "COMING_SOON": ("UNAVAILABLE", "COMING_SOON"),
    "FUTURE": ("UNAVAILABLE", "COMING_SOON"),
    "PRELEASING": ("UNAVAILABLE", "COMING_SOON"),
    "PRE-LEASING": ("UNAVAILABLE", "COMING_SOON"),
    "RESERVED": ("UNAVAILABLE", "RESERVED"),
    "PENDING": ("UNAVAILABLE", "PENDING"),
    "ON HOLD": ("UNAVAILABLE", "PENDING"),
    "HELD": ("UNAVAILABLE", "PENDING"),
    "APPLIED": ("UNAVAILABLE", "PENDING"),
    "LEASED": ("UNAVAILABLE", "LEASED"),
    "RENTED": ("UNAVAILABLE", "LEASED"),
    "OCCUPIED": ("UNAVAILABLE", "LEASED"),
    "MODEL UNIT": ("UNAVAILABLE", "MODEL"),
    "MODEL": ("UNAVAILABLE", "MODEL"),
    "DOWN": ("UNAVAILABLE", "OFF_MARKET"),
    "MAINTENANCE": ("UNAVAILABLE", "OFF_MARKET"),
    "OFF MARKET": ("UNAVAILABLE", "OFF_MARKET"),
    "OFF_MARKET": ("UNAVAILABLE", "OFF_MARKET"),
}

#: Sentinel returned for unrecognised input.
_STATUS_UNKNOWN: tuple[str, str | None] = ("UNKNOWN", None)


def canonicalize_status(raw: str | None) -> tuple[str, str | None]:
    """Map a raw availability_status string to the canonical 3-value
    enum plus optional subtype.

    Args:
        raw: Producer string from the adapter. Whitespace-stripped and
            uppercased before lookup. ``None`` / empty returns
            ``("UNKNOWN", None)``.

    Returns:
        ``(canonical_status, subtype_or_None)``. Canonical status is one
        of ``"AVAILABLE"``, ``"UNAVAILABLE"``, ``"UNKNOWN"``. Subtype is
        the original sub-category (``"WAITLIST"``, ``"COMING_SOON"``,
        ``"LEASED"``, ``"MODEL"``, ``"OFF_MARKET"``, ``"RESERVED"``,
        ``"PENDING"``) when applicable, ``None`` otherwise.

    Callers should write the subtype to a sibling field
    ``_avail_subtype`` so downstream consumers can filter by sub-category
    without the canonical 3-value enum being polluted.

    Why this is per-adapter shared: 30 rows in run 2026-05-23 shipped
    ``WAITLIST`` as canonical status, breaking the schema_gate's enum
    check. Both LLM_DOM and Nestin variants emit non-canonical strings
    independently. Centralising the mapping here means one fix covers
    all 21 adapters.
    """
    if raw is None:
        return _STATUS_UNKNOWN
    s = str(raw).strip().upper()
    if not s:
        return _STATUS_UNKNOWN
    return _STATUS_CANONICAL_MAP.get(s, _STATUS_UNKNOWN)


# ── sqft sanity (companion to extraction/sanity.py rent bounds) ─────────────

#: Acceptable sqft range. Lower bound 200 admits tiny-home studios
#: (smallest legitimate units observed: 220 sqft micro-apartments in
#: SF/NYC); upper bound 10000 rejects penthouse mis-reads while admitting
#: legitimate 3-bedroom luxury (largest observed: 5,800 sqft penthouse).
#: Values outside this range are virtually always extractor errors.
SQFT_LOWER_BOUND: int = 200
SQFT_UPPER_BOUND: int = 10_000

#: Regex matching unit-id-shaped strings. When a sqft value is derived
#: from text matching this pattern, it's almost certainly the unit-number
#: digit-strip artefact (the RentCafe profile-selector bug — see
#: ``project_format_area_unit_number_leak`` memory).
_UNIT_ID_SHAPE_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(?:unit|apt|apartment|suite|ste\.?|#)\b"
    r"|[A-Z]-\d"      # "E-5314" RentCafe naming
    r"|\d-[A-Z]"      # "5314-E" mirror
    r"|^#\s*\d"       # "#5314"
)


def is_valid_sqft(
    value: int | float | None,
    source_text: str | None = None,
) -> bool:
    """``True`` iff *value* is a plausible sqft and (when source_text
    provided) is NOT derived from a unit-id-shaped string.

    Used by every adapter's ``try_dom`` to validate sqft before emit,
    AND by :func:`ma_poc.scripts.runners.jugnu._format_area` to reject
    unit-number digit-strip artefacts at the v2 transform boundary.

    Args:
        value: Sqft candidate. ``None`` returns False.
        source_text: The original string the value was extracted from.
            When provided, additional shape check rejects values whose
            source matches the unit-id pattern (catches "Apartment:
            #E-5314" → 5314 leak).

    Returns:
        ``True`` iff ``SQFT_LOWER_BOUND <= value <= SQFT_UPPER_BOUND``
        AND source_text (if provided) does not look like a unit
        identifier.
    """
    if value is None:
        return False
    try:
        v = float(value)
    except (ValueError, TypeError):
        return False
    if not (SQFT_LOWER_BOUND <= v <= SQFT_UPPER_BOUND):
        return False
    if source_text is not None and _UNIT_ID_SHAPE_RE.search(str(source_text)):
        return False
    return True


# ── Defect B — same-rent concession-leak detector ───────────────────────────

#: Threshold below which uniform rent across 3+ plans is suspicious.
#: Above this, uniform pricing IS plausible (some buildings price all 1BRs
#: at one rate point). The strict <$1000 gate catches the canonical
#: deposit-leak ($200-$500) and rent-from-banner-concession ("from $675"
#: → 13 plans all $675) cases.
_SAME_RENT_LEAK_RENT_CAP: int = 1000

#: Minimum count of same-rent plans to fire the guard. With 1-2 plans
#: same-rent is just normal; the leak signature requires 3+.
_SAME_RENT_LEAK_MIN_COUNT: int = 3


def detect_same_rent_leak(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect same-rent concession leak (defect B); null rent on
    matching rows and return the modified list.

    Heuristic (playbook §T1.C):
      * ≥3 rows share the same rent_low value
      * That rent_low is < $1000
      * NONE of the matching rows have a real unit_id (all are
        ``inferred_*`` or ``None`` / missing)

    When all three conditions hit, the rent_low/rent_high on the
    matching rows is nulled (the value is almost certainly a banner /
    deposit value the LLM copied across every plan). The caller is
    expected to route the cleansed rows through ``classify`` again so
    they demote to ``plan_summaries`` (the correct partition for a
    plan-level row with no real rent).

    Args:
        units: List of unit dicts. Function does NOT mutate dicts in
            place — returns a new list with shallow-copied modified
            dicts where rent was nulled. Untouched rows are reference-
            shared with the input.

    Returns:
        List with rent_low/rent_high nulled on rows matching the leak
        signature. Length == len(units); position-preserving.

    Failure modes considered:
      * Uniform-priced low-income housing (rent <$1000, no unit_ids):
        IS the leak signature. Mitigated by the < $1000 cap — Section 8
        housing at $400-$500 IS a real rent regime, but the cohort that
        ships SAME rent for 3+ distinct beds/baths is virtually always
        a banner/deposit leak (no operator prices a 1BR and a 3BR
        identically).
      * Legitimate apartment building with 3 identical 1BRs at one rate
        point AND no unit_id (rare but possible): caught here as false
        positive. Acceptable trade-off — at $1000+ this doesn't fire,
        and below $1000 the false-positive cost is "demoted to plan_
        summary" which downstream still surfaces.
    """
    if not units:
        return units

    rent_counts: Counter[int] = Counter()
    rent_to_indices: dict[int, list[int]] = {}
    for i, u in enumerate(units):
        rl = get_numeric(u, RENT_LO_KEYS)
        if rl is None or rl >= _SAME_RENT_LEAK_RENT_CAP or rl <= 0:
            continue
        rent_int = int(rl)
        rent_counts[rent_int] += 1
        rent_to_indices.setdefault(rent_int, []).append(i)

    suspect_rents: set[int] = {
        r for r, n in rent_counts.items() if n >= _SAME_RENT_LEAK_MIN_COUNT
    }
    if not suspect_rents:
        return units

    out: list[dict[str, Any]] = list(units)
    for rent in suspect_rents:
        indices = rent_to_indices[rent]
        # Test the "no real unit_ids" condition across the matching rows.
        all_inferred_or_none = True
        for i in indices:
            uid = get_str(units[i], UID_KEYS)
            if uid and not uid.startswith("inferred_"):
                all_inferred_or_none = False
                break
        if not all_inferred_or_none:
            continue
        # Fire: null rent on every matching row.
        for i in indices:
            modified = dict(out[i])
            # Null every rent-low / rent-high alias we know about.
            for key in RENT_LO_KEYS:
                if key in modified:
                    modified[key] = None
            for key in RENT_HI_KEYS:
                if key in modified:
                    modified[key] = None
            modified["_same_rent_leak_nulled"] = True
            modified["_same_rent_leak_value"] = rent
            out[i] = modified

    return out


# ── Telemetry-only guards (defects F + G + concession-to-rent monitoring) ──

#: Maximum acceptable floor_plan_name length. Beyond this AND with a
#: joined " - " in the value, it's almost certainly a producer-side
#: concatenation of plan + property + community name (defect F).
_FPN_LONG_THRESHOLD: int = 35


def emit_fpn_long(unit: dict[str, Any], property_id: str = "") -> bool:
    """Telemetry: emit ``extract.floor_plan_name_long`` (INFO) when the
    unit's floor_plan_name exceeds the length threshold AND contains a
    ``" - "`` separator.

    Returns ``True`` when the event was emitted; ``False`` otherwise.
    Adapters can ignore the return value — this is fire-and-forget
    telemetry.

    Playbook §T2.B: collect 2 weeks of telemetry, then write per-template
    strip rules. Don't auto-strip — legit names like
    "Garden View - Upstairs" exist.
    """
    fpn = get_str(unit, FP_NAME_KEYS) or ""
    if len(fpn) <= _FPN_LONG_THRESHOLD or " - " not in fpn:
        return False
    try:
        from ma_poc.observability.events import EventKind, emit
        if hasattr(EventKind, "FLOOR_PLAN_NAME_LONG"):
            emit(
                EventKind.FLOOR_PLAN_NAME_LONG,
                str(property_id),
                fpn=fpn[:120],
                unit_id_hint=str(unit.get("unit_id") or "")[:40],
            )
            return True
    except Exception:
        pass
    return False


def emit_beds_zero_no_studio(
    unit: dict[str, Any],
    property_id: str = "",
) -> bool:
    """Telemetry: emit ``extract.beds_zero_no_studio`` (INFO) when the
    unit has ``beds=0`` AND the floor_plan_name lacks "studio" /
    "efficiency".

    Returns ``True`` when emitted; ``False`` otherwise. Same fire-and-
    forget pattern as :func:`emit_fpn_long`.

    Playbook §T2.C: telemetry-only — auto-renormalising from name is
    risky (``"S3A"`` could be Studio Plan A OR 3-bedroom Plan A —
    unclear without context).
    """
    beds_val = unit.get("beds") if "beds" in unit else unit.get("bedrooms")
    if beds_val is None:
        return False
    try:
        beds = int(float(beds_val))
    except (ValueError, TypeError):
        return False
    if beds != 0:
        return False
    fpn = (get_str(unit, FP_NAME_KEYS) or "").lower()
    if "studio" in fpn or "efficiency" in fpn:
        return False
    try:
        from ma_poc.observability.events import EventKind, emit
        if hasattr(EventKind, "BEDS_ZERO_NO_STUDIO"):
            emit(
                EventKind.BEDS_ZERO_NO_STUDIO,
                str(property_id),
                fpn=fpn[:80],
                unit_id_hint=str(unit.get("unit_id") or "")[:40],
            )
            return True
    except Exception:
        pass
    return False


def emit_concession_to_rent_leak(
    unit: dict[str, Any],
    concession_text: str | None,
    property_id: str = "",
) -> bool:
    """Telemetry: emit ``extract.concession_to_rent_leak`` (INFO) when
    the unit's rent_low equals a dollar amount appearing in
    concession_text.

    Returns ``True`` when emitted; ``False`` otherwise.

    Detects the canonical LLM hallucination shape: PID 11727 shipped 13
    plans all at rent_low=$675, with concession_text="from $675". The
    LLM read the banner and copied the value to BOTH fields. This guard
    surfaces the pattern for monitoring; the rent value is left intact
    here (the actual rent-nulling is done by ``detect_same_rent_leak``
    when the no-real-unit-ids + <$1000 conditions also hit).
    """
    if not concession_text:
        return False
    rl = get_numeric(unit, RENT_LO_KEYS)
    if rl is None:
        return False
    rent_int = int(rl)
    # Search for "$NNN" or "$N,NNN" in concession text matching the rent.
    formatted = f"${rent_int:,}"
    plain = f"${rent_int}"
    if formatted not in concession_text and plain not in concession_text:
        return False
    try:
        from ma_poc.observability.events import EventKind, emit
        if hasattr(EventKind, "CONCESSION_TO_RENT_LEAK"):
            emit(
                EventKind.CONCESSION_TO_RENT_LEAK,
                str(property_id),
                rent=rent_int,
                concession_sample=str(concession_text)[:120],
            )
            return True
    except Exception:
        pass
    return False


# ── Status-non-canonical telemetry (defect D monitoring) ───────────────────

def emit_status_non_canonical(
    raw_status: str | None,
    property_id: str = "",
) -> bool:
    """Telemetry: emit ``extract.status_non_canonical`` when a status
    value falls outside the canonical 3-value set after stripping.

    The canonicalize_status map handles the known set; this telemetry
    captures NEW variants we haven't seen so the enum can grow from
    real data.
    """
    if raw_status is None:
        return False
    s = str(raw_status).strip().upper()
    if not s or s in _STATUS_CANONICAL_MAP or s in ("AVAILABLE", "UNAVAILABLE", "UNKNOWN"):
        return False
    try:
        from ma_poc.observability.events import EventKind, emit
        if hasattr(EventKind, "STATUS_NON_CANONICAL"):
            emit(
                EventKind.STATUS_NON_CANONICAL,
                str(property_id),
                raw_value=s[:80],
            )
            return True
    except Exception:
        pass
    return False


# ── Unified entry point — every try_dom should call this ───────────────────

def apply_unit_guards(
    units: list[dict[str, Any]],
    *,
    property_id: str = "",
    source_html: str | None = None,
    detect_same_rent: bool = True,
) -> list[dict[str, Any]]:
    """Apply ALL applicable DQ guards to a list of unit dicts.

    Designed as the unified call point at every adapter's ``try_dom``
    emit boundary. Does:

      1. ``normalize_unit_id`` — null junk-token + plan-code-equal IDs
      2. ``canonicalize_status`` — map status to canonical 3-value enum
         plus ``_avail_subtype`` sibling field
      3. ``is_valid_sqft`` — null sqft outside bounds or shaped like
         unit_id (when ``_source_text`` is set on the unit dict)
      4. ``is_rent_in_fee_context`` — null rent when source_html
         indicates fee/deposit context
      5. ``detect_same_rent_leak`` — post-process pass over the whole
         list when ``detect_same_rent`` is True
      6. Fires telemetry for F, G, status-non-canonical (best-effort)

    Returns a new list; original dicts are shallow-copied when
    modified, reference-shared when untouched.

    Args:
        units: Adapter's candidate unit list.
        property_id: For telemetry. Empty string is acceptable.
        source_html: Whole-page HTML (or document fragment) the units
            were extracted from. Used by the fee-context guard. Pass
            None to skip the rent-context check (adapters with stable
            field selectors don't need it; the generic dom-scan path
            does).
        detect_same_rent: When False, skip the same-rent leak pass.
            Useful for adapters with one-unit-at-a-time emit paths.

    Returns:
        DQ-guarded list of unit dicts. Same length as input.
    """
    out: list[dict[str, Any]] = []
    for u in units:
        modified = False
        new_u = u

        # 1. unit_id normalisation
        raw_uid = u.get("unit_id") or u.get("unit_number")
        fpn = get_str(u, FP_NAME_KEYS)
        clean_uid = normalize_unit_id(raw_uid, fpn)
        if clean_uid != raw_uid:
            if not modified:
                new_u = dict(u); modified = True
            if clean_uid is None:
                new_u["unit_id"] = None
                new_u["_inferred_id"] = True
            else:
                new_u["unit_id"] = clean_uid

        # 2. status canonicalisation
        raw_status = get_str(u, AVAIL_STATUS_KEYS)
        if raw_status is not None:
            canonical, subtype = canonicalize_status(raw_status)
            if canonical != raw_status:
                emit_status_non_canonical(raw_status, property_id)
                if not modified:
                    new_u = dict(u); modified = True
                new_u["availability_status"] = canonical
                if subtype is not None:
                    new_u["_avail_subtype"] = subtype

        # 3. sqft sanity
        sqft_val = get_numeric(u, SQFT_KEYS)
        if sqft_val is not None:
            sqft_source = u.get("_source_text") or u.get("unit_id") or u.get("unit_number")
            if not is_valid_sqft(sqft_val, sqft_source if isinstance(sqft_source, str) else None):
                if not modified:
                    new_u = dict(u); modified = True
                # Null only the canonical sqft alias we know was set.
                for key in SQFT_KEYS:
                    if key in new_u:
                        new_u[key] = None

        # 4. rent in fee context (only when source_html provided)
        if source_html is not None:
            rl = get_numeric(u, RENT_LO_KEYS)
            if rl is not None and is_rent_in_fee_context(rl, source_html):
                if not modified:
                    new_u = dict(u); modified = True
                for key in RENT_LO_KEYS:
                    if key in new_u:
                        new_u[key] = None
                for key in RENT_HI_KEYS:
                    if key in new_u:
                        new_u[key] = None
                new_u["_fee_context_rent_rejected"] = True

        # Telemetry-only
        emit_fpn_long(u, property_id)
        emit_beds_zero_no_studio(u, property_id)

        out.append(new_u)

    # 5. Same-rent leak (whole-list pass)
    if detect_same_rent:
        out = detect_same_rent_leak(out)

    return out


# Re-export looks_date_like for adapter convenience.
from ma_poc.extraction.dates import looks_date_like as looks_date_like  # noqa: E402,F401

__all__ = [
    "apply_unit_guards",
    "canonicalize_status",
    "detect_same_rent_leak",
    "emit_beds_zero_no_studio",
    "emit_concession_to_rent_leak",
    "emit_fpn_long",
    "emit_status_non_canonical",
    "is_rent_in_fee_context",
    "is_valid_sqft",
    "looks_date_like",
    "normalize_unit_id",
    "SQFT_LOWER_BOUND",
    "SQFT_UPPER_BOUND",
]
