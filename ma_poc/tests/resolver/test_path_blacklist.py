"""F1 — AppFolio rental_applications added to resolver path blacklist."""

from __future__ import annotations

from pathlib import Path

import pytest

from ma_poc.pms.resolver import is_blacklisted_path

# parents[0]=resolver, [1]=tests, [2]=ma_poc, [3]=PropAi
_REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "url,expected",
    [
        # F1 — must be blacklisted
        ("https://example.appfolio.com/listings/rental_applications/new", True),
        ("https://prop.appfolio.com/listings/rental_applications/", True),
        ("https://prop.appfolio.com/listings/rental_applications/new?source=cta", True),
        ("https://PROP.APPFOLIO.COM/Listings/Rental_Applications/New", True),
        # Existing entries unaffected
        ("https://example.com/scheduletour", True),
        ("https://example.com/contact", True),
        # Negative
        ("https://example.com/listings/", False),
        ("https://example.com/listings/abc-123/", False),
        ("https://example.com/floorplans", False),
    ],
)
def test_f1_appfolio_rental_application_path_excluded(url: str, expected: bool) -> None:
    assert is_blacklisted_path(url) is expected


def test_f1_blacklist_single_source_of_truth() -> None:
    """H1: 'rental_applications' must appear in exactly one production file."""
    matches: list[str] = []
    for f in (_REPO / "ma_poc").rglob("*.py"):
        if "tests" in f.parts or "scripts" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "rental_applications" in text:
            matches.append(str(f.relative_to(_REPO)).replace("\\", "/"))
    assert matches == ["ma_poc/pms/resolver.py"], (
        f"rental_applications appears in multiple production files: {matches}"
    )
