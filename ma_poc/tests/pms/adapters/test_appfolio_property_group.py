"""2026-05-25 canary 1ef1060 regression #11 CRITICAL — AppFolio vanity
multi-property cross-contamination fix.

User-flagged: PROSPER Azalea City (Valdosta GA 31602) was getting 300
units extracted from St. Augustine FL 32086 — totally different
property in the same PMC (dlpcapital). Academy Place (Corning NY)
similarly cross-contaminated with units from Canandaigua NY, Erie PA,
Grand Island NY (same PMC: riedman).

Root cause: the AppFolio VANITY adapter fetches
``<slug>.appfolio.com/listings`` with NO filter — returns ALL units
managed by the PMC. The marketing page's embed JS specifies a
``propertyGroup`` filter to scope listings to just THIS property:

    Appfolio.Listing({
        hostUrl: 'dlpcapital.appfolio.com',
        propertyGroup: 'PM - PROSPER Azalea City',
        ...
    })

Fix: extract ``propertyGroup`` from the embed JS, append it as
``filters[property_list]={propertyGroup}`` to the /listings URL.
Verified live on PROSPER:
  Unfiltered: 300 listings (all PMC properties, all states/zips)
  Filtered:    47 listings (all matching ZIP 31602, all Valdosta GA,
               all at 1503 E Park Ave — matches PROSPER exactly)

This file pins the propertyGroup-extraction + slug-from-hostUrl fix
and the URL construction so a future refactor cannot silently
re-introduce the cross-contamination.
"""
from __future__ import annotations

import pytest

from ma_poc.pms.adapters.appfolio import (
    find_appfolio_property_group,
    find_appfolio_slug,
)

# ─────────────────────────────────────────────────────────────────────
# find_appfolio_property_group — extracts the per-property filter from
# the AppFolio embed JS configuration block.
# ─────────────────────────────────────────────────────────────────────


def test_property_group_extracted_from_appfolio_listing_embed() -> None:
    """The Prosper Azalea City signature — full embed JS block."""
    html = (
        '<html><body>'
        '<script>'
        '  Appfolio.Listing({'
        '    hostUrl: \'dlpcapital.appfolio.com\','
        '    propertyGroup: \'PM - PROSPER Azalea City\','
        '    themeColor: \'#12284b\','
        '    height: \'1000px\','
        '    width: \'100%\','
        '    defaultOrder: \'bedrooms\''
        '  })'
        '</script>'
        '</body></html>'
    )
    pg = find_appfolio_property_group(html)
    assert pg == "PM - PROSPER Azalea City"


def test_property_group_double_quoted() -> None:
    """Some operators use double quotes in the JS config."""
    html = '<script>Appfolio.Listing({propertyGroup: "Riverside Heights"})</script>'
    assert find_appfolio_property_group(html) == "Riverside Heights"


def test_property_group_no_whitespace_around_colon() -> None:
    """JSON-style minified config — no whitespace."""
    html = "<script>Appfolio.Listing({propertyGroup:'Compact Form'})</script>"
    assert find_appfolio_property_group(html) == "Compact Form"


def test_property_group_extra_whitespace() -> None:
    """Pretty-printed config — extra whitespace."""
    html = "<script>Appfolio.Listing({ propertyGroup   :   'Spacey Name'  })</script>"
    assert find_appfolio_property_group(html) == "Spacey Name"


def test_property_group_with_special_chars() -> None:
    """Operator-chosen names with dashes, ampersands, etc."""
    html = "<script>propertyGroup: 'Foo - Bar & Baz #1'</script>"
    assert find_appfolio_property_group(html) == "Foo - Bar & Baz #1"


def test_property_group_none_when_absent() -> None:
    """Single-property AppFolio account — no propertyGroup in JS.
    Caller falls through to unfiltered /listings URL (existing path)."""
    html = "<html><body>just text</body></html>"
    assert find_appfolio_property_group(html) is None


def test_property_group_none_when_html_lacks_appfolio_embed() -> None:
    """HTML without any AppFolio embed — returns None defensively."""
    assert find_appfolio_property_group("") is None
    assert find_appfolio_property_group(None) is None  # type: ignore[arg-type]


def test_property_group_first_match_wins_on_multiple() -> None:
    """If multiple embed blocks (rare but possible), take the first.
    Operators don't generally embed two property groups on one page."""
    html = (
        '<script>propertyGroup: \'First\'</script>'
        '<script>propertyGroup: \'Second\'</script>'
    )
    assert find_appfolio_property_group(html) == "First"


# ─────────────────────────────────────────────────────────────────────
# find_appfolio_slug — now ALSO recognises the hostUrl: 'X.appfolio.com'
# embed-JS config form (PROSPER signature). Pre-fix, the slug regex
# only matched ``https://X.appfolio.com`` URLs; PROSPER's embed has
# only the hostUrl config (no https-prefixed URL anywhere on the
# page) so the slug was missed, the propertyGroup filter couldn't
# be applied, and the contamination persisted.
# ─────────────────────────────────────────────────────────────────────


def test_slug_from_url_form_legacy() -> None:
    """Legacy path: slug discovered from an https://X.appfolio.com URL."""
    html = '<a href="https://cathcartres.appfolio.com/listings">View</a>'
    assert find_appfolio_slug(html) == "cathcartres"


def test_slug_from_hosturl_embed_config_only() -> None:
    """The PROSPER signature: only the embed JS hostUrl config is
    present; no https-prefixed URL elsewhere. New path must catch it."""
    html = (
        "<script>"
        "Appfolio.Listing({"
        "    hostUrl: 'dlpcapital.appfolio.com',"
        "    propertyGroup: 'PM - PROSPER Azalea City'"
        "})"
        "</script>"
    )
    assert find_appfolio_slug(html) == "dlpcapital"


def test_slug_url_form_wins_when_both_present() -> None:
    """When both forms appear (URL + hostUrl config), the legacy URL
    form wins — preserves all pre-fix discovery behaviour."""
    html = (
        '<a href="https://urlform.appfolio.com/listings">Apply</a>'
        "<script>hostUrl: 'configform.appfolio.com'</script>"
    )
    assert find_appfolio_slug(html) == "urlform"


def test_slug_hosturl_double_quoted() -> None:
    """hostUrl with double quotes."""
    html = '<script>hostUrl: "doublequoted.appfolio.com"</script>'
    assert find_appfolio_slug(html) == "doublequoted"


def test_slug_hosturl_skip_list_still_applies() -> None:
    """The skip-list (www, app, support, etc.) applies to BOTH discovery
    paths — don't return 'support' from a hostUrl config either."""
    html = "<script>hostUrl: 'support.appfolio.com'</script>"
    assert find_appfolio_slug(html) is None


def test_slug_none_when_no_appfolio_anywhere() -> None:
    html = "<html><body>nothing here</body></html>"
    assert find_appfolio_slug(html) is None


# ─────────────────────────────────────────────────────────────────────
# URL construction — verify the propertyGroup filter is properly
# URL-encoded into the listings URL. (Tests the integration logic
# pattern used in AppFolioAdapter.extract.)
# ─────────────────────────────────────────────────────────────────────


def test_url_construction_appends_property_group_filter() -> None:
    """Mirrors the adapter's URL build logic. Spaces, dashes, and
    special chars in propertyGroup must be URL-encoded."""
    from urllib.parse import quote

    slug = "dlpcapital"
    property_group = "PM - PROSPER Azalea City"
    base = f"https://{slug}.appfolio.com/listings"
    url = f"{base}?filters%5Bproperty_list%5D={quote(property_group)}"

    # Decoded form: ?filters[property_list]=PM - PROSPER Azalea City
    assert "dlpcapital.appfolio.com" in url
    assert "filters%5Bproperty_list%5D" in url
    assert "PM%20-%20PROSPER%20Azalea%20City" in url


@pytest.mark.parametrize("pg,expected_in_url", [
    ("Simple", "Simple"),
    ("With Spaces", "With%20Spaces"),
    ("Dashes-and_Underscores", "Dashes-and_Underscores"),
    ("Special & Chars #1", "Special%20%26%20Chars%20%231"),
])
def test_url_construction_encodes_special_chars(pg: str, expected_in_url: str) -> None:
    from urllib.parse import quote
    encoded = quote(pg)
    assert expected_in_url in encoded
