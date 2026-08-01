"""Strict operator-authored Elise application unit recovery.

Live schema validation on 2026-08-01 covered Electric City, ArtHaus Jack
London, Hudson Square, Riverwalk on the Hudson, River's Edge, Hudson Lookout
and Harbour Point Gardens: 49/49 rows carried unique native ids/unit numbers
and positive rent. Ridgeview and Spring Valley were valid empty controls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ma_poc.core.identity import unit_has_real_anchor
from ma_poc.pms.adapters import _elise_applications_recovery as recovery
from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.detector import DetectedPMS

_ELECTRIC_UUID = "917cf8d2-fa6e-11ed-bed7-a3fbe5561883"


def _source_html(
    *,
    uuid: str = _ELECTRIC_UUID,
    name: str = "Electric City",
    address: str = "240 State Street",
    city: str = "Schenectady",
    state: str = "NY",
    zip_code: str = "12305",
    link_shape: str = "lease",
) -> str:
    application_url = f"https://applications.eliseai.com/building/{uuid}"
    if link_shape == "anchor":
        authored = f'<a href="{application_url}">Apply now</a>'
    else:
        authored = f'<script>var leaseUrl = "{application_url}";</script>'
    return f"""
      <html><head><title>{name}</title></head><body>
        <h1>{name}</h1>
        <address>{address}<br>{city}, {state} {zip_code}</address>
        {authored}
        <script>
          JonahWidget.meetelise({{"building":"{uuid}"}});
        </script>
      </body></html>
    """


def _ctx(
    source_html: str | None = None,
    *,
    name: str = "Electric City",
    address: str = "240 State St",
    city: str = "Schenectady",
    state: str = "NY",
    zip_code: str = "12305",
) -> AdapterContext:
    return AdapterContext(
        base_url="https://primeelectriccity.com/",
        detected=DetectedPMS(pms="unknown", confidence=0.0),
        profile=None,
        expected_total_units=None,
        property_id="119119",
        fetch_result=SimpleNamespace(
            body=(source_html or _source_html()).encode(),
            final_url="https://primeelectriccity.com/",
        ),
        property_name=name,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
    )


def _configuration(
    *,
    uuid: str = _ELECTRIC_UUID,
    name: str = "Electric City",
) -> dict[str, Any]:
    return {
        "organization_name": "Prime Companies",
        "building_details": {
            "slug": uuid,
            "building_name": name,
        },
    }


def _unit(
    *,
    native_id: str = "u1909445292",
    unit_number: str = "111",
    plan: str = "American",
    beds: object = 1,
    baths: object = 1,
    sqft: object = 917,
    rent: object = 1822,
    available_date: object = "2026-09-11",
) -> dict[str, Any]:
    return {
        "id": native_id,
        "unit_number": unit_number,
        "floorplan_name": plan,
        "number_of_bedrooms": beds,
        "number_of_bathrooms": baths,
        "square_footage": sqft,
        "rent": rent,
        "date_available": available_date,
        "floor": "1",
        "sub_building_name": "",
        "availability_stage": "renovating",
    }


@pytest.mark.parametrize(
    ("uuid", "name", "address", "city", "zip_code"),
    (
        (
            _ELECTRIC_UUID,
            "Electric City",
            "240 State Street",
            "Schenectady",
            "12305",
        ),
        (
            "917d0fe8-fa6e-11ed-beda-c3e8b842872e",
            "Hudson Square",
            "1000 Hudson Square",
            "Cohoes",
            "12047",
        ),
        (
            "917d2032-fa6e-11ed-bedc-77fc60d54e1a",
            "Riverwalk on the Hudson",
            "200 Riverwalk Way",
            "Cohoes",
            "12047",
        ),
    ),
    ids=("electric-city", "hudson-square", "riverwalk"),
)
def test_discovers_three_verified_operator_authored_building_links(
    uuid: str,
    name: str,
    address: str,
    city: str,
    zip_code: str,
) -> None:
    source = _source_html(
        uuid=uuid,
        name=name,
        address=address,
        city=city,
        zip_code=zip_code,
    )

    assert recovery.discover_elise_application_uuid(source) == uuid


def test_discovery_accepts_duplicate_same_uuid_anchor_and_lease_setting() -> None:
    application_url = f"https://applications.eliseai.com/building/{_ELECTRIC_UUID}"
    source = (
        f'<a href="{application_url}">Apply</a>'
        f'<script>var leaseUrl = "{application_url}";</script>'
    )

    assert recovery.discover_elise_application_uuid(source) == _ELECTRIC_UUID


@pytest.mark.parametrize(
    "source",
    (
        # Chat configuration alone is not an application-inventory grant.
        f'<script>JonahWidget.meetelise({{"building":"{_ELECTRIC_UUID}"}})</script>',
        # A free-floating string is not operator-authored routing metadata.
        f'<script>"https://applications.eliseai.com/building/{_ELECTRIC_UUID}"</script>',
        f'<a href="http://applications.eliseai.com/building/{_ELECTRIC_UUID}">x</a>',
        f'<a href="https://applications.eliseai.com.evil.test/building/{_ELECTRIC_UUID}">x</a>',
        f'<a href="https://applications.eliseai.com:bad/building/{_ELECTRIC_UUID}">x</a>',
        f'<a href="https://applications.eliseai.com/building/{_ELECTRIC_UUID}?next=evil">x</a>',
        (
            f'<a href="https://applications.eliseai.com/building/{_ELECTRIC_UUID}">one</a>'
            '<a href="https://applications.eliseai.com/building/'
            '917d0fe8-fa6e-11ed-beda-c3e8b842872e">two</a>'
        ),
    ),
    ids=(
        "chat-only",
        "free-floating",
        "http",
        "deceptive-host",
        "invalid-port",
        "query",
        "ambiguous-buildings",
    ),
)
def test_discovery_fails_closed_on_untrusted_or_ambiguous_routes(source: str) -> None:
    assert recovery.discover_elise_application_uuid(source) is None


def test_source_and_configuration_require_independent_property_boundary() -> None:
    source = _source_html()
    ctx = _ctx(source)

    assert recovery._source_matches_context(source, ctx)
    assert recovery._configuration_matches_context(
        _configuration(),
        _ELECTRIC_UUID,
        ctx,
    )
    assert not recovery._source_matches_context(
        source,
        _ctx(source, address="999 Foreign Road"),
    )
    assert not recovery._source_matches_context(
        source,
        _ctx(source, zip_code="99999"),
    )
    assert not recovery._configuration_matches_context(
        _configuration(name="Hudson Square"),
        _ELECTRIC_UUID,
        ctx,
    )
    assert not recovery._configuration_matches_context(
        _configuration(uuid="917d0fe8-fa6e-11ed-beda-c3e8b842872e"),
        _ELECTRIC_UUID,
        ctx,
    )


def test_parser_preserves_native_fields_and_explicit_future_date() -> None:
    source_url = recovery._units_url(_ELECTRIC_UUID)
    rows = recovery.parse_elise_units(
        [_unit()],
        uuid=_ELECTRIC_UUID,
        source_url=source_url,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["unit_number"] == "111"
    assert row["unit_name"] == "111"
    assert row["floor_plan_name"] == "American"
    assert row["bedrooms"] == "1"
    assert row["bathrooms"] == "1"
    assert row["sqft"] == "917"
    assert row["market_rent_low"] == 1822
    assert row["market_rent_high"] == 1822
    assert row["availability_date"] == "2026-09-11"
    assert row["available_date"] == "2026-09-11"
    assert row["source_api_url"] == source_url
    assert row["extraction_tier"] == "TIER_1_API_ELISE_APPLICATIONS"
    assert row["source_ids"] == {
        "elise_applications_unit_id": "u1909445292"
    }
    assert row["_availability_date_provenance"] == "explicit_provider_date"
    assert row["_elise_availability_stage"] == "renovating"
    assert unit_has_real_anchor(row)


@pytest.mark.parametrize(
    "changed",
    (
        {"id": "1909445292"},
        {"unit_number": "Bedrooms"},
        {"floorplan_name": ""},
        {"number_of_bedrooms": None},
        {"number_of_bathrooms": 99},
        {"square_footage": 0},
        {"rent": 0},
        {"date_available": "Available Now"},
        {"date_available": "2026-02-30"},
    ),
    ids=(
        "bad-native-id",
        "junk-unit",
        "missing-plan",
        "missing-beds",
        "bad-baths",
        "zero-area",
        "zero-rent",
        "implicit-date",
        "invalid-date",
    ),
)
def test_parser_rejects_incomplete_or_noncanonical_rows(
    changed: dict[str, Any],
) -> None:
    item = _unit()
    item.update(changed)

    assert (
        recovery.parse_elise_units(
            [item],
            uuid=_ELECTRIC_UUID,
            source_url=recovery._units_url(_ELECTRIC_UUID),
        )
        == []
    )


def test_parser_rejects_duplicate_unit_or_native_identity() -> None:
    assert (
        recovery.parse_elise_units(
            [_unit(), _unit(native_id="u1909445293")],
            uuid=_ELECTRIC_UUID,
            source_url=recovery._units_url(_ELECTRIC_UUID),
        )
        == []
    )
    assert (
        recovery.parse_elise_units(
            [_unit()],
            uuid=_ELECTRIC_UUID,
            source_url="https://applications.eliseai.com/api/searchUnits?building_slug=other",
        )
        == []
    )
    assert (
        recovery.parse_elise_units(
            [_unit(), _unit(unit_number="112")],
            uuid=_ELECTRIC_UUID,
            source_url=recovery._units_url(_ELECTRIC_UUID),
        )
        == []
    )


@pytest.mark.asyncio
async def test_recovery_fetches_configuration_before_property_scoped_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str) -> object:
        calls.append(url)
        if url == recovery._configuration_url(_ELECTRIC_UUID):
            return _configuration()
        if url == recovery._units_url(_ELECTRIC_UUID):
            return [_unit()]
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(recovery, "_fetch_elise_json", fake_fetch)

    rows = await recovery.recover_elise_applications(_ctx())

    assert len(rows) == 1
    assert calls == [
        recovery._configuration_url(_ELECTRIC_UUID),
        recovery._units_url(_ELECTRIC_UUID),
    ]


@pytest.mark.asyncio
async def test_untrusted_source_or_wrong_configuration_never_fetches_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str) -> object:
        calls.append(url)
        return _configuration(name="Hudson Square")

    monkeypatch.setattr(recovery, "_fetch_elise_json", fake_fetch)

    assert (
        await recovery.recover_elise_applications(
            _ctx('<script>JonahWidget.meetelise({"building":"x"})</script>')
        )
        == []
    )
    assert calls == []

    assert await recovery.recover_elise_applications(_ctx()) == []
    assert calls == [recovery._configuration_url(_ELECTRIC_UUID)]


def test_jugnu_preserves_date_plan_rent_and_native_unit_number() -> None:
    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    [row] = recovery.parse_elise_units(
        [_unit()],
        uuid=_ELECTRIC_UUID,
        source_url=recovery._units_url(_ELECTRIC_UUID),
    )

    formatted = _format_v2_unit(
        row,
        datetime(2026, 8, 1, tzinfo=UTC),
        "119119",
    )

    assert formatted["unit_id"] == "111"
    assert formatted["floor_plan_name"] == "American"
    assert formatted["beds"] == 1
    assert formatted["baths"] == 1
    assert formatted["area"] == 917
    assert formatted["rent_low"] == 1822
    assert formatted["available_date"] == "2026-09-11"
