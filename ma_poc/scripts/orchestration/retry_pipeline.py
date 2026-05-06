"""
scripts/orchestration/retry_pipeline.py
========================================

RetryPipeline — class wrapper around retry_runner.run_retry().

Provides an object-oriented entry point for running a retry/resume pass
over the legacy daily_runner pipeline.  The actual logic lives in
``scripts/retry_runner.py``; this class simply delegates to it so
callers can program against a consistent ``pipeline.run(**kwargs)``
interface without depending directly on the module-level function.

Example usage::

    from scripts.orchestration.retry_pipeline import RetryPipeline
    from pathlib import Path

    pipeline = RetryPipeline()
    report = await pipeline.run(
        csv_path=Path("config/properties.csv"),
        run_date="2026-05-06",
        data_dir=Path("data"),
        mode="retry_errors",
        limit=None,
        proxy=None,
        scrape_timeout_s=180,
    )
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class RetryPipeline:
    """Thin class wrapper around :func:`scripts.retry_runner.run_retry`.

    Encapsulates the retry/resume pipeline so it can be referenced as an
    object and later extended (e.g. to inherit from a future DailyPipeline
    base class once PR-9 creates it).
    """

    async def run(self, **kwargs: Any) -> dict:
        """Execute a retry/resume pass over the pipeline.

        All keyword arguments are forwarded verbatim to
        :func:`scripts.retry_runner.run_retry`.  Required keyword
        arguments mirror that function's signature:

        Args:
            csv_path (Path): Path to the properties CSV file.
            run_date (str): Date string ``YYYY-MM-DD`` of the run to retry.
            data_dir (Path): Root data directory (e.g. ``Path("data")``).
            mode (str): ``"resume"`` or ``"retry_errors"``.
            limit (int | None): Maximum number of properties to process.
            proxy (str | None): Optional proxy URL.
            scrape_timeout_s (int): Per-property scrape timeout in seconds.
            schema_version (str): ``"v1"`` or ``"v2"`` (default ``"v1"``).

        Returns:
            dict: The run report produced by ``run_retry``.
        """
        # Deferred import to avoid pulling in all of retry_runner's heavy
        # dependencies (Playwright, dotenv, etc.) at import time.
        from scripts.retry_runner import run_retry  # noqa: PLC0415

        return await run_retry(**kwargs)
