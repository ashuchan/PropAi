"""Data-quality fixes from the 2026-05-31 may13 canary QC.

Three regressions caught by ``residue_analyzer.py`` + the QC scan:

  1. ``inferred_*`` unit_id collisions — 3,924 extra rows across 456
     props because two genuinely-distinct units shared the same plan-
     anchor (cid, fp_name, beds, baths) hash but had different
     area/rent/building. P1 EXACT_DUPE didn't collapse them (their
     fingerprints differed) and the downstream report saw the same
     unit_id N times.

  2. ``floor_plan_name`` case-variant duplication — same plan
     emitted as two distinct rows because adapter pulled both
     uppercase and title-case versions (Holland Residential
     'C2R TWO BEDROOM RENOVATED' + 'C2r Two Bedroom Renovated').

  3. Junk fp_name placeholders — '~' (217), '1 Bed 1 Bath' (213),
     '0 Bed 1 Bath' all surfaced as fp_name values when adapter had
     no real label. Now rejected by ``is_junk_floor_plan``.
"""
from __future__ import annotations


# ─── Fix 1: P3 inferred-id collision suffix ───────────────────


def test_p3_inferred_collision_suffix_disambiguates_same_hash() -> None:
    """Two rows sharing inferred_<hash> but differing on rent/area get
    distinct suffixed IDs so downstream sees them as separate units."""
    from ma_poc.scripts.runners.jugnu import _apply_p3_inferred_id_collision_suffix

    units = [
        {"unit_id": "inferred_abc123", "beds": 1, "baths": 1.0, "area": 700, "rent_low": 1500, "rent_high": 1500, "floor_plan_name": "A1", "building": "", "availability_status": "AVAILABLE"},
        {"unit_id": "inferred_abc123", "beds": 1, "baths": 1.0, "area": 720, "rent_low": 1550, "rent_high": 1550, "floor_plan_name": "A1", "building": "", "availability_status": "AVAILABLE"},
        {"unit_id": "inferred_abc123", "beds": 1, "baths": 1.0, "area": 740, "rent_low": 1600, "rent_high": 1600, "floor_plan_name": "A1", "building": "", "availability_status": "AVAILABLE"},
    ]
    n = _apply_p3_inferred_id_collision_suffix(units)
    assert n == 3
    ids = [u["unit_id"] for u in units]
    assert len(set(ids)) == 3, f"expected 3 distinct ids, got {ids}"
    # All ids must START with the original prefix so the plan-anchor is preserved
    assert all(uid.startswith("inferred_abc123-") for uid in ids)


def test_p3_inferred_unique_id_unchanged() -> None:
    """Solo inferred_<hash> stays put — no suffix added."""
    from ma_poc.scripts.runners.jugnu import _apply_p3_inferred_id_collision_suffix

    units = [{"unit_id": "inferred_unique", "beds": 1, "baths": 1.0, "area": 700, "rent_low": 1500, "rent_high": 1500, "floor_plan_name": "A1", "building": "", "availability_status": "AVAILABLE"}]
    n = _apply_p3_inferred_id_collision_suffix(units)
    assert n == 0
    assert units[0]["unit_id"] == "inferred_unique"


def test_p3_real_unit_ids_never_touched() -> None:
    """P3 must skip non-inferred IDs entirely — real adapter IDs
    (101, A1-203, etc.) get NO suffix even when duplicated."""
    from ma_poc.scripts.runners.jugnu import _apply_p3_inferred_id_collision_suffix

    units = [
        {"unit_id": "101", "beds": 1, "baths": 1.0, "area": 700, "rent_low": 1500, "rent_high": 1500, "floor_plan_name": "A1", "building": "Bldg A", "availability_status": "AVAILABLE"},
        {"unit_id": "101", "beds": 2, "baths": 2.0, "area": 1100, "rent_low": 2200, "rent_high": 2200, "floor_plan_name": "B1", "building": "Bldg B", "availability_status": "AVAILABLE"},
    ]
    n = _apply_p3_inferred_id_collision_suffix(units)
    assert n == 0
    assert units[0]["unit_id"] == "101"
    assert units[1]["unit_id"] == "101"


def test_p3_is_deterministic_across_runs() -> None:
    """Same inputs MUST produce same suffixes (sha-based, no randomness)
    so two runs of the same property produce stable unit_ids."""
    from ma_poc.scripts.runners.jugnu import _apply_p3_inferred_id_collision_suffix

    base = [
        {"unit_id": "inferred_abc", "beds": 1, "baths": 1.0, "area": 700, "rent_low": 1500, "rent_high": 1500, "floor_plan_name": "A1", "building": "", "availability_status": "AVAILABLE"},
        {"unit_id": "inferred_abc", "beds": 1, "baths": 1.0, "area": 720, "rent_low": 1550, "rent_high": 1550, "floor_plan_name": "A1", "building": "", "availability_status": "AVAILABLE"},
    ]
    a = [dict(u) for u in base]
    b = [dict(u) for u in base]
    _apply_p3_inferred_id_collision_suffix(a)
    _apply_p3_inferred_id_collision_suffix(b)
    assert [u["unit_id"] for u in a] == [u["unit_id"] for u in b]


# ─── Fix 2: P0 fp_name canonicalization ───────────────────


def test_p0_fp_name_canonicalization_picks_majority_form() -> None:
    """3× 'A5' + 1× 'a5' → all become 'A5'."""
    from ma_poc.scripts.runners.jugnu import _apply_p0_fp_name_canonicalization

    units = [
        {"floor_plan_id": "fp1", "floor_plan_name": "A5"},
        {"floor_plan_id": "fp1", "floor_plan_name": "A5"},
        {"floor_plan_id": "fp1", "floor_plan_name": "A5"},
        {"floor_plan_id": "fp1", "floor_plan_name": "a5"},
    ]
    n = _apply_p0_fp_name_canonicalization(units)
    assert n == 1
    assert all(u["floor_plan_name"] == "A5" for u in units)


def test_p0_fp_name_tie_prefers_uppercase_prefix() -> None:
    """Equal counts → tie-break to the form with more uppercase in first 3 chars.
    Holland Residential case: 'C2R TWO BEDROOM RENOVATED' vs 'C2r Two Bedroom Renovated'."""
    from ma_poc.scripts.runners.jugnu import _apply_p0_fp_name_canonicalization

    units = [
        {"floor_plan_id": "fp1", "floor_plan_name": "C2R TWO BEDROOM RENOVATED"},
        {"floor_plan_id": "fp1", "floor_plan_name": "C2r Two Bedroom Renovated"},
    ]
    n = _apply_p0_fp_name_canonicalization(units)
    assert n == 1
    assert all(u["floor_plan_name"] == "C2R TWO BEDROOM RENOVATED" for u in units)


def test_p0_fp_name_no_rewrite_when_unique() -> None:
    from ma_poc.scripts.runners.jugnu import _apply_p0_fp_name_canonicalization

    units = [{"floor_plan_id": "fp1", "floor_plan_name": "A5"}]
    assert _apply_p0_fp_name_canonicalization(units) == 0


def test_p0_fp_name_different_fp_ids_independent() -> None:
    """Each floor_plan_id is canonicalized independently — no cross-pollution."""
    from ma_poc.scripts.runners.jugnu import _apply_p0_fp_name_canonicalization

    units = [
        {"floor_plan_id": "fp1", "floor_plan_name": "A5"},
        {"floor_plan_id": "fp1", "floor_plan_name": "a5"},
        {"floor_plan_id": "fp2", "floor_plan_name": "b3"},
        {"floor_plan_id": "fp2", "floor_plan_name": "B3"},
    ]
    _apply_p0_fp_name_canonicalization(units)
    fp1_names = [u["floor_plan_name"] for u in units if u["floor_plan_id"] == "fp1"]
    fp2_names = [u["floor_plan_name"] for u in units if u["floor_plan_id"] == "fp2"]
    assert len(set(fp1_names)) == 1
    assert len(set(fp2_names)) == 1


# ─── Fix 3: junk fp_name patterns ────────────────────────


def test_is_junk_floor_plan_rejects_tilde() -> None:
    """The NEXT_DATA tilde default for missing plan-name."""
    from ma_poc.pms.adapters._parsing import is_junk_floor_plan
    assert is_junk_floor_plan("~") is True
    assert is_junk_floor_plan("~~~") is True


def test_is_junk_floor_plan_rejects_bed_bath_placeholder() -> None:
    """'1 Bed 1 Bath' / '2 Bed 1.5 Bath' style — slug-as-label."""
    from ma_poc.pms.adapters._parsing import is_junk_floor_plan
    assert is_junk_floor_plan("1 Bed 1 Bath") is True
    assert is_junk_floor_plan("2 Bed 2 Bath") is True
    assert is_junk_floor_plan("2 Bed 1.5 Bath") is True
    assert is_junk_floor_plan("0 Bed 1 Bath") is True


def test_is_junk_floor_plan_does_not_reject_real_studio() -> None:
    """'Studio' is a legit plan label when the unit actually IS a studio.
    The junk filter intentionally leaves it alone — the caller layer is
    responsible for setting fp_name=null when the source signal is
    ambiguous, not the regex."""
    from ma_poc.pms.adapters._parsing import is_junk_floor_plan
    assert is_junk_floor_plan("Studio") is False
    assert is_junk_floor_plan("Junior Studio") is False


def test_is_junk_floor_plan_does_not_reject_real_plan_names() -> None:
    """Anchor tests so we don't regress on legit names."""
    from ma_poc.pms.adapters._parsing import is_junk_floor_plan
    for name in ("A1", "The Reserve", "Bali", "1BR-Luxury", "Penthouse 3", "C2R TWO BEDROOM RENOVATED"):
        assert is_junk_floor_plan(name) is False, f"falsely flagged: {name!r}"


# ─── Integration: full pipeline on a realistic mixed input ───


def test_full_emit_pipeline_handles_all_three_fixes() -> None:
    """End-to-end: input has a case-variant pair, an inferred collision,
    and a junk tilde fp_name. After ``_emit_v2_units_for_property``:
      - case variants → canonical form
      - inferred ID collisions → distinct suffixed IDs
      - junk fp_name is detectable via is_junk_floor_plan (filter is
        the caller's job — this test pins the contract that the
        names show up unmodified for now, and is_junk_floor_plan
        recognizes them)."""
    from ma_poc.scripts.runners.jugnu import _emit_v2_units_for_property
    from ma_poc.pms.adapters._parsing import is_junk_floor_plan

    units = [
        # Case-variant pair (same fp_id, different case)
        {"unit_id": "101", "floor_plan_id": "fpA", "floor_plan_name": "A5", "beds": 1, "baths": 1.0, "area": 700, "rent_low": 1500, "rent_high": 1500, "building": "", "availability_status": "AVAILABLE"},
        {"unit_id": "102", "floor_plan_id": "fpA", "floor_plan_name": "a5", "beds": 1, "baths": 1.0, "area": 720, "rent_low": 1550, "rent_high": 1550, "building": "", "availability_status": "AVAILABLE"},
        # Inferred-ID collision (same hash, different area)
        {"unit_id": "inferred_xyz", "floor_plan_id": "fpB", "floor_plan_name": "B1", "beds": 2, "baths": 2.0, "area": 1100, "rent_low": 2100, "rent_high": 2100, "building": "", "availability_status": "AVAILABLE"},
        {"unit_id": "inferred_xyz", "floor_plan_id": "fpB", "floor_plan_name": "B1", "beds": 2, "baths": 2.0, "area": 1150, "rent_low": 2200, "rent_high": 2200, "building": "", "availability_status": "AVAILABLE"},
        # Junk tilde
        {"unit_id": "103", "floor_plan_id": "fpC", "floor_plan_name": "~", "beds": 1, "baths": 1.0, "area": 800, "rent_low": 1700, "rent_high": 1700, "building": "", "availability_status": "AVAILABLE"},
    ]
    _emit_v2_units_for_property(units)

    # Case-variant collapsed to one canonical
    fpA = [u for u in units if u["floor_plan_id"] == "fpA"]
    assert len({u["floor_plan_name"] for u in fpA}) == 1

    # Inferred IDs disambiguated
    fpB = [u for u in units if u["floor_plan_id"] == "fpB"]
    fpB_ids = {u["unit_id"] for u in fpB}
    assert len(fpB_ids) == 2, f"inferred collision not broken: {fpB_ids}"
    assert all(uid.startswith("inferred_xyz-") for uid in fpB_ids)

    # Junk tilde recognized by filter (downstream is_junk_floor_plan
    # can null it; we don't auto-null in the pipeline to preserve
    # backward-compatibility with callers that want raw values)
    fpC = [u for u in units if u["floor_plan_id"] == "fpC"]
    assert is_junk_floor_plan(fpC[0]["floor_plan_name"]) is True
