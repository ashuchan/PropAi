"""Wix iframe walker tests (2026-05-23).

Pins the Wix → AppFolio detection chain:
  • detect_wix_html_iframes finds *.filesusr.com/html/* iframe URLs
  • extract_appfolio_tenant pulls hostUrl from Appfolio.Listing init
  • build_appfolio_listings_url produces the canonical /listings URL

Validated against the live millenniumnw.com iframe body pulled
2026-05-23: ``hostUrl: 'newmpm.appfolio.com'`` → AppFolio listings at
``https://newmpm.appfolio.com/listings``.
"""
from __future__ import annotations

from ma_poc.pms.adapters._wix_iframe_walker import (
    build_appfolio_listings_url,
    detect_wix_html_iframes,
    extract_appfolio_tenant,
)

# ─── detect_wix_html_iframes ─────────────────────────────────────────


def test_detect_finds_filesusr_iframe() -> None:
    html = (
        '<iframe src="https://www-millenniumnw-com.filesusr.com/html/'
        '790584_8774c5f3287cc8cbd5b49adeb9ba3765.html"></iframe>'
    )
    urls = detect_wix_html_iframes(html)
    assert len(urls) == 1
    assert urls[0].endswith(".html")
    assert "filesusr.com" in urls[0]


def test_detect_handles_multiple_iframes() -> None:
    html = (
        '<iframe src="https://a.filesusr.com/html/a1b2.html"></iframe>'
        '<iframe src="https://b.filesusr.com/html/c3d4.html"></iframe>'
    )
    urls = detect_wix_html_iframes(html)
    assert len(urls) == 2


def test_detect_dedupes_repeated_iframe() -> None:
    """Same iframe rendered twice (desktop + mobile breakpoint) → one URL."""
    html = (
        '<iframe src="https://a.filesusr.com/html/abc.html"></iframe>'
        '<iframe src="https://a.filesusr.com/html/abc.html"></iframe>'
    )
    urls = detect_wix_html_iframes(html)
    assert urls == ["https://a.filesusr.com/html/abc.html"]


def test_detect_returns_empty_for_no_match() -> None:
    assert detect_wix_html_iframes("") == []
    assert detect_wix_html_iframes("<html><body>no iframes</body></html>") == []
    assert detect_wix_html_iframes(
        '<iframe src="https://other.com/page"></iframe>'
    ) == []


def test_detect_tolerates_single_quoted_src() -> None:
    html = "<iframe src='https://a.filesusr.com/html/x.html'></iframe>"
    assert detect_wix_html_iframes(html) == [
        "https://a.filesusr.com/html/x.html"
    ]


def test_detect_tolerates_extra_attributes_before_src() -> None:
    html = (
        '<iframe id="foo" class="bar" name="baz" '
        'src="https://a.filesusr.com/html/x.html" '
        'style="border:0"></iframe>'
    )
    assert detect_wix_html_iframes(html) == [
        "https://a.filesusr.com/html/x.html"
    ]


# ─── extract_appfolio_tenant ─────────────────────────────────────────


def test_extract_tenant_from_canonical_appfolio_listing_init() -> None:
    """The canonical millenniumnw iframe body, verbatim."""
    body = (
        "<script>Appfolio.Listing({\n"
        "    hostUrl: 'newmpm.appfolio.com',\n"
        "    themeColor: '#676767',\n"
        "    height: '1000px'\n"
        "  });</script>"
    )
    assert extract_appfolio_tenant(body) == "newmpm.appfolio.com"


def test_extract_tenant_handles_double_quoted_host_url() -> None:
    body = 'Appfolio.Listing({ hostUrl: "newmpm.appfolio.com" })'
    assert extract_appfolio_tenant(body) == "newmpm.appfolio.com"


def test_extract_tenant_falls_back_to_bare_host_when_minified() -> None:
    """When the JS is minified, the ``hostUrl:`` token can be lost. We
    fall back to any ``'TENANT.appfolio.com'`` string literal."""
    body = 'eval("https://mintenant.appfolio.com/listings/js/foo.js")'
    assert extract_appfolio_tenant(body) == "mintenant.appfolio.com"


def test_extract_tenant_rejects_bare_appfolio_com() -> None:
    """``'appfolio.com'`` alone (no subdomain) is never a tenant host."""
    body = "var x = 'appfolio.com';"
    assert extract_appfolio_tenant(body) is None


def test_extract_tenant_returns_none_for_no_match() -> None:
    assert extract_appfolio_tenant("") is None
    assert extract_appfolio_tenant("<script>no appfolio here</script>") is None


def test_extract_tenant_returns_lowercase_host() -> None:
    body = "Appfolio.Listing({ hostUrl: 'NewMPM.AppFolio.com' })"
    assert extract_appfolio_tenant(body) == "newmpm.appfolio.com"


# ─── build_appfolio_listings_url ─────────────────────────────────────


def test_build_listings_url_from_tenant_host() -> None:
    assert (
        build_appfolio_listings_url("newmpm.appfolio.com")
        == "https://newmpm.appfolio.com/listings"
    )


def test_build_listings_url_returns_none_for_empty_input() -> None:
    assert build_appfolio_listings_url("") is None


def test_build_listings_url_returns_none_for_invalid_host() -> None:
    """Hosts without a TLD aren't valid AppFolio tenants."""
    assert build_appfolio_listings_url("nottld") is None
