from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ma_poc.pms.adapters import _knock_dni_recovery as recovery


def _html(community_id: str) -> str:
    return f"""
        <script>
          const config = {{
            dniId: "{community_id}",
            dniApiKey: '00000000000000000000000000000000'
          }};
          if (typeof window.knockDoorway !== 'undefined') {{
            window.knockDoorway.init(
              config.dniApiKey, 'community', config.dniId
            );
          }}
        </script>
    """


def _ctx(
    *,
    community_id: str = "0962eddf11eb03b8",
    address: str = "320 W Palmetto Park Rd",
    city: str = "Boca Raton",
    state: str = "FL",
) -> SimpleNamespace:
    return SimpleNamespace(
        fetch_result=SimpleNamespace(body=_html(community_id).encode()),
        address=address,
        city=city,
        state=state,
        property_name="The Heritage at Boca Raton",
    )


def _property(
    *,
    property_id: int = 2012004,
    name: str = "(HBR) The Heritage at Boca Raton",
    street: str = "320 W Palmetto Park Rd.",
    raw: str = "320 W Palmetto Park Rd. Boca Raton FL 33432",
    city: str = "Boca Raton",
    state: str = "FL",
) -> dict[str, Any]:
    return {
        "id": property_id,
        "data": {
            "location": {
                "name": name,
                "address": {
                    "street": street,
                    "raw": raw,
                    "city": city,
                    "state": state,
                },
            }
        },
    }


@pytest.mark.parametrize(
    ("community_id", "property_id"),
    (
        ("0962eddf11eb03b8", "43715"),
        ("11eb93b65a9cedde", "1146"),
        ("1654d92211ebe742", "48769"),
    ),
    ids=("heritage-43715", "signal-pointe-1146", "spring-gate-48769"),
)
def test_find_knock_dni_community_id_for_live_cohort_shapes(
    community_id: str,
    property_id: str,
) -> None:
    assert property_id
    assert recovery.find_knock_dni_community_id(_html(community_id)) == community_id


@pytest.mark.parametrize(
    "html",
    (
        '<script>const config={dniId:"0962eddf11eb03b8"};</script>',
        """
        <script>
          const config={
            dniId:"0962eddf11eb03b8",
            dniApiKey:"00000000000000000000000000000000"
          };
        </script>
        """,
        """
        <script>
          const config={
            dniId:"0962eddf11eb03b8",
            dniApiKey:"00000000000000000000000000000000"
          };
          knockDoorway.init(config.dniApiKey, "application", config.dniId);
        </script>
        """,
        """
        <script>
          const config={
            dniId:"not-a-community-id",
            dniApiKey:"00000000000000000000000000000000"
          };
          knockDoorway.init(config.dniApiKey, "community", config.dniId);
        </script>
        """,
    ),
    ids=("id-only", "config-only", "wrong-kind", "malformed-id"),
)
def test_find_knock_dni_community_id_rejects_partial_or_broad_signals(
    html: str,
) -> None:
    assert recovery.find_knock_dni_community_id(html) is None


@pytest.mark.parametrize(
    ("expected_address", "observed_street", "city", "state"),
    (
        ("320 W Palmetto Park Rd", "320 W Palmetto Park Road", "Boca Raton", "FL"),
        ("1500 Springate Dr", "1500 Spring Gate Drive", "Panama City", "FL"),
        ("3616 Hogans Run Rd", "3616 Hogans Run Road", "Columbus", "OH"),
    ),
)
def test_property_scope_matches_live_address_variants(
    expected_address: str,
    observed_street: str,
    city: str,
    state: str,
) -> None:
    ctx = _ctx(address=expected_address, city=city, state=state)
    assert recovery.property_scope_matches(
        ctx,
        _property(
            street=observed_street,
            raw=observed_street,
            city=city,
            state=state,
        ),
    )


def test_property_scope_rejects_different_property() -> None:
    assert not recovery.property_scope_matches(
        _ctx(),
        _property(
            street="999 Unrelated Avenue",
            raw="999 Unrelated Avenue Boca Raton FL 33432",
        ),
    )


class _FakeClient:
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_recover_knock_dni_config_is_scoped_strict_and_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_kwargs: dict[str, Any] = {}
    urls: list[str] = []

    def fake_client(**kwargs: Any) -> _FakeClient:
        client_kwargs.update(kwargs)
        return _FakeClient()

    async def fake_fetch(
        _client: object,
        url: str,
    ) -> tuple[int, dict[str, Any]]:
        urls.append(url)
        if "/community/" in url:
            return 200, {"property": _property()}
        return 200, {
            "units_data": {
                "layouts": [{"id": "layout-1", "name": "A1"}],
                "units": [
                    {"name": "404", "price": "4105", "layoutId": "layout-1"},
                    {"name": "", "price": "2200", "layoutId": "layout-1"},
                    {"name": "405", "price": "0", "layoutId": "layout-1"},
                    {
                        "name": "406",
                        "price": "2500",
                        "layoutId": "layout-1",
                        "hidden": True,
                    },
                ],
            }
        }

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(recovery, "_fetch_public_json", fake_fetch)

    units = await recovery.recover_knock_dni_config(_ctx())

    assert [unit["unit_number"] for unit in units] == ["404"]
    assert units[0]["market_rent_low"] == 4105
    assert units[0]["extraction_tier"] == "TIER_1_API_KNOCK_DNI_CONFIG"
    assert units[0]["source_api_url"].endswith("/property/2012004/units")
    assert urls == [
        "https://doorway-api.knockrentals.com/v1/property/community/0962eddf11eb03b8",
        "https://doorway-api.knockrentals.com/v1/property/2012004/units",
    ]
    assert client_kwargs["trust_env"] is False
    assert "User-Agent" not in client_kwargs["headers"]
    assert client_kwargs["limits"].max_connections == 2


@pytest.mark.asyncio
async def test_scope_miss_never_fetches_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FakeClient())

    async def fake_fetch(
        _client: object,
        url: str,
    ) -> tuple[int, dict[str, Any]]:
        urls.append(url)
        return 200, {
            "property": _property(
                street="999 Unrelated Avenue",
                raw="999 Unrelated Avenue Boca Raton FL 33432",
            )
        }

    monkeypatch.setattr(recovery, "_fetch_public_json", fake_fetch)

    assert await recovery.recover_knock_dni_config(_ctx()) == []
    assert urls == ["https://doorway-api.knockrentals.com/v1/property/community/0962eddf11eb03b8"]


@pytest.mark.asyncio
async def test_bounded_json_reader_rejects_oversized_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(recovery._MAX_RESPONSE_BYTES + 1)},
            content=b"{}",
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        status, payload = await recovery._fetch_public_json(
            client,
            "https://doorway-api.knockrentals.com/test",
        )

    assert status == 200
    assert payload is None
