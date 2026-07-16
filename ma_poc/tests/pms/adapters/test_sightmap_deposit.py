"""SightMap deposit extraction (2026-07-16).

Deposit isn't a top-level SightMap field — it's the "Security Deposit
(Refundable)" line inside ``static_expenses[].expenses[]`` (definitions) with
the per-unit dollar figure in ``expense_amounts[<id>].amount``. Verified live on
anthemeverett ($500 on 92/92 units). The "... Alternative" line is always
"Varies" and must be ignored. Population is sparse across properties, so the
parser only fills the subset that publishes a real amount.
"""

from __future__ import annotations

from ma_poc.pms.adapters.sightmap import _sightmap_deposit, parse_sightmap_payload


def _unit(amount_36738):
    return {
        "floor_plan_id": "1",
        "unit_number": "3002",
        "price": 2400,
        "expense_amounts": {
            "36738": {"expense_id": "36738", "amount": amount_36738},
            "36721": {"expense_id": "36721", "amount": None, "text_amount": "Varies"},
        },
        "static_expenses": [
            {
                "key": "additional",
                "expenses": [
                    {"id": "36738", "label": "Security Deposit (Refundable)"},
                    {"id": "36721", "label": "Security Deposit Alternative"},
                ],
            }
        ],
    }


# ── helper ───────────────────────────────────────────────────────────────────

def test_deposit_refundable_amount():
    assert _sightmap_deposit(_unit("500.00")) == "$500"


def test_deposit_comma_amount():
    assert _sightmap_deposit(_unit("1,250.00")) == "$1,250"


def test_deposit_alternative_varies_is_skipped():
    # amount null on the refundable line → nothing (the "Alternative"/"Varies"
    # line must never be used as the deposit).
    assert _sightmap_deposit(_unit(None)) == ""


def test_deposit_absent_when_no_expense_block():
    assert _sightmap_deposit({"unit_number": "1", "price": 1000}) == ""


# ── parser integration ───────────────────────────────────────────────────────

def test_parser_emits_deposit():
    body = {
        "data": {
            "units": [_unit("500.00")],
            "floor_plans": [
                {"id": "1", "name": "A1", "bedroom_count": 1, "bathroom_count": 1}
            ],
        }
    }
    units, _ = parse_sightmap_payload(body, "test")
    assert len(units) == 1
    assert units[0]["deposit"] == "$500"


def test_parser_no_deposit_stays_empty():
    body = {
        "data": {
            "units": [_unit(None)],
            "floor_plans": [
                {"id": "1", "name": "A1", "bedroom_count": 1, "bathroom_count": 1}
            ],
        }
    }
    units, _ = parse_sightmap_payload(body, "test")
    assert units[0]["deposit"] == ""
