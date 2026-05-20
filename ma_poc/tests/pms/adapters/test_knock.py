"""Knock / Doorway adapter — regex coverage for the static-HTML init call.

The init call shape ``knockDoorway.init('<key>','community','<id>')``
appears in two forms in real-world HTML:

  1. **Plain** — the call is rendered as-is in a ``<script>`` block.
  2. **JSON-escaped** — the call is bundled inside an SSR string (Next.js
     / Nuxt-style hydration payload), so quotes appear as ``\\\"``. The
     browser unescapes at runtime; raw HTTP fetches see the escaped form.

The regex in ``ma_poc.pms.adapters.knock`` must match both — otherwise
the conditional-cache / static-fetch paths can't extract IDs even when
the detector correctly routes to Knock.
"""

from __future__ import annotations

from ma_poc.pms.adapters.knock import find_knock_ids


def test_find_knock_ids_plain_init_call() -> None:
    """Standard ``<script>knockDoorway.init('...', 'community', '...')``."""
    html = (
        "<html><body>"
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        '<script>knockDoorway.init('
        '"a8e311e98aee0ee4545fea9e01b06ac6","community","69e936e6567a11ef");'
        "</script></body></html>"
    )
    pk, kind, cid = find_knock_ids(html)
    assert pk == "a8e311e98aee0ee4545fea9e01b06ac6"
    assert kind == "community"
    assert cid == "69e936e6567a11ef"


def test_find_knock_ids_json_escaped_init_call() -> None:
    """Init call inside an SSR string with ``\\"`` for each quote.

    2026-05-20 raw-fetch evidence from cluster-3 G5+Knock properties
    (flatirondistrictataustinranch / altaaptstarga / unionthompson):
    the init call lives inside a Next.js hydration payload as
    ``knockDoorway.init(\\"...\\",\\"community\\",\\"...\\")``. The
    regex must accept the optional backslash before each quote so
    conditional-cache / static fetches can still extract IDs."""
    # Note: in the source file the bytes are literal backslash + quote.
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        r'knockDoorway.init(\"a8e311e98aee0ee4545fea9e01b06ac6\",'
        r'\"community\",\"69e936e6567a11ef\")'
        "</script></body></html>"
    )
    pk, kind, cid = find_knock_ids(html)
    assert pk == "a8e311e98aee0ee4545fea9e01b06ac6"
    assert kind == "community"
    assert cid == "69e936e6567a11ef"


def test_find_knock_ids_missing_returns_none_triple() -> None:
    """No knockDoorway anywhere → ``(None, None, None)``."""
    html = "<html><body>nothing here</body></html>"
    assert find_knock_ids(html) == (None, None, None)


def test_find_knock_ids_no_init_call_only_script_tag() -> None:
    """Doorway script loaded but no init() call — still returns None."""
    html = (
        "<html><body>"
        '<script src="https://doorway.knck.io/latest/doorway.min.js"></script>'
        "</body></html>"
    )
    assert find_knock_ids(html) == (None, None, None)
