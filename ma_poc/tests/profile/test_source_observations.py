"""Phase 11 — source observations + planner preference floating tests."""

from __future__ import annotations

from models.scrape_profile import ScrapeProfile, SourceObservation
from models.source import FieldValue, ProvenancedUnit, SourceId
from services.source_observer import record_source_observations
from services.source_planner import rank_sources_for_field_group


def _make_unit(rent_source: SourceId, rent_conf: float = 0.85) -> ProvenancedUnit:
    pu = ProvenancedUnit()
    pu["unit_id"] = FieldValue(value="U1", source=SourceId.API_SIGHTMAP, confidence=0.95)
    pu["asking_rent"] = FieldValue(value=1500, source=rent_source, confidence=rent_conf)
    return pu


def test_observation_recorded_for_winning_source() -> None:
    p = ScrapeProfile(canonical_id="phase11-001")
    record_source_observations(p, [_make_unit(SourceId.FIELD_PATCH)])
    obs = {(o.source_id, o.field_group): o for o in p.api_hints.source_observations}
    assert (SourceId.FIELD_PATCH.value, "transactional") in obs
    assert obs[(SourceId.FIELD_PATCH.value, "transactional")].contribution_count == 1


def test_observation_avg_confidence_running() -> None:
    p = ScrapeProfile(canonical_id="phase11-002")
    record_source_observations(p, [_make_unit(SourceId.FIELD_PATCH, 0.8)])
    record_source_observations(p, [_make_unit(SourceId.FIELD_PATCH, 0.6)])
    obs = next(
        o for o in p.api_hints.source_observations
        if o.source_id == SourceId.FIELD_PATCH.value and o.field_group == "transactional"
    )
    assert obs.contribution_count == 2
    assert abs(obs.avg_confidence_when_won - 0.7) < 1e-6


def test_consecutive_failures_increments_when_no_win() -> None:
    p = ScrapeProfile(canonical_id="phase11-003")
    # First, register FIELD_PATCH as a known source
    record_source_observations(p, [_make_unit(SourceId.FIELD_PATCH)])
    # Now mark it as having "run" but the new merged unit doesn't have a field_patch winner
    p.confidence.last_sources_run = [SourceId.FIELD_PATCH.value]
    record_source_observations(p, [_make_unit(SourceId.API_SIGHTMAP)])
    fp_obs = next(
        o for o in p.api_hints.source_observations
        if o.source_id == SourceId.FIELD_PATCH.value
    )
    assert fp_obs.consecutive_failures == 1


def test_planner_floats_observed_winners() -> None:
    """Profile says FIELD_PATCH won 5x in transactional → planner floats it
    above its default position."""
    prefs = [
        SourceObservation(
            source_id=SourceId.FIELD_PATCH.value,
            field_group="transactional",
            contribution_count=5,
        )
    ]
    ranking = rank_sources_for_field_group("transactional", "unknown", prefs)
    fp_position = next(i for i, (s, _) in enumerate(ranking) if s == SourceId.FIELD_PATCH)
    # FIELD_PATCH default position in transactional is well below 0
    assert fp_position == 0


def test_observations_capped_at_20() -> None:
    p = ScrapeProfile(canonical_id="phase11-004")
    # Manually insert 25 observations, then save+load the schema validator caps
    p.api_hints.source_observations = [
        SourceObservation(source_id=f"s_{i}", field_group="physical", contribution_count=1)
        for i in range(25)
    ]
    # Re-validate via model_dump→model_validate
    data = p.model_dump()
    p2 = ScrapeProfile.model_validate(data)
    assert len(p2.api_hints.source_observations) == 20


def test_no_op_on_empty_units() -> None:
    p = ScrapeProfile(canonical_id="phase11-005")
    record_source_observations(p, [])
    assert p.api_hints.source_observations == []
