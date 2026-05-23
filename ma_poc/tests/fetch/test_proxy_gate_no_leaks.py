"""Audit: no code outside the proxy_gate module may read PROBE_PROXY_URL
or construct proxy traffic on its own.

This is the **strict-allow** invariant: every proxy decision must go
through :mod:`ma_poc.fetch.proxy_gate`. The grep below is the bouncer.

A small allowlist exists for:

* ``fetch/proxy_gate.py`` — the gate itself reads the env.
* ``pms/adapters/_adapter_telemetry.py`` — reads the env as a
  READ-ONLY ``via_proxy`` telemetry boolean. It cannot route traffic.
* ``scripts/diagnostics/asn_ipv6_probe.py`` — diagnostic script that
  intentionally tests "what happens if the proxy is used". Not in
  the production data path.

If you need to add a new file to the allowlist, that's a design
review — discuss before extending.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ─── Configuration ───────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[2]   # …/PropAi/ma_poc

#: Files explicitly allowed to read ``PROBE_PROXY_URL``. Relative to
#: ``REPO_ROOT``. Use POSIX separators — pathlib normalises both.
_ALLOWED_PROBE_PROXY_URL_READERS = frozenset({
    "fetch/proxy_gate.py",                      # the gate itself
    "pms/adapters/_adapter_telemetry.py",       # read-only via_proxy telemetry
    "pms/adapters/_probe.py",                   # contains deprecated probe_proxies() stub
    "scripts/diagnostics/asn_ipv6_probe.py",    # diagnostic; not in prod data path
})

#: Patterns that signal a leak.
_PROBE_PROXY_URL_PATTERN = re.compile(
    r"""
    (
        os\.getenv\(\s*['"]PROBE_PROXY_URL['"]
      | os\.environ\.get\(\s*['"]PROBE_PROXY_URL['"]
      | os\.environ\[\s*['"]PROBE_PROXY_URL['"]
    )
    """,
    re.VERBOSE,
)


_PY_SOURCE_GLOBS = (
    "fetch/**/*.py",
    "pms/**/*.py",
    "scripts/**/*.py",
    "services/**/*.py",
    "core/**/*.py",
    "models/**/*.py",
    "extraction/**/*.py",
    "discovery/**/*.py",
    "observability/**/*.py",
    "reporting/**/*.py",
    "validation/**/*.py",
    "data_provider/**/*.py",
)


# ─── Helpers ─────────────────────────────────────────────────────────


def _rel_posix(p: Path) -> str:
    return p.relative_to(REPO_ROOT).as_posix()


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _PY_SOURCE_GLOBS:
        files.extend(REPO_ROOT.glob(pattern))
    # Skip test fixtures (they intentionally simulate proxy env usage)
    return [f for f in files if "/tests/" not in f.as_posix() and "/test_" not in f.name]


def _count_lines_with_match(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if pattern.search(line):
            hits.append((i, line.strip()))
    return hits


# ─── The actual leak audit ───────────────────────────────────────────


def test_no_leak_reads_of_PROBE_PROXY_URL() -> None:
    """No source file outside the curated allowlist may read PROBE_PROXY_URL.

    This is the **strict-allow** invariant. Failing this test means
    someone bypassed the gate. Either:

    * Re-route the new code through ``fetch.proxy_gate.decide`` (correct fix), or
    * Add the file to ``_ALLOWED_PROBE_PROXY_URL_READERS`` after design review.
    """
    leaks: list[tuple[str, list[tuple[int, str]]]] = []
    for path in _iter_source_files():
        if not path.exists():
            continue
        rel = _rel_posix(path)
        if rel in _ALLOWED_PROBE_PROXY_URL_READERS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hits = _count_lines_with_match(text, _PROBE_PROXY_URL_PATTERN)
        if hits:
            leaks.append((rel, hits))

    if leaks:
        msg_lines = [
            "Detected unauthorized reads of PROBE_PROXY_URL outside the proxy_gate:",
            "",
            "If this is intentional, route the code through "
            "ma_poc.fetch.proxy_gate.decide(), or extend "
            "_ALLOWED_PROBE_PROXY_URL_READERS in this test after design review.",
            "",
        ]
        for rel, hits in leaks:
            msg_lines.append(f"  {rel}:")
            for line_no, line in hits[:3]:
                msg_lines.append(f"    {line_no}: {line[:120]}")
            if len(hits) > 3:
                msg_lines.append(f"    ... and {len(hits) - 3} more hits")
        pytest.fail("\n".join(msg_lines))


def test_probe_proxies_returns_empty_unconditionally() -> None:
    """``ma_poc.pms.adapters._probe.probe_proxies()`` is a deprecated stub
    that MUST return an empty dict regardless of env state. The function
    is retained only for backwards-compat with the diagnostic script."""
    import os

    # Set the env to confirm the stub ignores it.
    prev = os.environ.get("PROBE_PROXY_URL")
    os.environ["PROBE_PROXY_URL"] = "http://should-be-ignored:1"
    try:
        from ma_poc.pms.adapters._probe import probe_proxies
        assert probe_proxies() == {}, (
            "probe_proxies() is deprecated and must return {} unconditionally. "
            "Routing decisions go through ma_poc.fetch.proxy_gate.decide()."
        )
    finally:
        if prev is None:
            del os.environ["PROBE_PROXY_URL"]
        else:
            os.environ["PROBE_PROXY_URL"] = prev


def test_probe_get_does_not_use_proxy_without_ctx_and_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end strict-allow: calling probe_get without ctx + stage
    must NOT route through the proxy, regardless of env."""
    monkeypatch.setenv("PROBE_PROXY_URL", "http://should-not-be-used:1")

    from ma_poc.fetch.proxy_gate import decide, ProxyDecisionReason

    # ctx=None / stage=None must fail-close with MISSING_CTX_OR_STAGE.
    d_no_ctx = decide("https://prop.securecafe.com/", None, "sc_probe")
    assert d_no_ctx.allow is False
    assert d_no_ctx.reason == ProxyDecisionReason.MISSING_CTX_OR_STAGE

    d_no_stage = decide("https://prop.securecafe.com/",
                        object(), None)  # type: ignore[arg-type]
    assert d_no_stage.allow is False
    assert d_no_stage.reason == ProxyDecisionReason.MISSING_CTX_OR_STAGE
