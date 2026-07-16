"""Entrata ``jd-fp-unit-card`` widget parser — parse_entrata_pp_jd_fp_cards.

Acceptance (2026-07-16, rendered /floorplans capture from
anthemeverett.com, structure verbatim):
- newer Entrata vanity-domain sites ship a static ``jd-fp-unit-card--preload``
  skeleton grid; JS populates real per-unit ``<a data-jd-fp-selector=
  "unit-card" title="#3100" data-unit="<hash>">`` rows post-render.
- the real unit_number is the ``title`` attribute (``#3100`` -> ``3100``),
  NOT a synthetic id.
- ``--preload`` skeletons must be skipped (they carry no real data).
- sqft is written dotted ("464 sq. ft.") and must still parse.
"""

from __future__ import annotations

from ma_poc.pms.adapters.entrata import parse_entrata_pp_jd_fp_cards

# Two real rendered rows (studio + 1bd, distinct floor plans) plus one
# --preload skeleton that must be dropped.
_JDFP_HTML = """
<div class="jd-fp-cards-container">
  <a data-jd-fp-selector="unit-card" title="#3100"
     href="/floorplans/unit-047ef1a34d7bfd8678fa521d5823b260/"
     data-unit="047ef1a34d7bfd8678fa521d5823b260"
     class="jd-fp-unit-card jd-fp-unit-card--row jd-fp-unit-card--style-default">
    <div class="jd-fp-unit-card__container">
      S1 View unit #3100 Available Now Studio 1 bath 464 sq. ft.
      Floorplan layout: S1 $2,400 /mo* 24 months $2,400 Base Rent
    </div>
  </a>
  <a data-jd-fp-selector="unit-card" title="#3092"
     href="/floorplans/unit-b2c59047b20f/"
     data-unit="b2c59047b20f"
     class="jd-fp-unit-card jd-fp-unit-card--row">
    <div class="jd-fp-unit-card__container">
      A1.H View unit #3092 Available 06/15/2026 1 bed 1 bath 570 sq. ft.
      Floorplan layout: A1.H $2,725 /mo
    </div>
  </a>
  <a data-jd-fp-selector="unit-card" title="#00000"
     class="jd-fp-unit-card jd-fp-unit-card--preload">
    <div class="jd-fp-unit-card__container">&nbsp;</div>
  </a>
</div>
"""

_URL = "https://anthemeverett.com/floorplans"


def test_jd_fp_skips_preload_skeleton():
    # 3 cards in the DOM, but one is a --preload skeleton -> 2 real units.
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    assert len(units) == 2


def test_jd_fp_extracts_real_unit_numbers():
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    nums = [u["unit_number"] for u in units]
    # Real marketing unit numbers from title="#NNNN", not synthetic ids.
    assert nums == ["3100", "3092"]
    # The skeleton's placeholder title (#00000) must never surface.
    assert "00000" not in nums


def test_jd_fp_floor_plan_names():
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    assert [u["floor_plan_name"] for u in units] == ["S1", "A1.H"]


def test_jd_fp_beds_studio_and_one():
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    # "Studio" -> 0 beds; "1 bed" -> 1.
    assert [u["bedrooms"] for u in units] == ["0", "1"]


def test_jd_fp_dotted_sqft_parses():
    # "464 sq. ft." (with periods) — the older PP sqft regex misses this.
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    assert [u["sqft"] for u in units] == ["464", "570"]


def test_jd_fp_rent_and_availability():
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    assert units[0]["market_rent_low"] == 2400
    assert units[1]["market_rent_low"] == 2725
    assert all(u["availability_status"] == "AVAILABLE" for u in units)
    # M/D/Y "06/15/2026" -> ISO.
    assert units[1]["availability_date"] == "2026-06-15"


def test_jd_fp_source_id_captured():
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    # data-unit hash preserved as an Entrata source id anchor.
    assert units[0]["source_ids"]["entrata_uid"] == "047ef1a34d7bfd8678fa521d5823b260"
    assert units[1]["source_ids"]["entrata_uid"] == "b2c59047b20f"


def test_jd_fp_extraction_tier_tagged():
    units = parse_entrata_pp_jd_fp_cards(_JDFP_HTML, _URL)
    assert all(u["extraction_tier"] == "TIER_1_DOM_ENTRATA_PP_JD_FP" for u in units)


def test_jd_fp_empty_html():
    assert parse_entrata_pp_jd_fp_cards("", _URL) == []


def test_jd_fp_all_preload_returns_empty():
    html = """
    <a data-jd-fp-selector="unit-card" title="#0"
       class="jd-fp-unit-card jd-fp-unit-card--preload"><div>&nbsp;</div></a>
    <a data-jd-fp-selector="unit-card" title="#0"
       class="jd-fp-unit-card jd-fp-unit-card--preload"><div>&nbsp;</div></a>
    """
    assert parse_entrata_pp_jd_fp_cards(html, _URL) == []
