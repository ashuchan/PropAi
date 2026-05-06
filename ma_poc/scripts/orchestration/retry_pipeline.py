"""
scripts/orchestration/retry_pipeline.py
========================================

RetryPipeline — thin delegation wrapper for the Jugnu retry path.

The legacy ``retry_runner.py`` has been deleted (PR-9).  Retry/resume
functionality lives in ``scripts/jugnu_retry_runner.py``.
This class delegates ``run()`` to that module so existing call-sites keep
working without directly importing jugnu_retry_runner.

Example usage::

    from scripts.orchestration.retry_pipeline import RetryPipeline

    result = await RetryPipeline().run(
        retry_errors=True, run_date="2026-05-06"
    )
"""
from __future__ import annotations

from typing import Any


class RetryPipeline:
    """Thin delegation wrapper around ``scripts.jugnu_retry_runner``.

    Provides a stable import path for callers that used to reference the
    now-deleted ``retry_runner.py``.  All keyword arguments are forwarded
    to :func:`jugnu_retry_runner.run_retry`.
    """

    async def run(self, **kwargs: Any) -> dict:
        """Delegate to ``jugnu_retry_runner.run_retry(**kwargs)``.

        Accepted kwargs mirror ``jugnu_retry_runner``'s CLI flags:
        ``retry_errors``, ``resume``, ``run_date``, ``limit``, ``csv``, etc.

        Returns:
            dict with ``properties``, ``run_dir``, and summary counters.
        """
        try:
            from scripts import jugnu_retry_runner as _rr
        except ImportError:
            from ma_poc.scripts import jugnu_retry_runner as _rr  # type: ignore[no-redef]

        return await _rr.run_retry(**kwargs)
