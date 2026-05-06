"""generic:profile_replay — deterministic replay of saved LlmFieldMapping."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


def _aggregate_quality(mappings: list[Any]) -> float:
    """Min quality_score across contributing mappings; defaults to 1.0."""
    if not mappings:
        return 1.0
    qs = [float(getattr(m, "quality_score", 1.0) or 1.0) for m in mappings]
    return min(qs) if qs else 1.0


def _apply_field_patches(
    units: list[dict[str, Any]],
    api_responses: list[dict[str, Any]],
    field_patches: list[Any],
    log_patch_hit: Any,
    log_patch_miss: Any,
) -> list[dict[str, Any]]:
    """Sub-tier 0b: apply saved FieldPatches to replayed units via positional join."""
    if not units or not api_responses or not field_patches:
        return units

    def _get_path(obj: Any, path: str) -> Any:
        for part in path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(part)
            elif isinstance(obj, list) and part.isdigit():
                idx = int(part)
                obj = obj[idx] if idx < len(obj) else None
            else:
                return None
            if obj is None:
                return None
        return obj

    for patch in field_patches:
        try:
            url_pat = getattr(patch, "api_url_pattern", None) or ""
            field_name = getattr(patch, "field_name", None) or ""
            json_path = getattr(patch, "json_path", None) or ""
        except Exception:
            continue
        if not (url_pat and field_name and json_path):
            continue
        matched_body = None
        for resp in api_responses:
            if url_pat in resp.get("url", ""):
                matched_body = resp.get("body")
                break
        if matched_body is None:
            log_patch_miss(patch, reason="no_url_match")
            continue
        values = _get_path(matched_body, json_path)
        if not isinstance(values, list):
            if values is not None:
                values = [values]
            else:
                log_patch_miss(patch, reason="path_returned_none")
                continue
        any_filled = False
        for i, unit in enumerate(units):
            if i >= len(values):
                break
            if unit.get(field_name) not in (None, "", -1, "-1"):
                continue
            v = values[i]
            if v not in (None, ""):
                unit[field_name] = v
                any_filled = True

        if any_filled:
            log_patch_hit(patch)
        else:
            log_patch_miss(patch, reason="no_unit_filled")
    return units


class ProfileReplayTier:
    """Deterministic replay of saved LlmFieldMapping / FieldPatch records.

    Runs before any parser: if a prior run's LLM told us exactly how to
    extract units from a specific API shape, we can reuse it with zero LLM
    cost. Falls through to the generic cascade on miss.
    """

    @staticmethod
    def run(
        profile: Any,
        api_responses: list[dict[str, Any]],
        result: Any,
        ctx: Any,
        log_attempt: Any,
        log_patch_hit: Any,
        log_patch_miss: Any,
    ) -> dict[str, Any]:
        """Run profile replay.

        Returns a dict with keys:
          - "replayed_units": list[dict]
          - "agg_quality": float
          - "short_circuit": bool  (True when caller should return early)
        """
        t0 = time.monotonic()
        replayed_units: list[dict[str, Any]] = []
        replayed_mappings: list[Any] = []

        try:
            saved = list(getattr(profile.api_hints, "llm_field_mappings", []) or [])
        except Exception:
            saved = []

        if not saved:
            log_attempt(
                "generic:profile_replay",
                "skipped",
                reason="no saved mappings",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            return {"replayed_units": [], "agg_quality": 1.0, "short_circuit": False}

        try:
            from ma_poc.services.llm_extractor import apply_saved_mapping
        except ImportError:
            apply_saved_mapping = None  # type: ignore[assignment]

        try:
            from ma_poc.models.source import envelope_hash_of as _env_hash
        except ImportError:
            _env_hash = None  # type: ignore[assignment]

        if apply_saved_mapping is not None:
            for mapping in saved:
                try:
                    pat = getattr(mapping, "api_url_pattern", None) or (
                        mapping.get("api_url_pattern") if isinstance(mapping, dict) else None
                    )
                except Exception:
                    pat = None
                if not pat:
                    continue
                for resp in api_responses:
                    if pat in resp.get("url", ""):
                        body = resp.get("body")
                        saved_hash = getattr(mapping, "source_envelope_hash", "") or ""
                        if saved_hash and _env_hash is not None:
                            current_hash = _env_hash(body)
                            if current_hash != saved_hash:
                                if hasattr(mapping, "consecutive_replay_failures"):
                                    try:
                                        mapping.consecutive_replay_failures += 1
                                    except Exception:
                                        pass
                                try:
                                    from ma_poc.observability.events import EventKind, emit
                                    emit(
                                        EventKind.MAPPING_DRIFT_DETECTED,
                                        getattr(ctx, "property_id", "unknown"),
                                        url=str(resp.get("url", ""))[:80],
                                        saved_hash=saved_hash[:8],
                                        current_hash=current_hash[:8],
                                    )
                                except Exception:
                                    pass
                                continue
                        mdict = (
                            mapping
                            if isinstance(mapping, dict)
                            else {
                                "api_url_pattern": pat,
                                "json_paths": getattr(mapping, "json_paths", {}) or {},
                                "response_envelope": getattr(mapping, "response_envelope", "") or "",
                            }
                        )
                        try:
                            units = apply_saved_mapping(body, mdict) or []
                        except Exception:
                            units = []
                        if hasattr(mapping, "last_replayed_at"):
                            try:
                                mapping.last_replayed_at = datetime.utcnow()
                            except Exception:
                                pass
                        if units:
                            replayed_units.extend(units)
                            result.api_responses.append(resp)
                            replayed_mappings.append(mapping)
                            if hasattr(mapping, "success_count"):
                                try:
                                    mapping.success_count += 1
                                except Exception:
                                    pass
                            if hasattr(mapping, "consecutive_replay_failures"):
                                try:
                                    mapping.consecutive_replay_failures = 0
                                except Exception:
                                    pass
                            break
                        else:
                            if hasattr(mapping, "consecutive_replay_failures"):
                                try:
                                    mapping.consecutive_replay_failures += 1
                                except Exception:
                                    pass
                            try:
                                from ma_poc.observability.events import EventKind, emit
                                emit(
                                    EventKind.MAPPING_REPLAY_EMPTY,
                                    getattr(ctx, "property_id", "unknown"),
                                    url=str(resp.get("url", ""))[:80],
                                )
                            except Exception:
                                pass

        if replayed_units:
            try:
                _fp_list = list(getattr(profile.api_hints, "field_patches", []) or [])
                if _fp_list:
                    replayed_units = _apply_field_patches(
                        replayed_units, api_responses, _fp_list, log_patch_hit, log_patch_miss
                    )
            except Exception:
                pass

            agg_q = _aggregate_quality(replayed_mappings)

            if agg_q >= 0.7:
                log_attempt(
                    "generic:profile_replay",
                    "ran_units",
                    units=len(replayed_units),
                    reason="replayed saved LlmFieldMapping (quality ok)",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
                return {"replayed_units": replayed_units, "agg_quality": agg_q, "short_circuit": True}

            log_attempt(
                "generic:profile_replay",
                "ran_units",
                units=len(replayed_units),
                reason="replayed mapping field-incomplete or low quality; cascading + merging",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            return {"replayed_units": replayed_units, "agg_quality": agg_q, "short_circuit": False}

        log_attempt(
            "generic:profile_replay",
            "ran_empty",
            reason="saved mappings didn't match any captured API",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return {"replayed_units": [], "agg_quality": 1.0, "short_circuit": False}
