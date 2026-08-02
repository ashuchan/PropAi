"""Content-addressed primary HTML persistence for offline replay."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime

from ma_poc.fetch.fetcher import _persist_raw_html


def test_raw_html_keeps_latest_pointer_and_immutable_hash_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    """An identical body reuses its SHA path while legacy replay still works."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    body = b"<html><body><div data-unit='101'>Available</div></body></html>"

    first = _persist_raw_html("42", body)
    second = _persist_raw_html("42", body)

    assert first == second
    assert first is not None
    assert first["response_sha256"] == hashlib.sha256(body).hexdigest()
    immutable = tmp_path / first["path"]
    assert gzip.decompress(immutable.read_bytes()) == body

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    latest = tmp_path / "raw_html" / date / "42.html.gz"
    assert gzip.decompress(latest.read_bytes()) == body
    metadata_path = immutable.with_name(immutable.name.removesuffix(".html.gz") + ".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["response_sha256"] == first["response_sha256"]
    assert metadata["body_bytes"] == len(body)
