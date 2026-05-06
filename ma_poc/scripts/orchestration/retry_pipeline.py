"""
scripts/orchestration/retry_pipeline.py
========================================

RetryPipeline — placeholder for the Jugnu retry path.

The legacy ``retry_runner.py`` has been deleted (PR-9).  Retry/resume
functionality is now handled by ``scripts/jugnu_retry_runner.py``.
This class is kept as a thin interface shim so existing call-sites that
reference ``RetryPipeline`` do not break at import time.

Example usage::

    from scripts.orchestration.retry_pipeline import RetryPipeline

    pipeline = RetryPipeline()
    # Use jugnu_retry_runner directly for actual retry logic.
"""
from __future__ import annotations

from typing import Any


class RetryPipeline:
    """Placeholder wrapper for the Jugnu retry pipeline.

    The legacy ``retry_runner.run_retry`` this class previously delegated
    to has been removed (PR-9).  Callers should use
    ``scripts.jugnu_retry_runner`` directly for retry/resume runs.
    """

    async def run(self, **kwargs: Any) -> dict:
        """Not implemented — use jugnu_retry_runner instead.

        Raises:
            NotImplementedError: Always.  The legacy retry_runner backend
                has been deleted.  Retry logic now lives in
                ``scripts/jugnu_retry_runner.py``.
        """
        raise NotImplementedError(
            "RetryPipeline.run() is a stub.  "
            "Use scripts/jugnu_retry_runner.py for retry/resume runs."
        )
