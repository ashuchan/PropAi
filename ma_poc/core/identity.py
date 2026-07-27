"""Identity fallback — deterministic SHA256-based unit ID when natural key is missing.

Three public surfaces:
  compute_fallback_unit_id  — v2, stable (physical attributes only; NO rent, NO date)
  assign_fallback_unit_id   — write a stable id onto a unit dict in place
  compute_unit_data_sha256  — informational hash of the full unit payload
  compute_fallback_id       — v1, legacy (kept for backward compat, rent-volatile)

RULE: unit identity = what the unit IS physically (floor_plan, beds, baths, sqft).
      Never hash rent or available_date — those are mutable attributes, not
      identity fields.

Normalization (`_n`) is intentionally aggressive — small string differences
between extraction tiers ("2 Bedrooms" vs "2 Bedroom", "A4" vs "A4 ", "1"
vs "1.0", floats with trailing zeros, area sentinel ``-1``) must hash to
the same value or the same physical unit fragments into multiple IDs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from ma_poc.core.source_ids import (
    PER_UNIT_EVIDENCE_KEYS,
    PER_UNIT_IDENTITY_KEYS,
    SourceIdScope,
    normalize_source_id_key,
    scope_of,
)

log = logging.getLogger(__name__)


# ── seen_at_iso — date-anchored ISO timestamp helper ────────────────────────
#
# Lives here (rather than in either store module) so the SQL and FS
# upsert paths share one implementation. The function is generic — any
# caller that wants ``last_seen_at`` rooted at a logical run-date can use
# it.

# Match the strict YYYY-MM-DD prefix. Ten-char prefix is enough for the
# splice; anything more is silently truncated by ``rd[:10]`` so callers
# accidentally passing an ISO timestamp ("2026-05-06T00:00:00") work too.
_RUN_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def seen_at_iso(run_date: str) -> str:
    """Return an ISO timestamp whose date portion is ``run_date``.

    ``last_seen_at`` is the day-level "this unit was observed on this
    run" signal. Production runs typically execute on the same UTC day
    they represent, so wall-clock and run_date agree. Backfills and
    timezone edges can disagree — splicing run_date into the date
    portion keeps day-level queries
    (``substring(last_seen_at, 1, 10) = :rd``) deterministic, while the
    time portion preserves wall-clock for within-run ordering when
    operators inspect the row.

    Falls back to wall-clock when ``run_date`` is empty, malformed, or
    fails the ``YYYY-MM-DD`` prefix check — this guards against
    accidental column corruption from buggy callers (e.g. an empty
    string would otherwise produce ``"Tnn:nn:nn..."``).
    """
    rd = (run_date or "").strip()
    if not _RUN_DATE_PREFIX_RE.match(rd):
        return datetime.now(UTC).isoformat()
    # ISO format is "YYYY-MM-DDTHH:MM:SS.ffffff+00:00" — replace the date
    # with the run-date prefix; everything from index 10 onwards is the
    # time + offset portion that we keep wall-clock-accurate.
    return rd[:10] + datetime.now(UTC).isoformat()[10:]


# Phenotype words emitted by extractors in many surface forms ("Bedrooms",
# "Bedroom", "beds", "bed"). The lambda in :func:`_normalize_token` strips
# the trailing ``s`` so all four hash identically.
_PLURAL_NOISE = re.compile(r"\b(bedrooms?|baths?|bathrooms?|beds?)\b")
# Numeric strings the SQL/JSON layer hands us as ``"1.0"`` / ``"2.00"``;
# canonicalise to ``"1"`` / ``"2"`` so ``int`` and ``float`` representations
# of the same value collapse.
_FLOAT_TRAILING_ZEROS = re.compile(r"^(-?\d+)\.0+$")


def _normalize_token(value: Any) -> str:
    """Aggressive normalization for identity hashing.

    1. Drop ``None``, empty string, and the ``-1`` sentinel (area "unknown").
    2. Lowercase + collapse whitespace.
    3. Normalize numeric strings: ``"1.0"`` → ``"1"``, ``"02"`` → ``"2"``.
    4. Strip plural-s on common phenotype words ("Bedrooms" → "bedroom").
    5. Strip trailing ``"s"`` on standalone tokens (defensive — the merge
       analysis showed the same plan rendered as both ``"3 Bedroom"`` and
       ``"3 Bedrooms"``).
    """
    if value is None or value == "" or value == -1 or value == "-1":
        return ""

    text = str(value).strip().lower()
    if not text:
        return ""

    # Step 3 — int/float canonicalization for numeric scalars.
    m = _FLOAT_TRAILING_ZEROS.match(text)
    if m:
        text = m.group(1)
    elif text.lstrip("-").isdigit():
        # Strip leading zeros: "02" → "2", "-04" → "-4". Preserve "0" itself.
        sign = "-" if text.startswith("-") else ""
        digits = text.lstrip("-").lstrip("0") or "0"
        text = sign + digits

    # Step 2 cont. — collapse internal whitespace.
    text = re.sub(r"\s+", " ", text)

    # Step 4 + 5 — plural normalization. Only fires when the token is a
    # phenotype word; never touches "address" or arbitrary nouns.
    text = _PLURAL_NOISE.sub(lambda m: m.group(1).rstrip("s"), text)

    return text


# Public alias kept for any importer that referenced the older internal helper.
_n = _normalize_token


def compute_fallback_unit_id(unit: dict[str, Any], property_id: str) -> str | None:
    """Compute a stable SHA256-based unit_id from physical attributes only.

    Hash inputs (rent and available_date deliberately excluded):
        property_id | floor_plan_name | beds | baths | sqft_rounded_to_10

    Accepts both v2 canonical field names (``floor_plan_name``, ``beds``,
    ``baths``, ``area``) AND the ``_``-prefixed scrape_properties names
    (``_floor_plan``, ``_bedrooms``, ``_bathrooms``, ``_sqft``) so this
    function can be called from ``scrape_properties._add()`` BEFORE the
    daily_runner ``_`` stripping occurs.

    Returns ``None`` when fewer than 2 non-empty identifying fields are
    present. Prefix ``inferred_`` marks the ID as non-natural.
    """
    floor_plan = (
        _normalize_token(unit.get("floor_plan_name"))
        or _normalize_token(unit.get("_floor_plan"))
        or _normalize_token(unit.get("floor_plan_type"))
        or _normalize_token(unit.get("floorplan_name"))
    )
    beds = (
        _normalize_token(unit.get("beds"))
        or _normalize_token(unit.get("bedrooms"))
        or _normalize_token(unit.get("_bedrooms"))
    )
    baths = (
        _normalize_token(unit.get("baths"))
        or _normalize_token(unit.get("bathrooms"))
        or _normalize_token(unit.get("_bathrooms"))
    )
    sqft_bucket = _bucket_sqft(unit)

    # Require floor_plan AND at least one other identifying field.
    identifying = [floor_plan, beds, baths, sqft_bucket]
    if not floor_plan or sum(1 for p in identifying if p) < 2:
        return None

    parts = [property_id or "", floor_plan, beds, baths, sqft_bucket]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"inferred_{digest}"


def _bucket_sqft(unit: dict[str, Any]) -> str:
    """Return the sqft 10-bucket as a normalized string; ``""`` for absent.

    ``-1`` is the project-wide "area unknown" sentinel and must hash
    identically to ``None`` so the same physical plan with-vs-without sqft
    doesn't fragment into two IDs.
    """
    raw = (
        unit.get("_sqft")
        if unit.get("_sqft") is not None
        else unit.get("area")
        if unit.get("area") is not None
        else unit.get("sqft")
        if unit.get("sqft") is not None
        else unit.get("square_feet")
    )
    if raw is None or raw == -1 or raw == "-1":
        return ""
    try:
        a = int(float(str(raw)))
    except (TypeError, ValueError):
        return ""
    return str(round(a / 10) * 10) if a > 0 else ""


def _last_resort_key(unit: dict[str, Any], property_id: str) -> str | None:
    """Single-anchor fallback when ``compute_fallback_unit_id`` returns None.

    Used when only ``floor_plan_name`` is present (no beds/baths/sqft).
    Hashes ``property_id|fp:floor_plan`` so two captures of the same plan
    name on the same property collide deterministically.

    Returns ``None`` when even the floor plan is missing — the caller
    (:func:`assign_fallback_unit_id`) treats that as truly anchorless and
    skips the record. Note we do NOT fall back to ``unit_number`` here:
    when ``unit_number`` is set, :func:`assign_fallback_unit_id` already
    used it as the natural id at line 1, so this branch is unreachable
    via that path. Surfacing it would also mis-hash literal sentinel
    strings like ``"null"`` that the early existence-check filtered out.
    """
    floor_plan = (
        _normalize_token(unit.get("floor_plan_name"))
        or _normalize_token(unit.get("_floor_plan"))
        or _normalize_token(unit.get("floor_plan_type"))
        or _normalize_token(unit.get("floorplan_name"))
    )
    if not floor_plan:
        return None
    digest = hashlib.sha256(
        f"{property_id or ''}|fp:{floor_plan}".encode("utf-8")
    ).hexdigest()[:16]
    return f"inferred_{digest}"


# Per-unit ids some adapters capture into ``source_ids`` but never promote to
# ``unit_id``. These are STABLE, UNIQUE-PER-UNIT keys — prefer them over the
# phenotype hash (which is rent-unstable when two same-plan units collide, and
# reads as synthetic ``inferred_``).
#
# 2026-07-27: the hand-maintained 6-key tuple that used to live here is gone.
# It had drifted from ``reporting/verdict.PER_UNIT_SOURCE_ID_KEYS`` in BOTH
# directions, and two of its six entries (``entrata_unit_id``,
# ``knock_unit_id``) had no writer anywhere in the repo while ten real per-unit
# keys were in neither list. Membership now comes from the one registry that
# every consumer shares — see ``ma_poc/core/source_ids.py`` for the per-key
# evidence and the "no name-shaped admission rule" argument.


def _source_id_anchor(unit: dict[str, Any]) -> str | None:
    """A stable, unique per-unit id captured in ``source_ids``, or None.

    Returns a provenance-prefixed id (e.g. ``appfolio_listing_id-12345``) so it
    is unambiguous and never collides with a real ``unit_number`` namespace.
    Only the keys in :data:`~ma_poc.core.source_ids.PER_UNIT_IDENTITY_KEYS`
    qualify; the tuple order IS the anchor preference order.

    The prefix is the FULL key name, not ``k.split("_")[0]``. The old truncated
    form was already minting collisions (measured: 3 units across 2 properties
    on 2026-07-12 collided under the then-current 6-key list) and becomes
    structurally broken now that both ``realpage_cws_unit_id`` and
    ``realpage_oll_unit_id`` are admitted — two unrelated backend id namespaces
    would both collapse onto ``realpage-<id>`` and silently merge distinct
    apartments at upsert.

    ⚠ THIS FUNCTION IS UNREACHABLE FROM THE PRODUCTION FORMATTER TODAY.
    ``jugnu._format_v2_unit`` calls :func:`assign_fallback_unit_id` at
    ``jugnu.py:3118`` but does not populate ``out["source_ids"]`` until
    :3134, so the lookup below reads a missing key and returns ``None`` every
    time. A prefix scan across all 228,708 units in the three run-artifact
    sets finds 0 occurrences of ``appfolio-`` / ``apts247-`` / ``entrata-`` /
    ``knock-`` AND 0 of the new full-key form — that is NOT "re-keying churn
    is nil", it is proof the path is dead; 280 rows carrying an
    already-admitted key still shipped ``inferred_``. Widening
    ``PER_UNIT_IDENTITY_KEYS`` therefore changes NOTHING in production until
    the jugnu ordering is fixed. See the MEASURED PRODUCTION IMPACT section of
    ``ma_poc/core/source_ids.py`` for the prerequisite and why it needs its own
    canary. ``test_source_id_anchor_is_inert_in_jugnu`` pins this so the claim
    cannot silently go stale.
    """
    sids = unit.get("source_ids")
    if not isinstance(sids, dict):
        return None
    for k in PER_UNIT_IDENTITY_KEYS:
        v = sids.get(k)
        if v in (None, "", -1, "-1"):
            continue
        s = str(v).strip()
        if s and s.lower() not in {"null", "none"}:
            return f"{k}-{s}"
    return None


def _has_per_unit_evidence(unit: dict[str, Any]) -> bool:
    """True when ``source_ids`` proves *unit* is a single apartment.

    The CLASSIFY view — ``PER_UNIT_EVIDENCE_KEYS`` = UNIT_STABLE u
    UNIT_VOLATILE. Wider than :func:`_source_id_anchor`'s minting view by
    exactly the keys that are unique-within-property but rotate across runs.
    Uniqueness is all a "is this a real apartment?" answer needs; only the
    daily-join key additionally needs stability.
    """
    sids = unit.get("source_ids")
    if not isinstance(sids, dict):
        return False
    for k in PER_UNIT_EVIDENCE_KEYS:
        v = sids.get(k)
        if v in (None, "", -1, "-1"):
            continue
        if str(v).strip() and str(v).strip().lower() not in {"null", "none"}:
            return True
    return False


#: Prefixes minted by this module when a row has no identifying anchor.
SYNTHETIC_ID_PREFIXES: tuple[str, ...] = ("inferred_", "unkeyable_")


def _is_floorplan_surrogate(unit: dict[str, Any], candidate: str) -> bool:
    """Return whether an apparent identity duplicates a plan-level source id.

    Some adapters historically copied a ``floorplanId`` into ``unit_number``.
    Such an id is stable but shared by every apartment on the plan, so it is
    evidence for plan matching only—not a genuine per-apartment anchor.  The
    comparison remains narrow: a normal unit number is rejected only when a
    source-id key explicitly labels the same literal value as a floor-plan id.

    2026-07-27 — the plan scan now consults the registry (20 plan keys)
    instead of suffix-matching (10 keys). Measured cost of the widening: ZERO
    — of 228,708 units across the three run-artifact sets, not one row
    collides with a plan key the suffix test missed. The suffix test is KEPT
    as the backstop for an UNREGISTERED key, so a ``foo_floorplan_id`` landing
    in a hotfix branch before the registry is updated is still caught.

    NO PROVENANCE OVERRIDE HERE — deliberately. An earlier draft opened this
    function with "if the row carries an admitted per-unit anchor, it is not a
    surrogate, return False". That reads as obviously right and is actively
    harmful, because of where this predicate is CONSUMED:
    :func:`assign_fallback_unit_id` resolves ``unit_id or unit_number`` at step
    1 and only reaches :func:`_source_id_anchor` at step 2. Suppressing the
    surrogate verdict therefore makes step 1 WIN — with the floor-plan id —
    and the real per-unit anchor one line later is never consulted. Three
    apartments on plan 5391405 carrying distinct ``realpage_cws_unit_id``s all
    minted the single id ``5391405``: exactly the PR #110 defect, silently
    re-introduced one layer down. Exposure had it shipped: 35,687 rows / 1,389
    properties across the three artifact sets carry BOTH an admitted per-unit
    key and a plan key (sightmap 29,585; entrata 5,540; apts247 435; spherexx
    72; onsite 43; realpage_cws 12), i.e. their #110 guard would have been
    unconditionally off. Benefit measured over the same 228,708 rows: the
    override changed the surrogate verdict on ZERO of them. The intent it was
    reaching for — "a row with a real backend id is a real apartment" — is
    already fully served by :func:`unit_has_real_anchor`'s final line, and
    demoting here is what ROUTES the row to step 2 so it gets
    ``<key>-<id>`` instead of the plan id. Pinned by
    ``test_demotion_routes_the_row_to_its_per_unit_anchor``.

    ACCEPTED FALSE POSITIVE, pinned by test so nobody "fixes" it wrongly:
    property 35256's row ``{"unit_id": "14", "source_ids":
    {"camden_floor_plan_id": 14, "camden_unit_id": 394}}`` — plan E1, $1,739 —
    stays demoted, BY DESIGN, and this PR makes that row strictly worse: it is
    the one row in 228,708 that goes from carrying per-unit evidence to
    carrying none, because ``camden_unit_id`` was in verdict's old 12-key list
    and is registered PLAN here. It IS a real apartment whose unit number
    collides with its plan id by coincidence, but ``camden_unit_id`` is
    genuinely plan-scoped — ``_camden.py:251`` reads ``plan["realPageUnitId"]``
    off the PLAN object under the comment "Plan-level fingerprint shared across
    all units of this plan" and stamps it identically into every emitted row
    (measured: 366 rows / 129 distinct; value rotates 27.2% of joined rows
    between 07-12 and 07-18; unit 394 -> 404 for this very apartment) — so it
    cannot serve as evidence. Using it would collapse unit_ids
    302/14/201/114/202 onto one anchor to rescue one row.

    Residual blast radius, measured: exactly 1 row in each of 2026-07-12 and
    the 07-18 canary, 0 in plancohort, and the PROPERTY-level verdict is
    unchanged in all three (``_units_are_unit_level`` stays True for 35256 —
    its sibling rows carry their own anchors). The correct fix is
    property-scoped — a TRUE plan surrogate makes every sibling row on the plan
    carry the same candidate, and here the siblings carry 302/201/114/202 —
    which needs sibling context threaded into this row-scoped function. Not
    worth it for 1 unit; revisit if the count ever exceeds ~50 in a run.
    """
    source_ids = unit.get("source_ids")
    if not isinstance(source_ids, dict) or not candidate:
        return False
    # Step 3 — registry plan scan, with the legacy suffix test as the
    # unregistered-key backstop.
    for key, value in source_ids.items():
        normalized_key = normalize_source_id_key(key)
        registered = scope_of(normalized_key)
        if registered is SourceIdScope.PLAN:
            pass
        elif registered is None and (
            normalized_key.endswith("floorplan_id")
            or normalized_key.endswith("floor_plan_id")
            or normalized_key.endswith("floorplanid")
        ):
            pass
        else:
            continue
        if str(value or "").strip() == candidate:
            return True
    return False


def unit_has_real_anchor(unit: dict[str, Any]) -> bool:
    """True when *unit* describes ONE REAL APARTMENT rather than a floor plan.

    This is a CLASSIFY question, not a minting question, and since 2026-07-27
    the two have different answers for three keys. Every consumer —
    ``reporting.verdict`` (unit-level vs SUCCESS_PLAN_LEVEL),
    ``pms.scraper.rows_are_plan_level`` (the Path-B retry trigger and the
    universal-recovery gate) — is asking "is this row a real apartment?", for
    which uniqueness-within-property is sufficient. It therefore resolves
    against :data:`~ma_poc.core.source_ids.PER_UNIT_EVIDENCE_KEYS`.

    :func:`assign_fallback_unit_id` asks a STRICTER question — "may this id be
    the daily-join key?" — which additionally needs cross-run stability, so it
    resolves against the narrower ``PER_UNIT_IDENTITY_KEYS``. A row carrying
    only ``appfolio_listing_id`` is consequently classified unit-level here
    (it IS one apartment) while still minting ``inferred_*`` there (the id
    rotates on 14.5% of rows, so anchoring on it would churn the daily join).
    That divergence is deliberate; it is not the two lists drifting again.

    ``unit_id`` is not minted until ``_format_v2_unit`` runs, but the verdict
    layer runs BEFORE the formatter — so a plan-vs-unit test written as
    ``unit_id.startswith("inferred_")`` silently reads an absent field and
    concludes "real identity" for every plan-level row. Measured on the
    2026-07-25 run that mislabelled ~436 plan-level properties as SUCCESS.

    Accepts rows in either shape:
      * pre-format  — identity lives in ``unit_number`` (or ``source_ids``)
      * post-format — identity lives in ``unit_id``, possibly ``inferred_*``
    """
    uid = str(unit.get("unit_id") or "").strip()
    if (
        uid
        and uid.lower() not in {"null", "none"}
        and not uid.startswith(SYNTHETIC_ID_PREFIXES)
        and not _is_floorplan_surrogate(unit, uid)
    ):
        return True
    number = str(unit.get("unit_number") or "").strip()
    if (
        number
        and number.lower() not in {"null", "none"}
        and not _is_floorplan_surrogate(unit, number)
    ):
        return True
    return _has_per_unit_evidence(unit)


def assign_fallback_unit_id(unit: dict[str, Any], property_id: str) -> str | None:
    """Mutate ``unit['unit_id']`` in place with a stable fallback id.

    Resolution order:

      1. Existing non-empty ``unit_id`` / ``unit_number`` — keep it.
      2. A stable per-unit id from ``source_ids`` (captured-but-ignored) —
         e.g. ``appfolio_listing_id``. Real, unique, rent-stable.
      3. ``compute_fallback_unit_id`` — SHA256 of physical attrs.
      4. ``_last_resort_key`` — SHA256 of just floor_plan.
      5. None — unit has no identifying anchor at all.

    Returns the assigned id, or ``None`` when nothing identifiable could be
    derived. Callers that must persist every record (no-drop contract)
    should chain :func:`synthesize_unkeyable_id` for the None case.
    """
    existing = str(
        unit.get("unit_id") or unit.get("unit_number") or ""
    ).strip()
    if (
        existing
        and existing.lower() not in {"null", "none"}
        and not _is_floorplan_surrogate(unit, existing)
    ):
        unit["unit_id"] = existing
        return existing

    # Prefer a captured per-unit source id before phenotype-hashing — it is a
    # real, unique, rent-stable anchor (fixes the synthetic-id + daily-join gap
    # for AppFolio/apts247 units that carry one).
    anchor = _source_id_anchor(unit)
    if anchor:
        unit["unit_id"] = anchor
        return anchor

    derived = compute_fallback_unit_id(unit, property_id) or _last_resort_key(unit, property_id)
    if derived:
        unit["unit_id"] = derived
    return derived


def synthesize_unkeyable_id(unit: dict[str, Any], property_id: str) -> str:
    """Absolute last-resort id for a unit with no identifying anchor.

    Some extractor outputs are pure garbage (empty dicts, only-rent records,
    CMS dropdown options) that have no floor_plan / unit_number / phenotype
    fields. Those records still represent observed events and the no-drop
    contract requires they land in the database — but they cannot be merged
    against each other across runs because there's nothing to merge ON.

    This helper produces a deterministic id from the canonical_id + the full
    payload hash. Properties:

      * Two byte-identical anchorless records on the same property hash to
        the same id — within-batch dedup, cross-run idempotency.
      * Different anchorless payloads → different ids — no false-merging.
      * Always returns a non-empty string. Never raises.

    Mutates ``unit['unit_id']`` in place to mirror :func:`assign_fallback_unit_id`.
    The ``unkeyable_`` prefix lets SQL queries cleanly filter / count these
    rows separately from the ``inferred_`` (semantic-fingerprint) bucket.
    """
    payload_hash = unit.get("data_sha256") or compute_unit_data_sha256(unit)
    digest = hashlib.sha256(
        f"{property_id or ''}|payload:{payload_hash}".encode("utf-8")
    ).hexdigest()[:16]
    uid = f"unkeyable_{digest}"
    unit["unit_id"] = uid
    return uid


def compute_unit_data_sha256(unit: dict[str, Any]) -> str:
    """Hash the full unit payload as a stable, sortable digest.

    Informational only — never compared between rows for identity. Used to
    track silent extractor-level drift (same physical unit, different
    payload) without changing any merge/dedup business rule.

    The hash uses ``json.dumps(sort_keys=True)`` so it's deterministic
    across Python processes (built-in ``hash()`` is not).
    """
    try:
        canonical = json.dumps(unit, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(sorted(unit.items())) if isinstance(unit, dict) else repr(unit)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_fallback_id(record: dict[str, Any]) -> str | None:
    """Legacy v1 fallback — retained for backward compatibility with existing state files.

    WARNING: includes rent in hash (rounded to nearest $25) — rent-volatile.
    Do not use for new code. Migrate callers to compute_fallback_unit_id.

    Hash input tuple:
        (normalised_floor_plan_name, bedrooms, bathrooms, sqft_rounded_10, rent_rounded_25)
    """
    floor_plan = record.get("floor_plan_type") or record.get("floorplan_name")
    bedrooms = record.get("bedrooms") or record.get("beds")

    if not floor_plan:
        return None
    if bedrooms is None:
        return None

    normalised_plan = re.sub(r"\s+", " ", str(floor_plan).strip().lower())
    bathrooms = record.get("bathrooms") or record.get("baths") or 0
    sqft = record.get("sqft") or record.get("square_feet") or 0
    sqft_rounded = _round_to(int(sqft), 10)
    rent = record.get("asking_rent") or record.get("market_rent_low") or record.get("rent") or 0
    rent_rounded = _round_to(int(rent), 25)

    hash_input = (
        normalised_plan,
        str(bedrooms),
        str(bathrooms),
        str(sqft_rounded),
        str(rent_rounded),
    )
    digest = hashlib.sha256("|".join(hash_input).encode()).hexdigest()[:12]
    return f"inferred_{digest}"


def _round_to(value: int, step: int) -> int:
    if step <= 0:
        return value
    return round(value / step) * step


def csv_get(row: dict, *keys: str) -> str:
    """Return the first non-empty value found under any of the given column names.

    Treats ``None``, empty string, ``"null"``, ``"none"``, ``"n/a"``, and
    ``"na"`` as absent.  Used by ``core/schema_v2.build_v2_property`` to
    pull CSV property-level fields by their various header spellings.
    """
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("null", "none", "n/a", "na"):
            return s
    return ""
