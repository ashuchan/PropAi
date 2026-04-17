"""Stub adapter shared by Phase 2. Phase 3 replaces each stub with a real impl.

Every stub is callable (raises NotImplementedError on ``extract``) and reports
its static fingerprints so the detector and tests can exercise the wiring.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page


class StubAdapter:
    """Base stub. Subclasses set ``pms_name`` and ``_fingerprints``."""

    pms_name: str = ""
    _fingerprints: list[str] = []

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        raise NotImplementedError(
            f"{self.pms_name!r} adapter is a Phase 2 stub; Phase 3 replaces it."
        )

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
