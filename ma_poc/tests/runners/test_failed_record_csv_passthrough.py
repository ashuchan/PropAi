"""Regression tests — ``_make_failed_record`` must preserve CSV identity fields.

Pre-fix behaviour (2026-05-18): every failure-path emit
(``per_property_timeout``, runtime exception, wedge-rescue budget exhausted)
wrote ``proj_name=None``, ``address=None``, etc. into ``properties.json``.
Sync then upserted these NULLs into the ``properties`` table. The COALESCE
guard in ``data_provider.sql.stores.SqlPropertyStateStore.upsert`` protects
existing rows from being clobbered, but **first-time-failing** properties
(no prior successful scrape) ended up with NULL ``proj_name`` / ``address``
in the DB — the frontend then displayed blank property cards.

840+ properties in the production DB had NULL name/address as a result.

The fix gives ``_make_failed_record`` an optional ``csv_row`` parameter
populated from the catalog row's ``as_csv_row()`` output, and all three
failure-path call sites pass it.
"""

from __future__ import annotations

import inspect
import re

from ma_poc.scripts.runners import jugnu
from ma_poc.scripts.runners.jugnu import _make_failed_record


# ── Direct unit tests of the helper ──────────────────────────────────────────


def test_failed_record_v2_with_csv_row_populates_identity_fields() -> None:
    csv_row = {
        "proj_name": "The Example Apartments",
        "address": "123 Main St",
        "city": "Boston",
        "state": "MA",
        "zip_code": "02110",
        "phone": "555-1234",
        "pmc": "Acme Property Mgmt",
    }
    rec = _make_failed_record("P001", "https://example.com/", "boom", "v2", csv_row=csv_row)
    assert rec["proj_name"] == "The Example Apartments"
    assert rec["address"] == "123 Main St"
    assert rec["city"] == "Boston"
    assert rec["state"] == "MA"
    assert rec["zip_code"] == "02110"
    assert rec["phone"] == "555-1234"
    assert rec["pmc"] == "Acme Property Mgmt"
    assert rec["website"] == "https://example.com/"


def test_failed_record_v2_with_title_case_csv_aliases() -> None:
    """``CatalogRow.as_csv_row`` populates both V2 and Title-Case aliases —
    the helper must pick whichever is present."""
    csv_row = {
        "Property Name": "Sunset Towers",
        "Property Address": "9 Elm Ave",
        "City": "Cambridge",
        "State": "MA",
        "ZIP Code": "02139",
        "Phone": "555-9999",
        "Management Company": "Greystar",
    }
    rec = _make_failed_record("P002", "https://x.com", "boom", "v2", csv_row=csv_row)
    assert rec["proj_name"] == "Sunset Towers"
    assert rec["address"] == "9 Elm Ave"
    assert rec["city"] == "Cambridge"
    assert rec["state"] == "MA"
    assert rec["zip_code"] == "02139"
    assert rec["phone"] == "555-9999"
    assert rec["pmc"] == "Greystar"


def test_failed_record_v2_without_csv_row_emits_none() -> None:
    """Backwards-compatible default — caller without csv_row gets None fields
    (same as the pre-fix behaviour, kept so existing callers don't break)."""
    rec = _make_failed_record("P003", "https://x.com", "boom", "v2")
    assert rec["proj_name"] is None
    assert rec["address"] is None
    assert rec["city"] is None


def test_failed_record_v2_skips_empty_string_csv_values() -> None:
    """An empty string in the CSV is not a real value — fall through to None."""
    csv_row = {"proj_name": "", "address": "", "city": "Boston"}
    rec = _make_failed_record("P004", "https://x.com", "boom", "v2", csv_row=csv_row)
    assert rec["proj_name"] is None
    assert rec["address"] is None
    assert rec["city"] == "Boston"


def test_failed_record_v1_with_csv_row_populates_identity_fields() -> None:
    csv_row = {"proj_name": "Foo", "address": "1 Bar St", "city": "NYC"}
    rec = _make_failed_record("P005", "https://x.com", "boom", "v1", csv_row=csv_row)
    assert rec["Property Name"] == "Foo"
    assert rec["Property Address"] == "1 Bar St"
    assert rec["City"] == "NYC"
    assert rec["Website"] == "https://x.com"


# ── Call-site verification (source-string assertions) ────────────────────────


def test_all_failure_call_sites_pass_csv_row() -> None:
    """Every ``_make_failed_record(...)`` invocation in jugnu.py must pass
    ``csv_row=`` so the CSV identity fields propagate. Pre-fix three call
    sites omitted it (timeout, exception, wedge-rescue budget)."""
    src = inspect.getsource(jugnu)
    calls = list(re.finditer(r"_make_failed_record\s*\(", src))
    # Exclude the function definition itself — the def line precedes the
    # name with the keyword ``def``. (m.start() is at the underscore, so
    # the 4 chars immediately before are "def " when it's a definition.)
    invocations = [m for m in calls if src[max(0, m.start() - 4): m.start()] != "def "]
    assert invocations, "Expected at least one _make_failed_record call site"
    for m in invocations:
        # Slurp until the matching closing paren (simple bracket-balance walk).
        i = m.end()
        depth = 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        call_text = src[m.start(): i]
        assert "csv_row=" in call_text, (
            f"_make_failed_record call missing csv_row= argument:\n{call_text}"
        )
