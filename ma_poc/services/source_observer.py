"""
Records SourceObservation deltas to the profile after every merge.

Phase 11 — closes the self-learning loop for sources. The planner reads
SourceObservations on the next run and floats observed-winning sources
to the top of the ranking.

Pure logic, side-effect-free until the final list-mutation step on the
profile. Best-effort — never raises.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from ma_poc.models.scrape_profile import SourceObservation
from ma_poc.models.source import FieldValue
from ma_poc.services.source_planner import FIELD_GROUP

log = logging.getLogger(__name__)


def record_source_observations(profile: Any, merged_units: list) -> None:
    """For each field in each merged unit, identify which source won and
    bump its SourceObservation. Best-effort — never raises."""
    try:
        # Aggregate wins: (source_id, field_group) -> list of confidences
        wins: dict[tuple[str, str], list[float]] = defaultdict(list)
        for unit in merged_units:
            for field_name, fv in unit.items():
                if not isinstance(fv, FieldValue):
                    continue
                group = FIELD_GROUP.get(field_name)
                if group is None:
                    continue
                key = (fv.source.value, group)
                wins[key].append(fv.confidence)

        existing = {
            (o.source_id, o.field_group): o
            for o in profile.api_hints.source_observations
        }
        now = datetime.utcnow()
        for (source_id, group), confidences in wins.items():
            obs = existing.get((source_id, group))
            if obs is None:
                obs = SourceObservation(source_id=source_id, field_group=group)
                profile.api_hints.source_observations.append(obs)
                existing[(source_id, group)] = obs
            n_prior = obs.contribution_count
            obs.contribution_count += len(confidences)
            obs.last_contributed_at = now
            new_avg = (
                (obs.avg_confidence_when_won * n_prior + sum(confidences))
                / max(obs.contribution_count, 1)
            )
            obs.avg_confidence_when_won = new_avg
            obs.consecutive_failures = 0

        # Increment failures on observations whose source ran but contributed nothing
        sources_run = list(getattr(profile.confidence, "last_sources_run", None) or [])
        for source_id in sources_run:
            for group in ("identity", "physical", "transactional"):
                if (source_id, group) in wins:
                    continue
                obs = existing.get((source_id, group))
                if obs is not None:
                    obs.consecutive_failures += 1

        # Cap (defensive — schema validator caps too)
        if len(profile.api_hints.source_observations) > 20:
            profile.api_hints.source_observations = profile.api_hints.source_observations[-20:]
    except Exception as exc:
        log.warning("record_source_observations failed: %s", exc)
