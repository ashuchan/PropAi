"""Unit tests for ``scripts.diagnostics.backfill_property_soft_fields``.

The end-to-end UPDATE path requires a live Postgres connection — those
checks live in integration tests. This file pins the pure helpers (query
builder, value selector, CSV loader, CLI parser) so behavioural changes
get caught before they hit the DB.

Specifically validates the gap fixes shipped 2026-05-18:
  - V1 Title-Case alias COALESCE in the snapshot CTE
  - CSV fallback for snapshot-aged-out rows
  - SUCCESS-verdict filter
  - `--limit` and `--cid` scoping
  - source-priority ordering (snapshot > extra > csv)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.diagnostics import backfill_property_soft_fields as bf


# ── _build_query ─────────────────────────────────────────────────────────────


def test_query_includes_v1_alias_coalesce_for_proj_name() -> None:
    """Snapshot CTE must COALESCE the V2 key with the V1 Title-Case alias.

    Without this, snapshots written by the legacy pipeline (which emitted
    payload={"Property Name": ...}) would silently fail to surface a
    backfill candidate.
    """
    sql, _ = bf._build_query(
        ("proj_name",),
        cid_filter=None,
        limit=None,
        require_success_verdict=False,
    )
    assert "ps.payload->>'proj_name'" in sql
    assert "ps.payload->>'Property Name'" in sql
    assert "COALESCE(" in sql


def test_query_falls_back_to_v1_alias_when_known() -> None:
    """Every backfillable field with a known V1 alias must reference both
    keys; fields without aliases must not (avoids COALESCE of two NULLs)."""
    for f, alias in bf._V1_ALIAS.items():
        if not alias:
            continue
        sql, _ = bf._build_query(
            (f,), cid_filter=None, limit=None, require_success_verdict=False,
        )
        assert f"ps.payload->>'{f}'" in sql
        assert f"ps.payload->>'{alias}'" in sql, (
            f"field {f!r} missing V1 alias {alias!r} in CTE SQL"
        )


def test_query_omits_v1_alias_for_fields_without_one() -> None:
    """``website_design`` and ``concessions`` have no V1 Title-Case key —
    the CTE should use the bare V2 expression."""
    sql, _ = bf._build_query(
        ("website_design", "concessions"),
        cid_filter=None,
        limit=None,
        require_success_verdict=False,
    )
    # Each field appears in its own CTE; no surprise COALESCE.
    assert "NULLIF(TRIM(ps.payload->>'website_design'), '')" in sql
    assert "NULLIF(TRIM(ps.payload->>'concessions'), '')" in sql


def test_query_includes_extra_json_fallback() -> None:
    """``properties.extra`` JSON is the second source (CSV-passthrough)."""
    sql, _ = bf._build_query(
        ("proj_name",),
        cid_filter=None,
        limit=None,
        require_success_verdict=False,
    )
    assert "p.extra->>'proj_name'" in sql
    assert "extra_proj_name" in sql


def test_query_applies_cid_filter() -> None:
    sql, params = bf._build_query(
        ("proj_name",),
        cid_filter=("12586", "12963"),
        limit=None,
        require_success_verdict=False,
    )
    assert "p.canonical_id IN :cids" in sql
    assert params["cids"] == ["12586", "12963"]


def test_query_applies_limit() -> None:
    sql, _ = bf._build_query(
        ("proj_name",),
        cid_filter=None,
        limit=25,
        require_success_verdict=False,
    )
    assert "LIMIT 25" in sql


def test_query_no_limit_when_none() -> None:
    sql, _ = bf._build_query(
        ("proj_name",),
        cid_filter=None,
        limit=None,
        require_success_verdict=False,
    )
    assert "LIMIT" not in sql


def test_query_applies_success_verdict_filter() -> None:
    """With --require-success-verdict, the CTE filters to success-class
    snapshots only (or snapshots with no verdict at all)."""
    sql, _ = bf._build_query(
        ("proj_name",),
        cid_filter=None,
        limit=None,
        require_success_verdict=True,
    )
    assert "_meta" in sql and "verdict" in sql
    # All three success verdicts present (mirrors Python _SUCCESS_VERDICTS).
    for v in ("SUCCESS", "SUCCESS_PLAN_LEVEL", "SUCCESS_PARTIAL"):
        assert v in sql


def test_query_omits_verdict_filter_by_default() -> None:
    sql, _ = bf._build_query(
        ("proj_name",),
        cid_filter=None,
        limit=None,
        require_success_verdict=False,
    )
    # No verdict mention in the WHERE / CTE filter.
    assert "verdict" not in sql


# ── _select_value (source priority) ──────────────────────────────────────────


def test_select_value_prefers_snapshot_over_extra_and_csv() -> None:
    row = {
        "canonical_id": "P1",
        "new_proj_name": "From Snapshot",
        "extra_proj_name": "From Extra",
    }
    csv_lookup = {"P1": {"proj_name": "From CSV"}}
    value, source = bf._select_value(row, "proj_name", csv_lookup)
    assert value == "From Snapshot"
    assert source == bf._SOURCE_SNAPSHOT


def test_select_value_falls_back_to_extra_when_snapshot_missing() -> None:
    row = {
        "canonical_id": "P1",
        "new_proj_name": None,
        "extra_proj_name": "From Extra",
    }
    csv_lookup = {"P1": {"proj_name": "From CSV"}}
    value, source = bf._select_value(row, "proj_name", csv_lookup)
    assert value == "From Extra"
    assert source == bf._SOURCE_EXTRA


def test_select_value_falls_back_to_csv_when_db_sources_missing() -> None:
    row = {"canonical_id": "P1", "new_proj_name": None, "extra_proj_name": None}
    csv_lookup = {"P1": {"proj_name": "From CSV"}}
    value, source = bf._select_value(row, "proj_name", csv_lookup)
    assert value == "From CSV"
    assert source == bf._SOURCE_CSV


def test_select_value_returns_none_when_all_sources_empty() -> None:
    row = {"canonical_id": "P1", "new_proj_name": None, "extra_proj_name": ""}
    value, source = bf._select_value(row, "proj_name", csv_lookup={})
    assert value is None
    assert source is None


def test_select_value_csv_lookup_missing_cid_returns_none() -> None:
    """CSV lookup that has data for OTHER cids but not this one falls
    through cleanly — no KeyError, no silent wrong-row data."""
    row = {"canonical_id": "P_MISSING", "new_proj_name": None, "extra_proj_name": None}
    csv_lookup = {"P1": {"proj_name": "Other"}}
    value, source = bf._select_value(row, "proj_name", csv_lookup)
    assert value is None
    assert source is None


def test_select_value_skips_empty_string_from_snapshot() -> None:
    """Empty string in a snapshot column should fall through to extra/CSV,
    not be treated as a real value. Belt-and-braces with the SQL TRIM."""
    row = {"canonical_id": "P1", "new_proj_name": "", "extra_proj_name": "Real"}
    value, source = bf._select_value(row, "proj_name", csv_lookup={})
    assert value == "Real"
    assert source == bf._SOURCE_EXTRA


# ── _pick_csv_value ──────────────────────────────────────────────────────────


def test_pick_csv_value_v2_key() -> None:
    assert bf._pick_csv_value({"proj_name": "Foo"}, "proj_name") == "Foo"


def test_pick_csv_value_v1_title_case_key() -> None:
    """``CatalogRow.as_csv_row`` populates Title-Case aliases so old CSVs
    with V1 column names still resolve."""
    assert bf._pick_csv_value({"Property Name": "Foo"}, "proj_name") == "Foo"


def test_pick_csv_value_lowercase_zip_alias() -> None:
    assert bf._pick_csv_value({"zip": "02110"}, "zip_code") == "02110"


def test_pick_csv_value_returns_none_for_missing_field() -> None:
    assert bf._pick_csv_value({"city": "Boston"}, "address") is None


def test_pick_csv_value_skips_empty_string() -> None:
    assert bf._pick_csv_value({"proj_name": "  ", "Property Name": "Foo"}, "proj_name") == "Foo"


def test_pick_csv_value_returns_none_for_none_row() -> None:
    assert bf._pick_csv_value(None, "proj_name") is None
    assert bf._pick_csv_value({}, "proj_name") is None


# ── _load_csv_lookup ─────────────────────────────────────────────────────────


def test_load_csv_lookup_returns_empty_for_none_path() -> None:
    """No --csv flag → empty dict; downstream falls through to snapshot/extra
    paths only."""
    assert bf._load_csv_lookup(None) == {}


def test_load_csv_lookup_reads_real_csv(tmp_path: Path) -> None:
    """Sanity check: small CSV in the project's normal column shape parses
    via ``CsvPropertyCatalogSource`` and lands in the lookup keyed by
    canonical_id."""
    csv_path = tmp_path / "props.csv"
    csv_path.write_text(
        "apartmentid,name,address,city,state,zip,website\n"
        "P1,Foo Apartments,1 Main St,Boston,MA,02110,http://foo.com\n"
        "P2,Bar Apartments,2 Elm Ave,Cambridge,MA,02139,http://bar.com\n",
        encoding="utf-8",
    )
    lookup = bf._load_csv_lookup(csv_path)
    assert len(lookup) == 2
    # CatalogRow.as_csv_row populates both V2 and Title-Case keys.
    assert lookup["P1"].get("proj_name") == "Foo Apartments"
    assert lookup["P1"].get("address") == "1 Main St"
    assert lookup["P2"].get("city") == "Cambridge"


# ── _parse_cid_arg ──────────────────────────────────────────────────────────


def test_parse_cid_arg_splits_csv_and_strips_whitespace() -> None:
    assert bf._parse_cid_arg(" 12586, 12963, 12985 ") == ("12586", "12963", "12985")


def test_parse_cid_arg_returns_none_for_empty_input() -> None:
    assert bf._parse_cid_arg(None) is None
    assert bf._parse_cid_arg("") is None
    assert bf._parse_cid_arg(",,,") is None


def test_parse_cid_arg_single_value() -> None:
    assert bf._parse_cid_arg("12586") == ("12586",)


# ── _build_update (idempotency guard) ────────────────────────────────────────


def test_update_includes_null_guard() -> None:
    """Every UPDATE must keep the IS NULL guard so re-runs and concurrent
    writers can't clobber a value that was just set."""
    for f in bf._BACKFILLABLE:
        sql = bf._build_update(f)
        assert f"SET {f} =" in sql
        assert f"({f} IS NULL OR TRIM({f}) = '')" in sql


# ── Field set consistency ────────────────────────────────────────────────────


def test_backfillable_and_csv_keys_match() -> None:
    """Every backfillable field must have at least one CSV key mapping,
    otherwise --csv would silently skip that field."""
    for f in bf._BACKFILLABLE:
        assert f in bf._CSV_KEYS, f"{f!r} missing from _CSV_KEYS"
        assert bf._CSV_KEYS[f], f"{f!r} has empty CSV key tuple"


def test_backfillable_and_v1_alias_match() -> None:
    """Every backfillable field must appear in _V1_ALIAS (value may be empty)
    so the query builder doesn't KeyError on lookup."""
    for f in bf._BACKFILLABLE:
        assert f in bf._V1_ALIAS, f"{f!r} missing from _V1_ALIAS"


# ── CLI smoke ────────────────────────────────────────────────────────────────


def test_unknown_field_returns_nonzero_exit() -> None:
    """``--fields`` validates against ``_BACKFILLABLE`` and bails before
    opening any DB connection. ``2`` is the canonical "bad usage" code."""
    rc = bf.main(["--fields", "made_up_column"])
    assert rc == 2
