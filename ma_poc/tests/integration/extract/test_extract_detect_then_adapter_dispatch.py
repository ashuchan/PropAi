"""Extract layer: PMS detection → adapter dispatch.

Verifies that `pms.detector.detect_pms` correctly identifies PMS from URL/HTML
signals, and that `pms.adapters.registry.get_adapter` routes to the right adapter.
"""

from __future__ import annotations


from ma_poc.pms.adapters.appfolio import AppFolioAdapter
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.adapters.funnel import FunnelAdapter
from ma_poc.pms.adapters.g5 import G5Adapter
from ma_poc.pms.adapters.generic import GenericAdapter
from ma_poc.pms.adapters.knock import KnockAdapter
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter
from ma_poc.pms.adapters.registry import get_adapter
from pms.detector import detect_pms

# Ensure the registry is bootstrapped under the ma_poc.* namespace so
# get_adapter() finds the registered adapters (avoids the dual-import split
# between `pms.adapters` and `ma_poc.pms.adapters` registries).
import ma_poc.pms.adapters as _adapters_pkg  # noqa: F401  — side-effect import


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------


def test_detect_rentcafe_from_host() -> None:
    detected = detect_pms("https://property.rentcafe.com/apartments/tx/dallas/")
    assert detected.pms == "rentcafe"
    assert detected.confidence >= 0.9


def test_detect_entrata_from_host() -> None:
    detected = detect_pms("https://www.exampleproperty.entrata.com/")
    assert detected.pms == "entrata"
    assert detected.confidence >= 0.9


def test_detect_appfolio_from_host() -> None:
    detected = detect_pms("https://testproperty.appfolio.com/listings")
    assert detected.pms == "appfolio"
    assert detected.confidence >= 0.9


def test_detect_unknown_returns_unknown() -> None:
    detected = detect_pms("https://www.genericapartments.com/floorplans")
    assert detected.pms == "unknown"


def test_detect_never_raises_on_bad_input() -> None:
    """detect_pms must never propagate exceptions — bad inputs return 'unknown'."""
    result = detect_pms("")
    assert result.pms == "unknown"
    result2 = detect_pms(None)  # type: ignore[arg-type]
    assert result2.pms == "unknown"


def test_detect_rentcafe_from_html_signal() -> None:
    html = '<script src="https://cdn.rentcafe.com/widgets/v2/main.js"></script>'
    detected = detect_pms("https://genericprop.com/floorplans", page_html=html)
    assert detected.pms == "rentcafe"


# ---------------------------------------------------------------------------
# Adapter dispatch tests
# ---------------------------------------------------------------------------


def test_get_adapter_rentcafe_returns_rentcafe_adapter() -> None:
    adapter = get_adapter("rentcafe")
    assert isinstance(adapter, RentCafeAdapter)
    assert adapter.pms_name == "rentcafe"


def test_get_adapter_entrata_returns_entrata_adapter() -> None:
    adapter = get_adapter("entrata")
    assert isinstance(adapter, EntrataAdapter)
    assert adapter.pms_name == "entrata"


def test_get_adapter_appfolio_returns_appfolio_adapter() -> None:
    adapter = get_adapter("appfolio")
    assert isinstance(adapter, AppFolioAdapter)
    assert adapter.pms_name == "appfolio"


def test_get_adapter_unknown_falls_back_to_generic() -> None:
    adapter = get_adapter("unknown")
    assert isinstance(adapter, GenericAdapter)


def test_get_adapter_custom_falls_back_to_generic() -> None:
    adapter = get_adapter("custom")
    assert isinstance(adapter, GenericAdapter)


def test_get_adapter_unrecognised_name_falls_back_to_generic() -> None:
    # Any unregistered PMS name that is NOT in _FALLBACK_NAMES should
    # also fall through to generic (registry.get_adapter fallback branch).
    adapter = get_adapter("totally_new_pms_xyz")
    assert isinstance(adapter, GenericAdapter)


# ---------------------------------------------------------------------------
# 2026-05-13: detect→dispatch chain for the new/expanded routes
# ---------------------------------------------------------------------------


def test_get_adapter_knock_returns_knock_adapter() -> None:
    """Knock was unwired before 2026-05-13. Confirm full registration."""
    adapter = get_adapter("knock")
    assert isinstance(adapter, KnockAdapter)
    assert adapter.pms_name == "knock"


def test_get_adapter_g5_returns_g5_adapter() -> None:
    """G5 is a new adapter as of 2026-05-13."""
    adapter = get_adapter("g5")
    assert isinstance(adapter, G5Adapter)
    assert adapter.pms_name == "g5"


def test_detect_then_dispatch_knock_chain() -> None:
    """Page with doorway.knck.io routes to KnockAdapter end-to-end."""
    html = '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
    detected = detect_pms("https://www.liveatcalista.com/", page_html=html)
    assert detected.pms == "knock"
    adapter = get_adapter(detected.pms)
    assert isinstance(adapter, KnockAdapter)


def test_detect_then_dispatch_g5_chain_via_inventory_host() -> None:
    """Page referencing inventory.g5marketingcloud routes to G5Adapter."""
    html = '<script src="https://inventory.g5marketingcloud.com/graphql"></script>'
    detected = detect_pms("https://www.morgan-properties.com/x", page_html=html)
    assert detected.pms == "g5"
    adapter = get_adapter(detected.pms)
    assert isinstance(adapter, G5Adapter)


def test_detect_then_dispatch_g5_chain_via_g5dxm_themes() -> None:
    """2026-05-13: themes.g5dxm.com is G5's theme CDN — must route to G5."""
    html = '<script src="https://themes.g5dxm.com/themes/g5-cs-x/main.js"></script>'
    detected = detect_pms("https://example-g5-property.com/", page_html=html)
    assert detected.pms == "g5"
    assert isinstance(get_adapter(detected.pms), G5Adapter)


def test_detect_then_dispatch_funnel_chain_via_funnelleasing_subdomain() -> None:
    """2026-05-13: ``apply.funnelleasing.com`` and ``bh.funnelleasing.com``
    are customer-specific Funnel portal subdomains. Detector must route
    these to Funnel, NOT to RentCafe via Pass-3 weak markers."""
    html = (
        '<a href="https://apply.funnelleasing.com/1170">Apply</a>'
        '<img src="https://cdn.rentcafe.com/something.png">'  # residual rentcafe noise
    )
    detected = detect_pms("https://livebh.com/x/", page_html=html)
    assert detected.pms == "funnel"
    assert isinstance(get_adapter(detected.pms), FunnelAdapter)


def test_detect_then_dispatch_funnel_chain_via_nestio_contact_widget() -> None:
    """Nestio = Funnel (acquired). Detector must route Nestio-widget pages
    to Funnel."""
    html = '<script src="https://integrations.nestio.com/contact-widget/v1/integration.js"></script>'
    detected = detect_pms("https://example-nestio-property.com/", page_html=html)
    assert detected.pms == "funnel"
    assert isinstance(get_adapter(detected.pms), FunnelAdapter)


def test_detect_entrata_application_authentication_path_no_longer_misroutes() -> None:
    """2026-05-13: 260 properties were mis-classified TIER_1_API_ENTRATA
    because the page only linked to Entrata's tenant login form
    ``/Apartments/module/application_authentication/`` — not an actual
    Entrata widget embed. Detector must NOT route those to Entrata."""
    html = '<a href="/Apartments/module/application_authentication/">Sign In</a>'
    detected = detect_pms("https://www.foxchaseofalexandriaapts.com/", page_html=html)
    assert detected.pms != "entrata"


def test_detect_real_entrata_widget_path_still_routes_to_entrata() -> None:
    """Counter-regression: a real Entrata widget embed must still route."""
    html = '<iframe src="/Apartments/module/floor_plans/property/12345"></iframe>'
    detected = detect_pms("https://example-entrata-property.com/", page_html=html)
    assert detected.pms == "entrata"
    assert isinstance(get_adapter(detected.pms), EntrataAdapter)
