"""
PR-8 tests: RetryPipeline class in scripts/orchestration/retry_pipeline.py

TDD: Write tests first, then implement.
"""
from __future__ import annotations

import asyncio
import inspect


def test_retry_pipeline_importable():
    """RetryPipeline can be imported from scripts.orchestration.retry_pipeline."""
    from scripts.orchestration.retry_pipeline import RetryPipeline  # noqa: F401


def test_retry_pipeline_has_run_method():
    """RetryPipeline has a 'run' method."""
    from scripts.orchestration.retry_pipeline import RetryPipeline

    pipeline = RetryPipeline()
    assert hasattr(pipeline, "run"), "RetryPipeline must have a 'run' method"
    assert callable(pipeline.run), "'run' must be callable"


def test_retry_pipeline_run_is_async():
    """RetryPipeline.run is an async coroutine function."""
    from scripts.orchestration.retry_pipeline import RetryPipeline

    pipeline = RetryPipeline()
    assert asyncio.iscoroutinefunction(pipeline.run), (
        "RetryPipeline.run must be an async coroutine function"
    )


def test_retry_pipeline_run_signature():
    """RetryPipeline.run accepts **kwargs."""
    from scripts.orchestration.retry_pipeline import RetryPipeline

    sig = inspect.signature(RetryPipeline.run)
    params = sig.parameters
    # Must accept **kwargs or explicit keyword args
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    assert has_var_keyword or len(params) > 1, (
        "RetryPipeline.run must accept keyword arguments"
    )


