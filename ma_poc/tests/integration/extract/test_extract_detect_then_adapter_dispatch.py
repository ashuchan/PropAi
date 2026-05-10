"""Extract layer: PMS detection → adapter dispatch.

Verifies that `pms.detector.detect_pms` correctly identifies PMS from URL/HTML
signals, and that `pms.adapters.registry.get_adapter` routes to the right adapter.
"""

from __future__ import annotations


from ma_poc.pms.adapters.appfolio import AppFolioAdapter
from ma_poc.pms.adapters.entrata import EntrataAdapter
from ma_poc.pms.adapters.generic import GenericAdapter
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
