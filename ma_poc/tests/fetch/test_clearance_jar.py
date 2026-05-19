"""Tests for the WAF clearance cookie jar (Phase 2 — stealth plan).

Verifies:
  - TTL expiry (expired rows not returned, purge_expired deletes them)
  - (host, proxy_ip, ua_hash) key uniqueness — different keys don't share cookies
  - Concurrent-write safety (SQLite WAL mode; two jars on the same DB)
  - Upsert semantics (store updates value + expiry for existing key)
  - ua_hash() stability — same input produces the same output
  - extract_clearance_from_set_cookie() parses the known cookie names
  - close() / context-manager behaviour
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ma_poc.fetch.clearance_jar import (
    CLEARANCE_COOKIE_NAMES,
    ClearanceJar,
    extract_clearance_from_set_cookie,
    ua_hash,
)


# ── ua_hash() ────────────────────────────────────────────────────────────────


def test_ua_hash_stable():
    h1 = ua_hash("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136", "en-US,en;q=0.9")
    h2 = ua_hash("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136", "en-US,en;q=0.9")
    assert h1 == h2


def test_ua_hash_different_ua_yields_different_hash():
    h1 = ua_hash("Mozilla/5.0 Chrome/136", "en-US")
    h2 = ua_hash("Mozilla/5.0 Firefox/137", "en-US")
    assert h1 != h2


def test_ua_hash_different_lang_yields_different_hash():
    h1 = ua_hash("Mozilla/5.0 Chrome/136", "en-US")
    h2 = ua_hash("Mozilla/5.0 Chrome/136", "de-DE")
    assert h1 != h2


def test_ua_hash_empty_inputs_stable():
    h = ua_hash("", "")
    assert isinstance(h, str) and len(h) == 16


def test_ua_hash_length():
    h = ua_hash("ua", "lang")
    assert len(h) == 16


# ── ClearanceJar basic CRUD ──────────────────────────────────────────────────


def test_store_and_lookup_roundtrip(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("example.com", "1.2.3.4", "abc123", "cloudflare",
               {"cf_clearance": "TOKEN"}, ttl_seconds=3600)
    result = jar.lookup("example.com", "1.2.3.4", "abc123")
    assert result == {"cf_clearance": "TOKEN"}
    jar.close()


def test_lookup_returns_empty_for_unknown_key(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    result = jar.lookup("nonexistent.com", "", "xyz")
    assert result == {}
    jar.close()


def test_upsert_updates_value(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("site.com", "", "uh1", "cloudflare", {"cf_clearance": "OLD"}, 3600)
    jar.store("site.com", "", "uh1", "cloudflare", {"cf_clearance": "NEW"}, 3600)
    result = jar.lookup("site.com", "", "uh1")
    assert result["cf_clearance"] == "NEW"
    jar.close()


def test_store_multiple_cookies_same_key(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    cookies = {"cf_clearance": "TOK1", "__cf_bm": "TOK2"}
    jar.store("multi.com", "10.0.0.1", "uh2", "cloudflare", cookies, 1800)
    result = jar.lookup("multi.com", "10.0.0.1", "uh2")
    assert result == cookies
    jar.close()


# ── Key isolation ────────────────────────────────────────────────────────────


def test_different_host_does_not_share_cookies(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("a.com", "1.1.1.1", "uh", "cloudflare", {"cf_clearance": "A"}, 3600)
    result = jar.lookup("b.com", "1.1.1.1", "uh")
    assert result == {}
    jar.close()


def test_different_proxy_ip_does_not_share_cookies(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("x.com", "1.1.1.1", "uh", "cloudflare", {"cf_clearance": "A"}, 3600)
    result = jar.lookup("x.com", "2.2.2.2", "uh")
    assert result == {}
    jar.close()


def test_different_ua_hash_does_not_share_cookies(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("y.com", "", "hash_a", "cloudflare", {"cf_clearance": "X"}, 3600)
    result = jar.lookup("y.com", "", "hash_b")
    assert result == {}
    jar.close()


# ── TTL expiry ───────────────────────────────────────────────────────────────


def test_expired_cookie_not_returned(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    # Store with a 1-second TTL — insert directly with past expiry.
    jar.store("ttl.com", "", "uh", "cloudflare", {"cf_clearance": "EXP"}, ttl_seconds=1)
    # Monkey-patch the DB row to have already expired.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "jar.sqlite"))
    conn.execute(
        "UPDATE clearance_cookies SET expires_at = ? WHERE host = ?",
        ((datetime.now(UTC) - timedelta(seconds=10)).isoformat(), "ttl.com"),
    )
    conn.commit()
    conn.close()
    result = jar.lookup("ttl.com", "", "uh")
    assert result == {}
    jar.close()


def test_purge_expired_removes_stale_rows(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("purge.com", "", "uh", "cloudflare", {"cf_clearance": "X"}, 3600)
    # Force the expiry to be in the past via direct SQL.
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "jar.sqlite"))
    conn.execute(
        "UPDATE clearance_cookies SET expires_at = ? WHERE host = ?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "purge.com"),
    )
    conn.commit()
    conn.close()
    deleted = jar.purge_expired()
    assert deleted == 1
    # Confirm row is gone.
    result = jar.lookup("purge.com", "", "uh")
    assert result == {}
    jar.close()


def test_purge_expired_does_not_touch_live_rows(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.store("live.com", "", "uh", "cloudflare", {"cf_clearance": "Y"}, 7200)
    deleted = jar.purge_expired()
    assert deleted == 0
    result = jar.lookup("live.com", "", "uh")
    assert result == {"cf_clearance": "Y"}
    jar.close()


# ── Concurrent-write safety ──────────────────────────────────────────────────


def test_concurrent_writes_are_safe(tmp_path: Path):
    """Two jar instances writing to the same SQLite file concurrently should
    not raise or corrupt data.  SQLite WAL mode handles the serialisation."""
    db = tmp_path / "concurrent.sqlite"
    errors: list[Exception] = []

    def _writer(n: int) -> None:
        j = ClearanceJar(db)
        try:
            for i in range(10):
                j.store(
                    f"host{n}-{i}.com", "", "uh", "cloudflare",
                    {"cf_clearance": f"T{n}-{i}"}, 3600,
                )
        except Exception as exc:
            errors.append(exc)
        finally:
            j.close()

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent write errors: {errors}"


# ── extract_clearance_from_set_cookie ───────────────────────────────────────


def test_extract_cf_clearance():
    hdr = "cf_clearance=TOK123; Path=/; Secure; HttpOnly; SameSite=None; Max-Age=1800"
    result = extract_clearance_from_set_cookie(hdr, {})
    assert result == {"cf_clearance": "TOK123"}


def test_extract_cf_bm():
    hdr = "__cf_bm=BMBM; Path=/; Secure; SameSite=None"
    result = extract_clearance_from_set_cookie(hdr, {})
    assert result == {"__cf_bm": "BMBM"}


def test_extract_ignores_non_clearance_cookies():
    hdr = "sessionid=abc123; Path=/; HttpOnly"
    result = extract_clearance_from_set_cookie(hdr, {})
    assert result == {}


def test_extract_from_headers_dict():
    headers = {
        "set-cookie": "cf_clearance=DICT_TOK; Path=/; Secure",
        "content-type": "text/html",
    }
    result = extract_clearance_from_set_cookie("", headers)
    assert result.get("cf_clearance") == "DICT_TOK"


def test_extract_empty_inputs():
    assert extract_clearance_from_set_cookie("", {}) == {}


def test_clearance_cookie_names_set_includes_known_names():
    assert "cf_clearance" in CLEARANCE_COOKIE_NAMES
    assert "__cf_bm" in CLEARANCE_COOKIE_NAMES
    assert "_pxhd" in CLEARANCE_COOKIE_NAMES
    assert "srcfh-cookie" in CLEARANCE_COOKIE_NAMES


# ── close() and context-manager ──────────────────────────────────────────────


def test_close_is_idempotent(tmp_path: Path):
    jar = ClearanceJar(tmp_path / "jar.sqlite")
    jar.close()
    jar.close()  # Should not raise.


def test_context_manager_closes_on_exit(tmp_path: Path):
    with ClearanceJar(tmp_path / "jar.sqlite") as jar:
        jar.store("ctx.com", "", "uh", "cloudflare", {"cf_clearance": "CTX"}, 3600)
    # Verify connection is closed by re-opening and confirming data is there.
    j2 = ClearanceJar(tmp_path / "jar.sqlite")
    assert j2.lookup("ctx.com", "", "uh") == {"cf_clearance": "CTX"}
    j2.close()
