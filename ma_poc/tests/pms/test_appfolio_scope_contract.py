"""2026-07-28 — the ONE AppFolio surface predicate and the ONE strictness
contract, and the guards that stop them being re-forked.

Three branches independently answered "what counts as a scopeable AppFolio
surface" and "how strictly do we admit an account roster":

* ``_appfolio_embed._is_listings_index_url`` + a ``strict_scope`` boolean whose
  ``True`` TIGHTENED the address filter (default ``False``);
* ``appfolio._is_appfolio_account_index_url`` + a ``strict_scope`` boolean whose
  ``True`` was the BASELINE and whose ``False`` LOOSENED it (default ``True``);
* ``resolver.normalize_appfolio_url``'s own third copy of the shape rule.

Same parameter name, opposite default, different meaning for the same value.
Reproduced before the fix on a naive merge of the first two branches
(``ba68279`` + A + B, scratch worktree, commit ``6c8cfa7``): the AppFolio-embed
recovery's PUBLISHED-INDEX caller, which passes the non-strict value, silently
acquired branch B's "never return empty" leniency and shipped a foreign account
roster where branch A alone emitted nothing::

    E  AssertionError: contamination reopened:
       ['10309-92ND SW, 07, TACOMA, WA 98498', '9805 PACIFIC AVE, 12, SUMNER, WA 98390']
    FAILED test_repro_flip.py::test_published_index_roster_that_is_not_ours_still_demotes

No test on either branch caught it. This file is the permanent guard:

  1. ONE predicate, table-tested, imported everywhere (no re-forking);
  2. ONE contract with THREE grades — a boolean cannot carry three states,
     which is the whole bug — and every grade distinguishable on identical
     input;
  3. the retired ``strict_scope`` name must raise, never be silently accepted.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from ma_poc.pms import appfolio_urls, detector, resolver
from ma_poc.pms.adapters import _appfolio_embed, appfolio
from ma_poc.pms.adapters.appfolio import (
    ScopeEvidence,
    filter_listings_by_property_address,
)
from ma_poc.pms.appfolio_urls import (
    is_account_index_url,
    is_appfolio_tenant_url,
    is_listings_index_url,
    is_scoped_listings_url,
    scoped_listings_url,
)

# ─────────────────────────────────────────────────────────────────────
# 1. ONE predicate — the table.
#
# 28 cases: 12 that MUST be admitted as an operator-published listings
# index, 16 that MUST NOT. The must-NOT half carries the host-spoofing
# cases branch B covered and the per-unit deep links branch A covered,
# because those two sets were the ones the three copies disagreed on.
# ─────────────────────────────────────────────────────────────────────

# url, is_index, is_tenant, is_account_index, why
_SURFACE_TABLE: list[tuple[str, bool, bool, bool, str]] = [
    # ── MUST be admitted: operator-published listings index ──
    ("https://becovic.appfolio.com/listings", True, True, True, "listings index"),
    ("https://becovic.appfolio.com/listings/", True, True, True, "index, trailing slash"),
    ("http://cmnd.appfolio.com/listings", True, True, True, "http scheme"),
    ("https://BECOVIC.AppFolio.COM/LISTINGS", True, True, True, "mixed case"),
    ("https://olympicmanagement.appfolio.com/listings#top", True, True, True, "index + fragment"),
    ("https://illumepm.appfolio.com/listings?portfolio_uuid=abc", True, True, True,
     "portfolio_uuid is NOT a property scope (live-tested 2026-07-28: byte-identical)"),
    ("https://chamberlin.appfolio.com/listings?1785172489213", True, True, True,
     "cache-buster only"),
    ("https://www.chamberlin.appfolio.com/listings", True, True, True,
     "www-prefixed TENANT is still a tenant"),
    ("https://wwwfoo.appfolio.com/listings", True, True, True,
     "a slug that starts with 'www' is a real tenant"),
    # Scoped index: still an index, no longer ACCOUNT-wide.
    ("https://illumepm.appfolio.com/listings?filters%5Bproperty_list%5D=brookside",
     True, True, False, "scoped index, percent-encoded brackets"),
    ("https://illumepm.appfolio.com/listings?filters[property_list]=brookside",
     True, True, False, "scoped index, literal brackets"),
    ("https://h.appfolio.com/listings?1785&filters%5Bproperty_list%5D=EAST%20HAMPTON&x=1",
     True, True, False, "scope among other params"),
    # ── MUST NOT be admitted: per-unit deep links (branch A's cohort) ──
    ("https://yourmetropolitan.appfolio.com/listings/detail/46495a3b", False, True, False,
     "per-unit detail page"),
    ("https://bltliveworkplay.appfolio.com/listings/showings/new?listable_uid=x",
     False, True, False, "per-unit tour form"),
    ("https://pillarrei.appfolio.com/listings/rental_applications/new?listable_uid=x",
     False, True, False, "per-unit application form (Middlebury Crossing shape)"),
    ("https://becovic.appfolio.com/listings/233", False, True, False, "per-unit numeric"),
    # ── MUST NOT: tenant-only / portal / auth paths ──
    ("https://chamberlin.appfolio.com/connect/users/sign_in", False, True, False,
     "resident portal"),
    ("https://ironridgecapital.appfolio.com/connect/users/sign_in?a=cw&utm_source=apmsites_v3",
     False, True, False, "pay-rent button — the 127-property fingerprint"),
    ("https://richelsonmanagement.appfolio.com/", False, True, False, "bare tenant root"),
    ("https://becovic.appfolio.com", False, True, False, "bare root, no slash"),
    ("https://vdbprop.appfolio.com/oportal/dashboard", False, True, False, "owner portal"),
    ("https://account.appfolio.com/realms/foliospace/protocol/openid-connect/auth?client_id=x",
     False, True, False, "AppFolio SSO endpoint"),
    # ── MUST NOT: prefix collisions ──
    ("https://becovic.appfolio.com/listingsmore", False, True, False, "prefix-only path"),
    ("https://becovic.appfolio.com/listings-archive", False, True, False, "hyphen path"),
    # ── MUST NOT: host spoofing / reserved hosts (branch B's cohort) ──
    ("https://chamberlin.appfolio.com.evil.example/listings", False, False, False,
     "suffix-spoof host"),
    ("https://evil-appfolio.com/listings", False, False, False, "hyphen-spoof host"),
    ("https://www.appfolio.com/terms/listings", False, False, False,
     "AppFolio's own marketing site"),
    ("https://www.appfolio.com/listings", False, False, False, "marketing host, index path"),
    ("https://appfolio.com/listings", False, False, False, "bare apex is not a tenant"),
    ("https://secure.appfolio.com/listings", False, True, False,
     "reserved infra sub-domain, never an operator roster"),
    # ── MUST NOT: not a URL at all ──
    ("ftp://chamberlin.appfolio.com/listings", False, False, False, "non-http scheme"),
    ("https://example.com/listings", False, False, False, "unrelated site"),
    ("", False, False, False, "empty"),
    ("not a url", False, False, False, "garbage"),
]


@pytest.mark.parametrize("url,is_index,is_tenant,is_acct,why", _SURFACE_TABLE)
def test_unified_surface_predicate_table(
    url: str, is_index: bool, is_tenant: bool, is_acct: bool, why: str
) -> None:
    assert is_listings_index_url(url) is is_index, f"is_listings_index_url: {why}"
    assert is_appfolio_tenant_url(url) is is_tenant, f"is_appfolio_tenant_url: {why}"
    assert is_account_index_url(url) is is_acct, f"is_account_index_url: {why}"


def test_table_covers_enough_of_both_answers() -> None:
    """A table that only says yes proves nothing. Guard the shape of the
    table itself so a later edit cannot quietly drop the rejection half."""
    admitted = [row for row in _SURFACE_TABLE if row[1]]
    rejected = [row for row in _SURFACE_TABLE if not row[1]]
    assert len(_SURFACE_TABLE) >= 20
    assert len(rejected) >= 10, "the must-NOT-match half is the load-bearing half"
    assert len(admitted) >= 5


def test_scoped_listings_url_round_trips_through_the_predicate() -> None:
    """The URL we BUILD must be one the predicate admits as scoped —
    otherwise the scope we just applied would be re-graded as account-wide."""
    built = scoped_listings_url(
        "https://olympicmanagement.appfolio.com/listings", "COLLEGE PARK"
    )
    assert built == (
        "https://olympicmanagement.appfolio.com/listings"
        "?filters%5Bproperty_list%5D=COLLEGE%20PARK"
    )
    assert is_listings_index_url(built) is True
    assert is_scoped_listings_url(built) is True
    assert is_account_index_url(built) is False


# ─────────────────────────────────────────────────────────────────────
# 2. ONE predicate — no second copy anywhere.
#
# Three copies of a rule is how these drifted apart, and the same disease
# already exists elsewhere in this repo (roster_identity.py in two
# divergent copies, 660 vs 1049 lines). Structural, so it fails on the
# re-fork rather than on the behaviour change six weeks later.
# ─────────────────────────────────────────────────────────────────────

# Module objects, not path literals — the source location comes from the
# import system, so this stays correct under any CWD (see
# tests/pms/test_fixture_paths_cwd_independent.py).
_CONSUMERS = (_appfolio_embed, appfolio, resolver, detector)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "module", _CONSUMERS, ids=[m.__name__.rsplit(".", 1)[-1] for m in _CONSUMERS]
)
def test_no_consumer_restates_the_listings_index_rule(module: object) -> None:
    """``/listings`` as a REGEX ANCHOR — ``\\.appfolio\\.com/listings`` inside a
    compiled pattern used to decide "is this an inventory surface" — is the
    shape that got copied three times. Consumers may canonicalise a URL, but
    the grading rule lives in ``appfolio_urls`` only.
    """
    rel = getattr(module, "__name__", "?")
    src = Path(inspect.getfile(module)).read_text()  # type: ignore[arg-type]
    # Every module that grades an AppFolio URL must import the shared rule...
    if "appfolio.com" in src:
        assert "appfolio_urls" in src, f"{rel} grades AppFolio URLs without the shared module"
    # ...and must not define its own index predicate.
    for banned in (
        "def _is_listings_index_url",
        "def is_listings_index_url",
        "def _is_appfolio_account_index_url",
        "def _property_list_filter",
    ):
        assert banned not in src, f"{rel} re-forked the shared predicate: {banned}"


def test_reserved_slug_list_is_not_duplicated() -> None:
    """``_APPFOLIO_SKIP_SLUGS`` and the URL grader's reserved list are the same
    object — a second literal copy is how ``secure.appfolio.com`` ends up
    admitted by one caller and rejected by another."""
    assert appfolio._APPFOLIO_SKIP_SLUGS is appfolio_urls.APPFOLIO_RESERVED_SLUGS


# ─────────────────────────────────────────────────────────────────────
# 3. ONE contract, THREE grades.
# ─────────────────────────────────────────────────────────────────────


def _unit(address: str, uid: str) -> dict[str, str]:
    return {"floor_plan_name": address, "unit_number": uid, "rent_range": "$1,500"}


def test_all_three_grades_are_distinguishable_on_identical_input() -> None:
    """THE REGRESSION. One roster, one ctx, three answers — this is exactly
    what a boolean cannot express, and collapsing any two of these is the
    silent flip reproduced in this file's docstring.

    Rows: one the parser could not read an address out of, and one at a real
    address 40 miles from the property (the Heritage Amity Commons shape,
    pid 237787: CSV Douglassville PA, roster all Perkasie PA).
    """
    units = [
        _unit("AppFolio listing 4475", "unreadable"),
        _unit("1 Applewood Drive, Perkasie, PA 18944", "foreign"),
    ]
    kw = {"ctx_address": "606A Lake Dr", "ctx_zip": "19518"}

    lenient, tel_l = filter_listings_by_property_address(
        units, evidence=ScopeEvidence.OPERATOR_SCOPED, **kw  # type: ignore[arg-type]
    )
    baseline, tel_b = filter_listings_by_property_address(
        units, evidence=ScopeEvidence.PUBLISHED_INDEX, **kw  # type: ignore[arg-type]
    )
    weak, tel_w = filter_listings_by_property_address(
        units, evidence=ScopeEvidence.WEAK_EVIDENCE, **kw  # type: ignore[arg-type]
    )

    # OPERATOR_SCOPED: the operator published these on this property's own
    # surface. The unreadable row is a parse gap, not contamination.
    assert [u["unit_number"] for u in lenient] == ["unreadable"]
    assert tel_l["reason"] == "address_filter_applied"

    # PUBLISHED_INDEX: historical behaviour — nothing matched, so demote.
    assert baseline == []
    assert tel_b["reason"] == "filter_rejected_all_demote"

    # WEAK_EVIDENCE: same outcome here, reached with no pass-throughs at all.
    assert weak == []
    assert tel_w["reason"] == "filter_rejected_all_demote"

    # And the grades really are three, not two: the single-address
    # pass-through separates PUBLISHED_INDEX from WEAK_EVIDENCE.
    one_address = [
        _unit("1 Applewood Drive, Perkasie, PA 18944", "a"),
        _unit("1 Applewood Drive, Perkasie, PA 18944", "b"),
    ]
    kept_b, tel_b2 = filter_listings_by_property_address(
        one_address, evidence=ScopeEvidence.PUBLISHED_INDEX, **kw  # type: ignore[arg-type]
    )
    kept_w, tel_w2 = filter_listings_by_property_address(
        one_address, evidence=ScopeEvidence.WEAK_EVIDENCE, **kw  # type: ignore[arg-type]
    )
    assert len(kept_b) == 2 and tel_b2["reason"] == "single_address_in_response"
    assert kept_w == [] and tel_w2["reason"] == "filter_rejected_all_demote"


def test_default_grade_is_the_historical_behaviour() -> None:
    """The vanity / DUDA callers pass nothing. Their behaviour is the
    2026-07-18 contamination fix, byte for byte."""
    sig = inspect.signature(filter_listings_by_property_address)
    assert sig.parameters["evidence"].default is ScopeEvidence.PUBLISHED_INDEX


def test_the_retired_strict_scope_name_raises_instead_of_flipping() -> None:
    """``strict_scope`` meant two contradictory things across the branches
    this reconciles. A caller still using it must FAIL LOUDLY — a silently
    accepted boolean is precisely the defect."""
    assert "strict_scope" not in inspect.signature(
        filter_listings_by_property_address
    ).parameters
    with pytest.raises(TypeError):
        filter_listings_by_property_address(
            [_unit("1 A St, Nowhere, TX 75001", "a")],
            ctx_address="1 A St",
            ctx_zip="75001",
            strict_scope=True,  # type: ignore[call-arg]
        )


def test_no_caller_still_passes_strict_scope() -> None:
    """Grep guard: the name is retired repo-wide as a keyword ARGUMENT, not
    just removed from the signature. Prose that explains why it is gone is
    welcome; a live ``strict_scope=`` is not.
    """
    root = _repo_root() / "ma_poc"
    here = Path(__file__).resolve()
    pat = re.compile(r"strict_scope\s*=")
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p.resolve() != here and pat.search(p.read_text())
    ]
    assert offenders == [], f"stale strict_scope callers: {offenders}"


def test_every_grade_is_reachable_from_production_code() -> None:
    """A grade nothing passes is a grade nobody maintains. All three must be
    named by a real caller."""
    embed_src = inspect.getsource(_appfolio_embed)
    adapter_src = inspect.getsource(appfolio.AppFolioAdapter.extract)
    assert "ScopeEvidence.WEAK_EVIDENCE" in embed_src
    assert "ScopeEvidence.PUBLISHED_INDEX" in embed_src
    assert "ScopeEvidence.OPERATOR_SCOPED" in adapter_src
    assert "ScopeEvidence.PUBLISHED_INDEX" in adapter_src
