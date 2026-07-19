"""Tests for the confirmation → ScrapeProfile seeder helpers (2026-07-19).

Pins the page-vs-API classification + canonical-id join used to inject the
roster-confirmation hot URLs into ``winning_page_url`` / ``known_endpoints``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SEEDER = (
    Path(__file__).resolve().parents[2] / "scripts" / "seed_profiles_from_confirmation.py"
)
_spec = importlib.util.spec_from_file_location("_seed_profiles", _SEEDER)
seed = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(seed)


@pytest.mark.parametrize(
    "url,is_api",
    [
        ("https://sightmap.com/app/api/v1/zlpo6578pg4/sightmaps/90718", True),
        ("https://doorway-api.knockrentals.com/v1/property/2022625/units", True),
        ("https://inventory.g5marketingcloud.com/graphql", True),
        ("https://api-v3.peek.us/communities/123?include=spaces", True),
        # page surfaces → winning_page_url, NOT known_endpoints
        ("https://www.on-site.com/web/online_app3?property_id=606821&unit_id=0", False),
        ("https://x.securecafe.com/onlineleasing/y/availableunits.aspx", False),
        ("https://livenjoy.myresman.com/Portal/Applicants/Availability?a=1588", False),
        ("https://www.marketingsite.com/floorplans", False),
    ],
)
def test_is_api_surface(url: str, is_api: bool) -> None:
    assert seed._is_api_surface(url) is is_api


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("onsite_apply", "onsite_apply"),
        ("rentcafe (parse_securecafe_applicant_floorplans)", "rentcafe"),
        ("realpage/onsite", "realpage"),
        ("_harbor_group", "harbor_group"),
        ("sightmap, funnel", "sightmap"),
        ("", ""),
    ],
)
def test_norm_pms(raw: str, expected: str) -> None:
    assert seed._norm_pms(raw) == expected


def test_url_key_scheme_www_slash_insensitive() -> None:
    a = seed._url_key("https://www.16bennett.com/")
    b = seed._url_key("http://16bennett.com")
    assert a == b == "16bennett.com"


def test_http_guard() -> None:
    assert seed._http("https://x.com") is True
    assert seed._http("ftp://x.com") is False
    assert seed._http("") is False
    assert seed._http(None) is False  # type: ignore[arg-type]


def test_build_url_to_cid_joins_worklist(tmp_path: Path) -> None:
    wl = tmp_path / "wl.jsonl"
    wl.write_text(
        '{"cid":"118965","url":"https://www.16bennett.com"}\n'
        '{"cid":"273160","url":"http://riviera.com/"}\n',
        encoding="utf-8",
    )
    m = seed.build_url_to_cid([wl])
    assert m[seed._url_key("https://16bennett.com/")] == "118965"
    assert m[seed._url_key("http://www.riviera.com")] == "273160"
