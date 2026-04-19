"""Tests for conditional — ETag/Last-Modified SQLite cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ma_poc.fetch.conditional import ConditionalCache


def test_cond_cache_roundtrip_etag(tmp_path: Path) -> None:
    cache = ConditionalCache(tmp_path / "test.db")
    cache.write("https://example.com", '"abc123"', None)
    etag, lm = cache.read("https://example.com")
    assert etag == '"abc123"'
    assert lm is None
    cache.close()


def test_cond_cache_roundtrip_last_modified(tmp_path: Path) -> None:
    cache = ConditionalCache(tmp_path / "test.db")
    cache.write("https://example.com", None, "Mon, 01 Jan 2024 00:00:00 GMT")
    etag, lm = cache.read("https://example.com")
    assert etag is None
    assert lm == "Mon, 01 Jan 2024 00:00:00 GMT"
    cache.close()


def test_cond_cache_read_missing_url_returns_nones(tmp_path: Path) -> None:
    cache = ConditionalCache(tmp_path / "test.db")
    etag, lm = cache.read("https://nonexistent.example.com")
    assert etag is None
    assert lm is None
    cache.close()


def test_cond_cache_expire_removes_stale(tmp_path: Path) -> None:
    cache = ConditionalCache(tmp_path / "test.db")
    # Write an entry
    cache.write("https://old.example.com", '"old"', None)
    # Manually backdate the entry
    cache._conn.execute(
        "UPDATE conditional_cache SET fetched_at = '2020-01-01T00:00:00' WHERE url = ?",
        ("https://old.example.com",),
    )
    cache._conn.commit()
    removed = cache.expire_older_than(7)
    assert removed == 1
    etag, lm = cache.read("https://old.example.com")
    assert etag is None
    cache.close()


def test_cond_cache_thread_safe_writes(tmp_path: Path) -> None:
    cache = ConditionalCache(tmp_path / "test.db")

    def write_entry(i: int) -> None:
        cache.write(f"https://example.com/{i}", f'"etag_{i}"', None)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_entry, range(20)))

    # All 20 entries should exist
    for i in range(20):
        etag, _ = cache.read(f"https://example.com/{i}")
        assert etag == f'"etag_{i}"'
    cache.close()
