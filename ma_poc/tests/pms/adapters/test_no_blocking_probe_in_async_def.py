"""Structural guard: no bare blocking probe inside an ``async def``.

WHY THIS FILE EXISTS — two mutations survived without it. Reverting
``await asyncio.to_thread(web_unlocker_get, …)`` to the bare blocking call at
``rentcafe.py:~1573`` and at ``onesite.py:~640`` BOTH passed the full suite
with a byte-identical failure set. Those two off-loads are the fix for the
2026-07-25 event-loop-starvation RCA (a synchronous probe on the loop → mean
property 305s against a 600s cap → the timeout victims rotate between runs),
and nothing in the suite would have caught a refactor putting them back.

``_probe.probe_get`` and ``web_unlocker_get`` are blocking transport calls.
Called bare inside a coroutine they park the WHOLE event loop — every other
property in the shard included — for the full timeout. ``hb_raw_get`` is an
async Playwright coroutine and must be awaited normally (never sent to a worker
thread).

Scope: a RATCHET, not a clean sweep. 40-odd such sites exist across ma_poc and
fixing them all is a separate piece of work; this test pins the functions that
have been fixed, so they cannot silently regress, and fails when a NEW bare
call lands in one of them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# parents[0]=adapters, [1]=pms, [2]=tests, [3]=ma_poc
_MA_POC = Path(__file__).resolve().parents[3]

#: Blocking helpers that must never be called bare from a coroutine.
_BLOCKING = {"probe_get", "web_unlocker_get"}

#: ``(relative path, coroutine name)`` — every async function whose blocking
#: probes have been deliberately off-loaded. Each entry is a fix that a
#: mutation proved was otherwise unbound.
_CLEAN_COROUTINES: list[tuple[str, str]] = [
    # The RentCafe SecureCafe drill: up to 3 candidates x 120s = up to 360s of
    # loop starvation per property, on a path that fires on ~1,344 properties.
    ("pms/adapters/rentcafe.py", "_try_rentcafe_securecafe_probe"),
    ("pms/adapters/onesite.py", "_probe_onesite_workflowstartup"),
    # Modern Jonah per-plan pages: direct public HTML is cheaper than a
    # browser click, but its curl_cffi transport must stay off the event loop.
    ("pms/adapters/encoreskyline_template.py", "_probe_public_html"),
    # Entrata's SSR cascade fans out across conventional indexes and plan
    # detail pages; a bare curl call here starves every concurrent property.
    ("pms/adapters/entrata.py", "_entrata_static_fetch"),
    # 4 sequential probes plus one per drill page, on the 51%-SecureCafe
    # plan-level cohort the 2026-07-25 RCA was about.
    ("pms/adapters/rentcafe_layout_tab.py", "_extract_code_only"),
]


def _bare_blocking_calls(path: Path, func_name: str) -> list[tuple[int, str]]:
    """``(lineno, callee)`` for blocking helpers called bare in *func_name*.

    A call is "bare" when it is a direct ``Name`` call — ``probe_get(...)``.
    The off-loaded form passes the helper as a REFERENCE to
    ``asyncio.to_thread(probe_get, ...)``, which is an ``ast.Name`` in argument
    position and never an ``ast.Call.func``, so it is not matched here.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != func_name:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id in _BLOCKING
            ):
                out.append((inner.lineno, inner.func.id))
    return out


@pytest.mark.parametrize(("relpath", "func_name"), _CLEAN_COROUTINES)
def test_no_blocking_probe_in_async_def(relpath: str, func_name: str) -> None:
    """These coroutines must reach the network only through a worker thread."""
    path = _MA_POC / relpath
    assert path.is_file(), f"{relpath} moved — update _CLEAN_COROUTINES"

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
    }
    assert func_name in names, (
        f"{relpath} has no `async def {func_name}` — it was renamed or made "
        f"sync. Update _CLEAN_COROUTINES rather than deleting this guard."
    )

    bare = _bare_blocking_calls(path, func_name)
    assert not bare, (
        f"{relpath}::{func_name} calls a BLOCKING probe directly on the event "
        f"loop at {bare}. urllib's urlopen parks the whole loop — this is the "
        f"2026-07-25 timeout RCA's failure mode. Wrap it: "
        f"`await asyncio.to_thread(<fn>, <url>, timeout=<n>)`."
    )


def test_the_guard_can_actually_fail() -> None:
    """Self-check: prove the detector fires on the shape it is meant to catch.

    Without this, a detector bug (e.g. matching ``ast.Attribute`` only) would
    make every test above pass vacuously — which is exactly how the two
    surviving mutations went unnoticed in the first place.
    """
    import tempfile

    src = (
        "import asyncio\n"
        "async def f():\n"
        "    a = probe_get('http://x', timeout=20)\n"
        "    b = await asyncio.to_thread(probe_get, 'http://y', timeout=20)\n"
        "    return a, b\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        tmp = Path(fh.name)
    try:
        found = _bare_blocking_calls(tmp, "f")
        # The bare call is caught; the to_thread reference is NOT a false hit.
        assert found == [(3, "probe_get")], found
    finally:
        tmp.unlink()


def test_hb_raw_get_remains_async() -> None:
    """Prevent the async HB seam from silently becoming a blocking helper."""
    import inspect

    from ma_poc.fetch.hyperbrowser_backend import hb_raw_get

    assert inspect.iscoroutinefunction(hb_raw_get)
