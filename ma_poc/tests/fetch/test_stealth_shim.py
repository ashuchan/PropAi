"""S1 tests — exactly one Playwright shim, no double-import, additive Identity schema."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FETCH_DIR = REPO_ROOT / "ma_poc" / "fetch"
AUDIT_DOC = REPO_ROOT / "docs" / "STEALTH_SHIM_AUDIT.md"

_FORBIDDEN_SHIMS = ("playwright_stealth",)
_PLAYWRIGHT_SHIMS = ("patchright", "rebrowser_playwright", "playwright")
# Anti-detection ("stealth") Playwright forks. At most ONE of these may be
# used in fetch/ — mixing stealth forks is the accident the S1 rule guards
# against. Vanilla ``playwright`` is NOT a stealth fork: it is deliberately
# used by the clean "2a" residential-render tier
# (fetch/providers/residential_render.py), which must ship a real, un-patched
# browser. That documented exception is why it is excluded here. See
# docs/STEALTH_SHIM_AUDIT.md.
_STEALTH_SHIMS = ("patchright", "rebrowser_playwright")


def _read_all_py(root: Path) -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in root.rglob("*.py")}


def test_s1_no_playwright_stealth_imports() -> None:
    """`playwright_stealth` is forbidden — patchright supersedes it."""
    offenders: list[str] = []
    for path, text in _read_all_py(REPO_ROOT / "ma_poc").items():
        for forbidden in _FORBIDDEN_SHIMS:
            if f"import {forbidden}" in text or f"from {forbidden}" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"forbidden shim imports found: {offenders}"


def test_s1_single_playwright_shim() -> None:
    """At most ONE stealth Playwright fork in fetch/.

    Vanilla ``playwright`` is permitted (the documented 2a exception — the
    clean residential-render tier deliberately uses a non-stealth browser);
    the rule only forbids mixing multiple STEALTH forks.
    """
    found: dict[str, list[str]] = {s: [] for s in _STEALTH_SHIMS}
    for path, text in _read_all_py(FETCH_DIR).items():
        for shim in _STEALTH_SHIMS:
            if f"from {shim}." in text or f"import {shim}" in text:
                found[shim].append(str(path.relative_to(REPO_ROOT)))
    used = [s for s, files in found.items() if files]
    assert len(used) <= 1, (
        f"expected at most one STEALTH shim in ma_poc/fetch/; found {len(used)}: "
        f"{used}. Details: {found}"
    )


def test_s1_audit_doc_exists() -> None:
    assert AUDIT_DOC.exists(), (
        f"missing audit doc at {AUDIT_DOC}. S1 mandates documenting the chosen shim "
        f"and the pre-audit findings, even if the choice is vanilla."
    )
    text = AUDIT_DOC.read_text(encoding="utf-8")
    assert "## Pre-PR audit" in text, "audit doc missing `## Pre-PR audit` heading"
    assert any(s in text for s in _PLAYWRIGHT_SHIMS), (
        "audit doc must name which shim is being used"
    )


def test_s1_identity_schema_additive_only() -> None:
    """All 8 existing identities still have all 4 original fields populated."""
    from ma_poc.fetch.stealth import Identity, _IDENTITIES

    required_existing = {"user_agent", "accept_language", "platform", "viewport"}
    assert len(_IDENTITIES) == 8, (
        f"identity count changed: expected 8, got {len(_IDENTITIES)}. "
        f"PR-1 must not reduce the pool."
    )
    for i, ident in enumerate(_IDENTITIES):
        for field in required_existing:
            assert hasattr(ident, field), f"identity[{i}] missing field {field}"
            assert getattr(ident, field) is not None, (
                f"identity[{i}] field {field} is None — must remain populated"
            )
