"""Apartment-number-first identity and merge helpers.

Single source of truth for two questions that every merge pass needs to
answer the same way:

  1. *"What is this row's apartment-level identity?"* — answered by
     :func:`canonical_unit_key`. Prefers the website-extracted
     ``unit_number`` (normalised), falls back to a real (non-inferred)
     ``unit_id``, and returns ``None`` for rows that carry only a
     synthetic ``inferred_*`` identity (so callers can't silently bucket
     on a hash and destructively collapse distinct apartments).

  2. *"How do I combine two rows that describe the same apartment?"* —
     answered by :func:`merge_units`. Field-level fill (primary wins),
     plus identity-provenance protection: a primary chosen for its real
     ``unit_id`` cannot inherit ``_inferred_id=True`` from a secondary,
     and a primary carrying an inferred uid IS promoted to the
     secondary's real uid when one is available for the same apartment.

History — why a separate module:
    Prior to D16 the cross-page dedup pass (``_merge_complementary_fields``
    in ``post_process.py``) inherited every key from secondary, including
    provenance markers. The bug surfaced on the 2026-05-16 canary as
    Montclair emitting 12/12 inferred-uid units even though the underlying
    cascade had real unit_ids — the inferred-id flag from a sibling row
    was poisoning the primary. Centralising the merge contract here
    prevents the same drift recurring at the other ~13 merge call sites
    across post_process, source_merger, generic.py B4, _merge_fns R0a,
    and state_store cross-run dedup.

This module is **pure**: deterministic, no side effects, never raises.
"""

from __future__ import annotations

from typing import Any

from ma_poc.extraction.canonical import (
    BUILDING_KEYS,
    UID_KEYS,
    get_str,
    is_present,
)
from ma_poc.extraction.unit_number import extract_unit_number, normalize_unit_number

#: Provenance / book-keeping keys that must NEVER be inherited from a
#: secondary row by the plain field-level fill loop. ``_inferred_id``
#: is the classic offender — a real-uid primary inheriting
#: ``_inferred_id=True`` gets downgraded to plan-level by
#: ``_has_natural_unit_identity``.
#:
#: ``_date_placeholder`` is special: it's coupled to ``available_date`` /
#: ``availability_date`` (set by the schema-gate when an unparseable
#: date string is observed; both date fields are nulled in the same
#: pass). It must NOT be copied by the field-level loop (which would
#: leave it on primary even when primary just inherited a real date),
#: but it MUST be reconciled with the resulting date state — see the
#: ``_reconcile_date_placeholder`` step at the tail of ``merge_units``.
#:
#: ``unit_id`` is handled separately by :func:`merge_units` (with the
#: real-uid-promotes-inferred rule) and is intentionally excluded from
#: the field-level loop.
_PROVENANCE_KEYS: frozenset[str] = frozenset({
    "_inferred_id",
    "_inferred",
    "_date_placeholder",
})

#: Date field keys that ``_date_placeholder`` is coupled to. When either
#: of these carries a real value, the placeholder must not be present.
#: When both are absent, the placeholder may remain (or be inherited
#: from a secondary that observed the original unparseable string).
_DATE_FIELDS: frozenset[str] = frozenset({
    "available_date",
    "availability_date",
})


def _resolved_uid(unit: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (uid_string, is_inferred) for a unit dict.

    ``uid_string`` is ``None`` when no recognisable uid alias is present.
    ``is_inferred`` is ``True`` when the uid is the synthetic
    ``inferred_<hash>`` form OR the row carries the explicit
    ``_inferred_id=True`` marker.

    This is the SINGLE place that resolves "is this a real apartment
    identifier or a synthetic stand-in?" — every consumer of identity
    should read through this helper rather than re-implementing the
    prefix check.
    """
    uid = get_str(unit, UID_KEYS)
    explicit_inferred = unit.get("_inferred_id") is True
    if uid is None:
        return None, explicit_inferred
    is_inferred = explicit_inferred or uid.startswith("inferred_")
    return uid, is_inferred


def canonical_unit_key(
    unit: dict[str, Any],
    *,
    building_context: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(building_key, identity_key)`` or ``None``.

    Priority order — apartment number wins:

      1. ``normalize_unit_number(extract_unit_number(unit))`` if that
         yields a non-empty key. The extracted apartment number is the
         property's natural per-unit identifier; we trust it above all
         synthetic forms.
      2. A real (non-inferred) ``unit_id``. Used when a producer doesn't
         expose ``unit_number`` but does emit a stable per-unit id
         (e.g. MAA's ULID per-unit ``id``).
      3. ``None`` — the row carries only a synthetic ``inferred_*`` uid
         or no identity at all. Callers MUST NOT silently bucket on the
         inferred hash; doing so collapses distinct apartments that
         happen to share ``(fp_name, beds, baths)``.

    The ``building_key`` (resolved via ``BUILDING_KEYS``) scopes the
    identity so townhome complexes that re-use apartment numbers across
    buildings ("Bldg A #1" vs "Bldg B #1") stay distinct. Pass
    ``building_context`` to inject a building hint when the row itself
    doesn't carry one (e.g. when iterating a building-scoped sub-list).

    Returns:
        ``(building_key, identity_key)`` tuple — both strings, both
        lowercased and stripped, suitable for use as a dict key.
        ``None`` when the row has no usable apartment-level identity.

    Examples:
        >>> canonical_unit_key({"unit_number": "Apt 618"})
        ('', '618')
        >>> canonical_unit_key({"unit_number": "618", "building": "A"})
        ('a', '618')
        >>> canonical_unit_key({"unit_id": "real-618"})  # no unit_number
        ('', 'real-618')
        >>> canonical_unit_key({"unit_id": "inferred_abc"}) is None
        True
        >>> canonical_unit_key({"_inferred_id": True, "unit_id": "x"}) is None
        True
    """
    if not isinstance(unit, dict):
        return None

    # Path 1 — extracted apartment number (the natural per-unit key).
    raw_un = extract_unit_number(unit)
    if raw_un is not None:
        norm = normalize_unit_number(raw_un)
        if norm:
            bldg = get_str(unit, BUILDING_KEYS)
            bldg_key = bldg.strip().lower() if bldg else (building_context or "")
            return (bldg_key, norm)

    # Path 2 — real (non-inferred) unit_id.
    uid, is_inferred = _resolved_uid(unit)
    if uid and not is_inferred:
        bldg = get_str(unit, BUILDING_KEYS)
        bldg_key = bldg.strip().lower() if bldg else (building_context or "")
        return (bldg_key, uid.strip().lower())

    # Path 3 — no usable identity. Caller must NOT bucket on inferred uid.
    return None


def merge_units(
    primary: dict[str, Any],
    secondary: dict[str, Any],
) -> dict[str, Any]:
    """Merge *secondary* into *primary* with identity-provenance protection.

    *primary* is the higher-ranked source. Mutates *primary* in place and
    returns it (caller deep-copies beforehand if it needs the input
    preserved).

    Rules — beyond a plain "fill gaps" merge:

      a. **Provenance keys never inherited.** ``_inferred_id``,
         ``_inferred``, and ``_date_placeholder`` are skipped — a
         real-uid primary cannot be poisoned by a secondary's synthetic
         markers.
      b. **Real uid promotes inferred.** If *primary* carries an
         ``inferred_*`` (or explicitly-inferred) uid AND *secondary*
         carries a real (non-inferred) uid for the same apartment,
         copy the real uid into *primary* and clear ``_inferred_id``.
         This is what closes the door on the 2026-05-16 Montclair
         all-inferred regression: when a sibling row carries the real
         id, the primary inherits it.
      c. **``unit_id`` is identity, not content.** It's never reached
         by the field-level fill loop — the rule above is the only
         path that writes the uid slot.
      d. **Field-level fill is primary-wins.** For every other key, only
         copy the secondary value when *primary* has no present value
         (``is_present`` returns False).

    The function is pure-ish: it mutates *primary* only. *secondary* is
    never written. The two arguments must NOT be the same object.

    Args:
        primary: The higher-ranked unit dict. Mutated and returned.
        secondary: The lower-ranked unit dict. Read-only.

    Returns:
        The mutated *primary* dict.
    """
    if not isinstance(secondary, dict):
        return primary

    # Step 1 — identity promotion. Resolve before the field-level loop so
    # the new uid is in place before any other key copies happen.
    p_uid, p_inf = _resolved_uid(primary)
    s_uid, s_inf = _resolved_uid(secondary)
    if p_inf and not s_inf and s_uid:
        # Secondary has the real uid; promote it onto primary and clear
        # the inferred-id marker. Don't preserve secondary's ``unit_id``
        # wrapper shape — write the resolved string so downstream
        # ``_has_natural_unit_identity`` and ``startswith("inferred_")``
        # checks see a real value regardless of how secondary stored it.
        primary["unit_id"] = s_uid
        if primary.get("_inferred_id") is True:
            primary["_inferred_id"] = False

    # Step 2 — field-level fill, skipping provenance + identity.
    for k, v in secondary.items():
        if k in _PROVENANCE_KEYS:
            continue
        if k == "unit_id":
            continue
        if not is_present(primary.get(k)) and is_present(v):
            primary[k] = v

    # Step 3 — reconcile ``_date_placeholder`` with the resulting date
    # state. The placeholder is meaningful only when BOTH date fields
    # are absent. Four cases:
    #
    #   (a) Primary now has a real date (its own, or inherited from
    #       secondary by Step 2). Drop any stale placeholder — the row
    #       no longer represents "we observed an unparseable string".
    #   (b) Primary still has no real date AND originally carried a
    #       placeholder. Keep it — primary wins on placeholder content.
    #   (c) Primary still has no real date AND originally had no
    #       placeholder AND secondary carries one. Inherit it — the
    #       placeholder is the only signal that the row's missing date
    #       was synthesised, and dropping it would erase that provenance.
    #   (d) Neither side carries a placeholder. No-op.
    _reconcile_date_placeholder(primary, secondary)

    return primary


def _reconcile_date_placeholder(
    primary: dict[str, Any],
    secondary: dict[str, Any],
) -> None:
    """Couple ``_date_placeholder`` with the resulting ``available_date``.

    See :func:`merge_units` for the rule statement. Mutates *primary*.
    """
    has_real_date = any(is_present(primary.get(k)) for k in _DATE_FIELDS)
    if has_real_date:
        # Case (a): real date present → placeholder is stale.
        primary.pop("_date_placeholder", None)
        return
    # Case (b)/(c)/(d). Inherit secondary's placeholder only when
    # primary doesn't already carry one. Never overwrite primary's
    # placeholder with secondary's (primary wins on every conflict).
    if "_date_placeholder" not in primary or not is_present(primary.get("_date_placeholder")):
        sec_placeholder = secondary.get("_date_placeholder")
        if is_present(sec_placeholder):
            primary["_date_placeholder"] = sec_placeholder
