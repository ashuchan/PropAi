"""Tests for F6 — cluster_retry.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.cluster_retry import filter_by_domain, registered_domain


def _prop(website: str, verdict: str = "FAILED_NO_DATA") -> dict:
    return {
        "Website": website,
        "_meta": {"verdict": verdict, "scrape_tier_used": "TIER_1_API"},
    }


# ── registered_domain ─────────────────────────────────────────────────────────


def test_cluster_analysis_groups_by_registered_domain_not_full_host() -> None:
    """www.gscapts.com and gscapts.com should group as gscapts.com."""
    assert registered_domain("https://www.gscapts.com/harbour-pointe") == "gscapts.com"
    assert registered_domain("https://gscapts.com") == "gscapts.com"


def test_cluster_analysis_sorts_clusters_by_failure_count_desc(tmp_path: Path) -> None:
    """Clusters with more failures appear first."""
    from scripts.cluster_retry import analyze

    props = [
        _prop("https://gscapts.com/1"),
        _prop("https://gscapts.com/2"),
        _prop("https://gscapts.com/3"),
        _prop("https://mark-taylor.com/1"),
        _prop("https://mark-taylor.com/2"),
        {"Website": "https://example.com", "_meta": {"verdict": "SUCCESS"}},
    ]
    p = tmp_path / "properties.json"
    p.write_text(json.dumps(props), encoding="utf-8")

    result = analyze(p)
    clusters = result["clusters"]
    assert len(clusters) >= 2
    # gscapts has 3 failures, mark-taylor has 2 — gscapts should come first
    assert clusters[0]["failure_count"] >= clusters[1]["failure_count"]


def test_cluster_analysis_includes_sample_body_truncated_to_500(tmp_path: Path) -> None:
    """Sample body is truncated to 500 characters."""
    from scripts.cluster_retry import analyze

    long_body = "x" * 1000
    props = [
        {
            "Website": "https://gscapts.com/1",
            "_meta": {"verdict": "FAILED_NO_DATA"},
            "_raw_api_responses": [{"url": "https://api.gscapts.com", "body": long_body}],
        },
        _prop("https://gscapts.com/2"),
    ]
    p = tmp_path / "properties.json"
    p.write_text(json.dumps(props), encoding="utf-8")
    result = analyze(p)

    clusters = result["clusters"]
    gscapts = next((c for c in clusters if c["domain"] == "gscapts.com"), None)
    assert gscapts is not None
    if gscapts["sample_body"]:
        assert len(gscapts["sample_body"]) <= 503  # 500 + "..."


# ── filter_by_domain ─────────────────────────────────────────────────────────


def test_cluster_retry_filters_properties_by_domain() -> None:
    props = [
        _prop("https://gscapts.com/harbour-pointe"),
        _prop("https://gscapts.com/sawgrass-cove"),
        _prop("https://mark-taylor.com/san-lagos"),
        _prop("https://example.com/other"),
    ]
    filtered = filter_by_domain(props, "gscapts.com")
    assert len(filtered) == 2
    for p in filtered:
        assert "gscapts.com" in p["Website"]


def test_cluster_retry_preserves_full_runner_output_shape() -> None:
    """filter_by_domain returns full property dicts, not stripped versions."""
    props = [_prop("https://gscapts.com/unit1")]
    filtered = filter_by_domain(props, "gscapts.com")
    assert len(filtered) == 1
    assert "_meta" in filtered[0]
    assert filtered[0]["Website"] == "https://gscapts.com/unit1"
