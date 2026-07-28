"""Drift guards for the source-id provenance registry.

The registry (``ma_poc/core/source_ids.py``) replaced two hand-maintained
whitelists that had silently diverged from each other AND from what adapters
actually write. These tests exist so that cannot recur:

  * every ``source_ids`` key any adapter writes is CLASSIFIED (an unclassified
    key fails CI, which is the whole point — a new adapter must declare scope);
  * the derived views obey their invariants;
  * a ``DEAD`` entry cannot quietly come alive at the wrong scope;
  * the two behaviour cases the design turns on — the accepted Camden false
    positive and the provenance override — are pinned.

The writer scan is AST-based, not grep-based. Plain grep misses every
``{k: v for k, v in {...}.items() if v}`` comprehension (``sightmap.py:285``,
``spherexx.py:308``, ``appfolio.py:814``, ``_appfolio_websites_duda.py:263``)
and every ``source_ids=<IfExp>`` kwarg (``appfolio.py:742``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from ma_poc.core.identity import (
    _is_floorplan_surrogate,
    assign_fallback_unit_id,
    unit_has_real_anchor,
)
from ma_poc.core.source_ids import (
    PER_UNIT_EVIDENCE_KEYS,
    PER_UNIT_IDENTITY_KEYS,
    PLAN_LEVEL_KEYS,
    SOURCE_ID_SCOPES,
    SourceIdScope,
    normalize_source_id_key,
    scope_of,
)

# parents[0]=core, [1]=tests, [2]=ma_poc
_MA_POC = Path(__file__).resolve().parents[2]


# ── AST writer scan ─────────────────────────────────────────────────────────


def _literal_dict_keys(node: ast.AST) -> list[tuple[str, int]]:
    """Literal str keys of a dict-valued expression.

    Recurses through the shapes adapters actually use: plain ``ast.Dict``,
    ``IfExp`` (``{...} if x else {}``), ``BoolOp`` (``a or {...}``), and
    ``DictComp`` whose iterable is ``{...}.items()``.
    """
    out: list[tuple[str, int]] = []
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.append((k.value, k.lineno))
    elif isinstance(node, ast.IfExp):
        out += _literal_dict_keys(node.body)
        out += _literal_dict_keys(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            out += _literal_dict_keys(v)
    elif isinstance(node, ast.DictComp):
        for gen in node.generators:
            it = gen.iter
            if (
                isinstance(it, ast.Call)
                and isinstance(it.func, ast.Attribute)
                and it.func.attr == "items"
            ):
                out += _literal_dict_keys(it.func.value)
            else:
                out += _literal_dict_keys(it)
    elif isinstance(node, ast.Call):
        for kw in node.keywords:
            if kw.arg:
                out.append((kw.arg, node.lineno))
    return out


def _scan_file(path: Path) -> list[tuple[str, int]]:
    """Every ``source_ids`` key literal written in *path*."""
    found: list[tuple[str, int]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for t in targets:
                # source_ids["k"] = v  /  row["source_ids"]["k"] = v
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant):
                    base = t.value
                    name: str | None = None
                    if isinstance(base, ast.Name):
                        name = base.id
                    elif isinstance(base, ast.Attribute):
                        name = base.attr
                    elif isinstance(base, ast.Subscript) and isinstance(
                        base.slice, ast.Constant
                    ):
                        name = (
                            "source_ids"
                            if base.slice.value == "source_ids"
                            else None
                        )
                    if name == "source_ids" and isinstance(t.slice.value, str):
                        found.append((t.slice.value, t.lineno))
                # source_ids = {...}  /  self.source_ids = {...}
                #   /  row["source_ids"] = {...}
                is_sid_target = (
                    (isinstance(t, ast.Name) and t.id == "source_ids")
                    or (isinstance(t, ast.Attribute) and t.attr == "source_ids")
                    or (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "source_ids"
                    )
                )
                if is_sid_target and value is not None:
                    found += _literal_dict_keys(value)
        # f(..., source_ids={...})
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "source_ids":
                    found += _literal_dict_keys(kw.value)
        # {"source_ids": {...}}
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=False):
                if isinstance(k, ast.Constant) and k.value == "source_ids":
                    found += _literal_dict_keys(v)
    return found


def _all_writers() -> dict[str, list[str]]:
    """key -> ["<relpath>:<lineno>", …] over every non-test module."""
    writers: dict[str, list[str]] = {}
    for path in sorted(_MA_POC.rglob("*.py")):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        for key, lineno in _scan_file(path):
            rel = path.relative_to(_MA_POC.parent)
            writers.setdefault(normalize_source_id_key(key), []).append(
                f"{rel}:{lineno}"
            )
    return writers


_WRITERS = _all_writers()


def test_every_written_source_id_key_is_registered() -> None:
    """An unclassified key fails CI — declare its scope in core/source_ids.py.

    This is the guard that turns the registry from documentation into an
    enforced namespace. A new adapter landing a new ``source_ids`` key must say
    what that key proves; it may not default into either derived view by the
    shape of its name.
    """
    unregistered = {k: v for k, v in _WRITERS.items() if scope_of(k) is None}
    assert not unregistered, (
        "source_ids keys written by an adapter but NOT registered in "
        "ma_poc/core/source_ids.py:\n"
        + "\n".join(f"  {k}  <- {', '.join(v)}" for k, v in sorted(unregistered.items()))
        + "\nAdd each to SOURCE_ID_SCOPES with its scope + the evidence for it."
    )


def test_writer_scan_finds_the_comprehension_and_ifexp_shapes() -> None:
    """Self-check: the scanner must not silently regress to grep-equivalence.

    Each of these is written in a shape a plain text search misses. If the
    scanner is ever simplified, this fails before the coverage test starts
    passing vacuously.
    """
    for key in (
        "sightmap_unit_id",  # sightmap.py DictComp
        "spherexx_unit_id",  # spherexx.py DictComp
        "appfolio_unit_id",  # appfolio.py DictComp
        "appfolio_listable_uid",  # _appfolio_websites_duda.py DictComp
        "appfolio_listing_id",  # appfolio.py source_ids=<IfExp> kwarg
        "rentcafe_floorplan_id",  # rentcafe.py
        "udr_unitid",  # _udr.py
    ):
        assert key in _WRITERS, f"AST scan lost writer for {key}"


def test_registry_has_no_stale_entries_beyond_dead_and_apartment_id() -> None:
    """Every registered key either has a writer or is explicitly accounted for.

    ``DEAD`` entries are writer-less by definition. Bare ``apartment_id`` is
    writer-less on purpose (renamed to ``securecafe_apartment_id``) and is kept
    registered so it can never be admitted later.

    ``api_floorplan_id`` is writer-less IN THIS COMMIT: its only writer,
    ``scripts/diagnostics/browser_endpoint_discovery.py:373``, is an untracked
    file owned by a concurrent workstream. It is allowlisted explicitly rather
    than left to pass by accident — without this entry the test's green would
    be contingent on a foreign session's uncommitted file sitting in the
    working tree, and committing this PR alone would fail CI. Delete the
    allowlist entry in the diagnostics PR that lands the writer.
    """
    allowed_writerless = {
        k for k, s in SOURCE_ID_SCOPES.items() if s is SourceIdScope.DEAD
    } | {"apartment_id", "api_floorplan_id"}
    stale = {
        k for k in SOURCE_ID_SCOPES if k not in _WRITERS and k not in allowed_writerless
    }
    assert not stale, (
        f"Registered but written by nobody: {sorted(stale)}. Either the writer "
        f"was deleted (move the entry to DEAD) or the key was mistyped."
    )


def test_dead_keys_have_zero_writers() -> None:
    """A DEAD entry must stay dead.

    Guards the exact failure that produced this PR: ``entrata_unit_id`` sat in
    identity's per-unit whitelist for months with no writer, while the REAL
    Entrata key ``entrata_uid`` (2,843 rows / 190 props on 2026-07-12) was
    absent from it. If some adapter starts writing a DEAD name, it must be
    reclassified deliberately rather than inheriting a stale scope.
    """
    for key, scope in SOURCE_ID_SCOPES.items():
        if scope is SourceIdScope.DEAD:
            assert key not in _WRITERS, (
                f"{key!r} is registered DEAD but is now written at "
                f"{_WRITERS[key]}. Reclassify it with measured evidence."
            )


# ── derived-view invariants ─────────────────────────────────────────────────


def test_identity_view_is_subset_of_evidence_view() -> None:
    """PER_UNIT_IDENTITY_KEYS <= PER_UNIT_EVIDENCE_KEYS.

    Violated in BOTH directions by the two hand-maintained lists this registry
    replaced (identity had ``entrata_unit_id``/``knock_unit_id`` that verdict
    lacked; verdict had ``camden_unit_id``/``sightmap_unit_id`` etc. that
    identity lacked).
    """
    assert set(PER_UNIT_IDENTITY_KEYS) <= PER_UNIT_EVIDENCE_KEYS


def test_identity_view_has_no_duplicates() -> None:
    """The tuple is an ordered preference list — a repeat is a typo."""
    assert len(PER_UNIT_IDENTITY_KEYS) == len(set(PER_UNIT_IDENTITY_KEYS))


def test_per_unit_views_are_disjoint_from_every_other_scope() -> None:
    """No key may be both per-unit evidence and something else."""
    other = {
        k
        for k, s in SOURCE_ID_SCOPES.items()
        if s
        not in (SourceIdScope.UNIT_STABLE, SourceIdScope.UNIT_VOLATILE)
    }
    assert not (PER_UNIT_EVIDENCE_KEYS & other)
    assert not (set(PER_UNIT_IDENTITY_KEYS) & other)
    assert not (PER_UNIT_EVIDENCE_KEYS & PLAN_LEVEL_KEYS)


def test_pending_tautological_and_dead_are_in_neither_view() -> None:
    """Unmeasured, tautological and dead keys grant no unit-level credit.

    ``UNIT_PENDING`` implements the "unmeasured ⇒ excluded" rule.
    ``UNIT_TAUTOLOGICAL`` (``edifice_unit_id``, ``thinkreside_unit``) would
    otherwise re-admit through the side door a row that
    ``_is_floorplan_surrogate`` deliberately demoted, since both are verbatim
    copies of ``unit_number``.
    """
    for key, scope in SOURCE_ID_SCOPES.items():
        if scope in (
            SourceIdScope.UNIT_PENDING,
            SourceIdScope.UNIT_TAUTOLOGICAL,
            SourceIdScope.DEAD,
        ):
            assert key not in PER_UNIT_EVIDENCE_KEYS
            assert key not in PER_UNIT_IDENTITY_KEYS


@pytest.mark.parametrize(
    "key",
    [
        "camden_unit_id",
        "realpage_unit_id",
        "sightmap_floor_plan_id",
        "securecafe_floorplan_id",
        "rentcafe_floorplan_id",
        "onsite_style_id",
    ],
)
def test_named_plan_keys_stay_plan_scoped(key: str) -> None:
    """The two ``*_unit_id`` keys that are PLAN-scoped are why rule 2 exists.

    ``camden_unit_id`` (``_camden.py:251``) is read off the plan object under
    the comment "Plan-level fingerprint shared across all units of this plan";
    measured 366 rows / 129 distinct on 2026-07-12 and 30% of (property, plan)
    pairs rotated value within six days. ``realpage_unit_id``
    (``camden.py:95``) reads the same field off a ``suggestedFloorPlans``
    entry. Admitting either to rescue the one demoted Camden row would collapse
    that property's unit_ids 302/14/201/114/202 onto a single anchor.

    ``sightmap_floor_plan_id`` is the load-bearing one: 5,415 rows on
    2026-07-12, every one ``UNAVAILABLE`` / ``rent_low=None`` / ``area=-1`` —
    sold-out plan-presence markers. Admitting it manufactures exactly the false
    gold PR #110 removed.
    """
    assert scope_of(key) is SourceIdScope.PLAN
    assert key in PLAN_LEVEL_KEYS
    assert key not in PER_UNIT_EVIDENCE_KEYS
    assert key not in PER_UNIT_IDENTITY_KEYS


def test_normalize_folds_case_and_hyphen() -> None:
    assert normalize_source_id_key("Sightmap-Unit-Id") == "sightmap_unit_id"
    assert scope_of("SIGHTMAP-UNIT-ID") is SourceIdScope.UNIT_STABLE
    assert scope_of("no_such_key_anywhere") is None


# ── behaviour: the two cases the design turns on ────────────────────────────


def test_camden_35256_row_stays_demoted() -> None:
    """ACCEPTED false positive — pinned so nobody "fixes" it the wrong way.

    Property 35256 carries ``{"unit_id": "14", "source_ids":
    {"camden_floor_plan_id": 14, "camden_unit_id": 394}}`` — a REAL apartment
    (plan E1, $1,739) whose unit number collides with its plan id by
    coincidence. It stays demoted because ``camden_unit_id`` is plan-scoped and
    so cannot serve as the provenance override. Blast radius: exactly 1 unique
    unit in 138,370 across the three run-artifact sets.

    If this test starts failing because someone added ``camden_unit_id`` to a
    per-unit view: don't. Read ``test_named_plan_keys_stay_plan_scoped``.

    NOT "the Camden false positive is fixed" — it is not, and this PR makes
    this row strictly worse: it is the one row in 228,708 that goes from
    carrying per-unit evidence to carrying none, because ``camden_unit_id`` was
    in verdict's old 12-key list. Property-level verdict is unchanged in all
    three artifact sets (35256's sibling rows carry their own anchors).
    """
    from ma_poc.reporting.verdict import _has_per_unit_source_id

    row = {
        "unit_id": "14",
        "source_ids": {"camden_floor_plan_id": 14, "camden_unit_id": 394},
    }
    assert _is_floorplan_surrogate(row, "14") is True
    # Still demoted, and now with no verdict-layer evidence either. Both halves
    # are asserted so the regression is a recorded fact, not a footnote.
    assert unit_has_real_anchor(row) is False
    assert _has_per_unit_source_id(row) is False
    # …so it falls through to the phenotype hash. Physical attrs are the real
    # row's (plan E1, $1,739) — without them the ladder returns None and the
    # assertion would pass for the wrong reason.
    shipped = assign_fallback_unit_id(
        {**row, "floor_plan_name": "E1", "beds": 1, "baths": 1, "sqft": 750},
        "35256",
    )
    assert str(shipped).startswith("inferred_")


def test_demotion_routes_the_row_to_its_per_unit_anchor() -> None:
    """The demotion is what DELIVERS the anchor — it must not be suppressed.

    An earlier draft opened ``_is_floorplan_surrogate`` with a "provenance
    override": if the row carries an admitted per-unit anchor, return False.
    That reads as obviously right and re-introduced the exact PR #110 defect,
    because ``assign_fallback_unit_id`` resolves ``unit_id or unit_number`` at
    step 1 and only reaches ``_source_id_anchor`` at step 2. Suppressing the
    verdict made step 1 win — WITH THE FLOOR-PLAN ID.

    The old test asserted only the boolean and passed while the minted id was
    the plan id, so it is the minted id that is asserted here. Exposure had it
    shipped: 35,687 rows / 1,389 properties across the three run-artifact sets
    carry both an admitted per-unit key and a plan key.
    """
    row = {
        "unit_number": "5391405",
        "source_ids": {
            "floorplan_id": "5391405",
            "realpage_cws_unit_id": "16399273",
        },
    }
    # The row IS a plan surrogate — the unit_number really is the plan id …
    assert _is_floorplan_surrogate(row, "5391405") is True
    # … and that is precisely why step 2 gets to mint the real backend id.
    assert assign_fallback_unit_id(dict(row), "P1") == "realpage_cws_unit_id-16399273"


def test_siblings_on_one_plan_get_distinct_ids() -> None:
    """The failure the boolean-only test could not see: three flats, one id.

    Under the suppressed-demotion draft, three apartments on plan 5391405
    carrying distinct ``realpage_cws_unit_id``s all minted ``"5391405"``.
    """
    minted = [
        assign_fallback_unit_id(
            {
                "unit_number": "5391405",
                "source_ids": {
                    "floorplan_id": "5391405",
                    "realpage_cws_unit_id": uid,
                },
            },
            "P1",
        )
        for uid in ("16399273", "16399274", "16399275")
    ]
    assert len(set(minted)) == 3, f"apartments collapsed onto one id: {minted}"
    assert "5391405" not in minted, "a floor-plan id was minted as a unit_id"


def test_source_id_anchor_is_used_in_jugnu() -> None:
    """Jugnu must prefer an admitted native ID over a phenotype hash.

    The production formatter populates ``source_ids`` before calling
    ``assign_fallback_unit_id``.  This prevents a row that already contains a
    stable per-unit CWS ID from shipping as a fabricated ``inferred_*`` ID.
    """
    from datetime import UTC, datetime

    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    # A row that passed through an earlier formatter with an inferred id must
    # still be upgraded when a later parser surfaces its native backend ID.
    out = _format_v2_unit(
        {
            "unit_id": "inferred_a1_700_1_1",
            "floor_plan_name": "A1",
            "beds": 1,
            "baths": 1,
            "sqft": 700,
            "source_ids": {"realpage_cws_unit_id": 16399273},
        },
        datetime(2026, 7, 26, tzinfo=UTC),
        "282594",
    )
    # The anchor is preserved and becomes the emitted natural identifier.
    assert out["source_ids"] == {"realpage_cws_unit_id": 16399273}
    assert out["unit_id"] == "realpage_cws_unit_id-16399273"


def test_jugnu_retains_plan_row_provenance_for_downstream_gates() -> None:
    """A recombined plan summary remains visibly plan-level after formatting."""
    from datetime import UTC, datetime

    from ma_poc.scripts.runners.jugnu import _format_v2_unit

    out = _format_v2_unit(
        {
            "floor_plan_name": "A1",
            "beds": 1,
            "baths": 1,
            "sqft": 700,
            "asking_rent": 1200,
            "data_quality_flag": "PLAN_LEVEL_NO_UNIT_ANCHOR",
            "data_gaps": ["unit_number"],
            "extraction_tier": "TIER_1_API_APTS247_PLAN_LEVEL",
        },
        datetime(2026, 7, 27, tzinfo=UTC),
        "P1",
    )

    assert out["is_floor_plan_level"] is True
    assert out["data_quality_flag"] == "PLAN_LEVEL_NO_UNIT_ANCHOR"
    assert out["data_gaps"] == ["unit_number"]
    assert out["extraction_tier"] == "TIER_1_API_APTS247_PLAN_LEVEL"


def test_plan_marker_prevents_junk_unit_token_from_counting_as_real_anchor() -> None:
    """Post-process plan provenance must win over a stale raw DOM token."""
    from ma_poc.core.identity import unit_has_real_anchor

    assert unit_has_real_anchor(
        {
            "unit_number": "Left",
            "data_quality_flag": "PLAN_LEVEL_NO_UNIT_ANCHOR",
            "extraction_tier": "TIER_3_DOM_PLAN_LEVEL",
        }
    ) is False


def test_unregistered_floorplan_suffix_key_is_still_caught() -> None:
    """The legacy suffix heuristic survives as the unregistered-key backstop.

    A ``*_floorplan_id`` key that lands in a hotfix branch before the registry
    is updated must still demote. The coverage test above should make this
    unreachable in practice; it must exist anyway.
    """
    assert scope_of("brandnew_floorplan_id") is None
    row = {"unit_number": "7", "source_ids": {"brandnew_floorplan_id": "7"}}
    assert _is_floorplan_surrogate(row, "7") is True


#: The measurement that justified admitting each UNIT_STABLE key. Every entry
#: is a replay over 2026-07-12 (4,982 props / 110,226 units), the 2026-07-18
#: canary (4,982 / 106,820), 2026-07-26-plancohort (1,127 / 11,662), or a
#: captured real-payload fixture. Ratio = distinct values / rows, per property;
#: "rot" = cross-run rotation on the 07-12 <-> canary join (the second property
#: UNIT_STABLE claims, and the one that was never measured before 2026-07-27).
_ADMISSION_EVIDENCE: dict[str, str] = {
    "sightmap_unit_id": "16,347 rows / 530 props @ 1.000 (2026-07-12); rot 3/9,676 = 0.03%",
    "apts247_unit_id": "316 rows / 33 props @ 1.000 (2026-07-12); rot 0/49",
    "spherexx_unit_id": "42 rows / 2 props @ 1.000 (canary 2026-07-18); rot unmeasured (0 joined)",
    "appfolio_listable_uid": "2,306 rows / 81 props @ 1.000 (2026-07-12); rot 0/19",
    "appfolio_id": "2,306 rows / 81 props @ 1.000 (2026-07-12); rot 0/19",
    "realpage_cws_unit_id": "12 rows / 12 distinct / 2 props (plancohort); rot unmeasured",
    "fortresstech_unit_id": "3 rows @ 1.000 (canary); rot 0/1, UUID identical across runs",
    "onsite_unit_id": "43 rows / 2 props @ 1.000 (plancohort); fixtures 11/11 and 4/4",
    "venterra_unit_code": "fixtures 19/19 and 20/20",
    "realpage_oll_unit_id": "fixture 4/4 (UnitId= off the per-unit application URL)",
    "securecafe_apartment_id": "fixtures 7/7 and 18/18",
}

#: UNIT_VOLATILE: unique within a property, but MEASURED to rotate across runs.
#: Evidence view only — these must never mint a daily-join unit_id.
_VOLATILE_EVIDENCE: dict[str, str] = {
    "appfolio_listing_id": "9,561 rows / 153 props @ 0.9997 (07-12) BUT rot 44/303 = 14.52%",
    "entrata_uid": "2,843 rows / 190 props @ 0.9996 (07-12) BUT rot 50/1,985 = 2.52%",
    "udr_unitid": "420 rows / 17 props @ 1.000 (07-12) BUT rot 7/281 = 2.49%",
}


@pytest.mark.parametrize(
    ("key", "evidence"), sorted(_ADMISSION_EVIDENCE.items())
)
def test_admitted_key_mints_a_real_anchor(key: str, evidence: str) -> None:
    """Every admitted per-unit key must actually produce a non-synthetic id.

    Parametrised over the full UNIT_STABLE set so adding a key without adding
    its evidence line here is impossible — the id-count assertion below fails.
    *evidence* is the measurement that justified admission; it is carried in
    the parametrize id so a failure names the claim it invalidates.
    """
    assert evidence
    row = {
        "floor_plan_name": "A1",
        "beds": 1,
        "baths": 1,
        "sqft": 700,
        "source_ids": {key: "9001"},
    }
    res = assign_fallback_unit_id(row, "P1")
    assert res == f"{key}-9001", evidence
    assert not res.startswith(("inferred_", "unkeyable_"))


def test_every_unit_stable_key_has_an_evidence_case() -> None:
    """No silent additions: UNIT_STABLE and the identity view must match, and
    the evidence-parametrised test above must cover every one of them."""
    stable = {
        k for k, s in SOURCE_ID_SCOPES.items() if s is SourceIdScope.UNIT_STABLE
    }
    assert stable == set(PER_UNIT_IDENTITY_KEYS)
    assert set(_ADMISSION_EVIDENCE) == stable, (
        "UNIT_STABLE keys without a measured-evidence entry in "
        f"_ADMISSION_EVIDENCE: {sorted(stable - set(_ADMISSION_EVIDENCE))}; "
        f"evidence entries for non-admitted keys: "
        f"{sorted(set(_ADMISSION_EVIDENCE) - stable)}"
    )


@pytest.mark.parametrize(("key", "evidence"), sorted(_VOLATILE_EVIDENCE.items()))
def test_volatile_key_never_mints_an_anchor(key: str, evidence: str) -> None:
    """A key that rotates across runs must NOT become the daily-join id.

    ``appfolio_listing_id`` was registered UNIT_STABLE — "stable across runs.
    MEASURED" — and sat FIRST in the anchor preference order while rotating on
    14.5% of joined rows, within the same order of magnitude as the 27.2% that
    disqualified ``camden_unit_id`` and sent it to PLAN. Same apartment, same
    plan, same rent, different id six days later: prop 19712 unit '114'
    7724 -> 8094; prop 305576 unit 'F-103' 8143 -> 7456.

    Minting on a rotating id makes the apartment read as "disappeared + new"
    at every daily join — the instability class recorded on 2026-07-15.
    """
    assert evidence
    row = {
        "floor_plan_name": "A1",
        "beds": 1,
        "baths": 1,
        "sqft": 700,
        "source_ids": {key: "9001"},
    }
    res = assign_fallback_unit_id(row, "P1")
    assert res != f"{key}-9001", evidence
    assert res.startswith("inferred_"), (
        f"{key} rotates across runs and must fall through to the phenotype "
        f"hash, not mint an anchor. {evidence}"
    )


def test_every_volatile_key_has_an_evidence_case() -> None:
    """UNIT_VOLATILE must be non-empty and fully evidenced.

    Non-empty matters on its own: while the scope was reserved-but-empty the
    two derived views had identical membership, so the split they exist to
    express was never exercised by any test.
    """
    volatile = {
        k for k, s in SOURCE_ID_SCOPES.items() if s is SourceIdScope.UNIT_VOLATILE
    }
    assert volatile, "UNIT_VOLATILE is empty — the two-view split is untested"
    assert set(_VOLATILE_EVIDENCE) == volatile, (
        f"missing evidence: {sorted(volatile - set(_VOLATILE_EVIDENCE))}; "
        f"stale evidence: {sorted(set(_VOLATILE_EVIDENCE) - volatile)}"
    )
    # Evidence view yes, minting view no — that IS the distinction.
    assert volatile <= PER_UNIT_EVIDENCE_KEYS
    assert not (volatile & set(PER_UNIT_IDENTITY_KEYS))


def test_anchor_prefix_is_the_full_key_name() -> None:
    """The two realpage namespaces must not collapse onto one prefix.

    ``k.split("_")[0]`` mapped both ``realpage_cws_unit_id`` and
    ``realpage_oll_unit_id`` to ``realpage-<id>``, which silently merges
    distinct apartments at upsert once both are admitted.
    """
    cws = assign_fallback_unit_id({"source_ids": {"realpage_cws_unit_id": "7"}}, "P1")
    oll = assign_fallback_unit_id({"source_ids": {"realpage_oll_unit_id": "7"}}, "P1")
    assert cws != oll
    assert cws == "realpage_cws_unit_id-7"
    assert oll == "realpage_oll_unit_id-7"
