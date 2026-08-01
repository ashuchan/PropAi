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


def test_find_knock_ids_base64_public_key() -> None:
    """Public key in BASE64 form, not lowercase hex.

    2026-07-12 prod evidence (impressionsapts.com + ~57 detected-as-knock
    props that fell through to LLM): Knock now emits base64 public keys,
    e.g. ``init(\\"VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP\\",
    \\"community\\",\\"3838514011eb718b\\")``. The old regex bounded the
    key to ``[a-f0-9]{20,40}`` (hex only), silently failing on the base64
    key and never reaching the well-formed community_id — so the Tier-1
    Doorway API call never fired and the property fell to LLM. The key
    class must accept the base64/base64url alphabet. Live-verified: the
    recovered community_id resolves to numeric id 2010536 → 13 units."""
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        r'knockDoorway.init(\"VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP\",'
        r'\"community\",\"3838514011eb718b\")'
        "</script></body></html>"
    )
    pk, kind, cid = find_knock_ids(html)
    assert pk == "VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP"
    assert kind == "community"
    assert cid == "3838514011eb718b"


def test_find_knock_ids_jonahwidget_knock_wrapper() -> None:
    """Jonah Digital wrapper form ``JonahWidget.knock({init:[...]})``.

    2026-07-12 prod evidence (6 encoreskyline-detected props, e.g. pids
    60038/1783/284136): Jonah sites init the SAME Knock backend via
    ``JonahWidget.knock({init:['<public_key>','community','<community_id>']})``
    — identical (key, kind, community_id) arg shape, different call wrapper.
    5/6 resolved to real Doorway Tier-1 units (10/31/10/3/65). The regex
    prefix must accept both call forms."""
    html = (
        "<html><body><script>"
        "JonahWidget.knock({init:["
        "'VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP',"
        "'community','3838514011eb718b']})"
        "</script></body></html>"
    )
    pk, kind, cid = find_knock_ids(html)
    assert pk == "VzlINUlZMlNVRDBNN1JFTjpUWEgxOURPTjhRU1hLTlRP"
    assert kind == "community"
    assert cid == "3838514011eb718b"


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


def test_find_knock_ids_dynamic_dni_config() -> None:
    """Harbor Group binds the same Knock ids through config variables."""
    html = """
    <script>
      const config = {
        dniLibrary: "https://doorway.knck.io/latest/doorway.min.js",
        dniId: "91011ebb76019d4d",
        dniApiKey: 'ad96e5d25f696e657111eb979d127cae'
      };
      window.knockDoorway.init(
        config.dniApiKey, 'community', config.dniId
      );
    </script>
    """

    assert find_knock_ids(html) == (
        "ad96e5d25f696e657111eb979d127cae",
        "community",
        "91011ebb76019d4d",
    )


def test_find_knock_ids_rejects_unrelated_dni_fields_without_doorway() -> None:
    html = """
    <script>
      const config = {
        dniId: "91011ebb76019d4d",
        dniApiKey: "ad96e5d25f696e657111eb979d127cae"
      };
    </script>
    """

    assert find_knock_ids(html) == (None, None, None)
