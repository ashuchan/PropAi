"""Runner-startup sentinel probe for self-learning persistence.

Writes a sentinel ``ScrapeProfile`` carrying one entry in every channel that
``services.profile_updater.update_profile_after_extraction`` populates, reads
it back, and asserts each channel round-trips. The intent is to catch
deploy-time regressions of the persistence layer — Pydantic model drift, a
``SqlProfileStore.put`` serializer dropping a field, a partial alembic
migration — instead of three days later when the analyser notices the cache
is empty.

The probe is gated by ``ENABLE_PERSISTENCE_PROBE`` (default ``true``). On
failure it emits ``STARTUP_PROBE_FAILED`` and raises ``PersistenceProbeError``
— the runner exits non-zero, the shard's PG sync sees the bad exit code, and
the daily run is gated rather than poisoning the DB with a half-broken state.

The sentinel canonical_id ``__persistence_probe__`` is reserved; the probe
deletes its own row at the end of every successful run so it never appears in
analytics queries.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ma_poc.models.scrape_profile import (
    ApiEndpoint,
    BlockedEndpoint,
    FieldPatch,
    FieldSelectorMap,
    LlmFieldMapping,
    ScrapeProfile,
)

log = logging.getLogger(__name__)

SENTINEL_CANONICAL_ID = "__persistence_probe__"


class PersistenceProbeError(RuntimeError):
    """Raised when the round-trip probe detects a persistence regression."""


def _enabled() -> bool:
    """Read each call so a flip via env var doesn't require a process restart."""
    return os.environ.get("ENABLE_PERSISTENCE_PROBE", "true").lower() == "true"


def _build_sentinel_profile() -> ScrapeProfile:
    """Construct a sentinel that exercises every writeable channel.

    Every field below corresponds to a writer in
    ``profile_updater.update_profile_after_extraction``. Adding a new
    writer to the updater MUST add a corresponding field here AND a
    corresponding assertion in ``_assert_round_trip``.
    """
    sentinel = ScrapeProfile(canonical_id=SENTINEL_CANONICAL_ID)
    # Channel 1 — llm_field_mappings
    sentinel.api_hints.llm_field_mappings.append(
        LlmFieldMapping(
            api_url_pattern="https://probe.example.com/api/units",
            json_paths={"rent": "units.0.rent", "unit_id": "units.0.id"},
            response_envelope="units",
            source_envelope_hash="probe_hash_16chr",
            quality_score=1.0,
        )
    )
    # Channel 2 — blocked_endpoints
    sentinel.api_hints.blocked_endpoints.append(
        BlockedEndpoint(
            url_pattern="https://probe.example.com/api/chatbot",
            reason="probe_blocked",
        )
    )
    # Channel 3 — known_endpoints
    sentinel.api_hints.known_endpoints.append(
        ApiEndpoint(
            url_pattern="https://probe.example.com/api/floorplans",
            json_paths={"name": "floorplans.0.name"},
            provider="probe_provider",
        )
    )
    # Channel 6 — field_patches (PR 2 added; covers per-field replay).
    # If FieldPatch's Pydantic Literal[field_name] gets out of sync with
    # the producer at jugnu.py:1324 _PATCH_FIELDS, this round-trip fails
    # at deploy time instead of silently producing zero patches in the DB.
    sentinel.api_hints.field_patches.append(
        FieldPatch(
            api_url_pattern="https://probe.example.com/api/units",
            field_name="rent_low",
            json_path="units[*].rent",
            confidence=0.9,
            source_envelope_hash="probe_hash_16chr",
        )
    )
    # Channel 4 — dom_hints.field_selectors
    sentinel.dom_hints.field_selectors = FieldSelectorMap(
        container=".probe-unit",
        rent=".probe-rent",
        sqft=".probe-sqft",
    )
    sentinel.dom_hints.platform_detected = "probe_platform"
    # Channel 5 — navigation
    sentinel.navigation.winning_page_url = "https://probe.example.com/floorplans"
    sentinel.navigation.last_navigation_hints = ["/probe-hint"]
    return sentinel


def _assert_round_trip(loaded: ScrapeProfile) -> None:
    """Each assertion corresponds to one writeable channel.

    Raises ``PersistenceProbeError`` with a precise diagnostic on the first
    channel that fails. The error message names the channel and the
    expected vs actual values so the operator can tell at a glance whether
    the regression is in the model layer, the serializer, the schema, or
    the DB grant.
    """
    if loaded.canonical_id != SENTINEL_CANONICAL_ID:
        raise PersistenceProbeError(
            f"canonical_id mismatch: expected {SENTINEL_CANONICAL_ID!r}, got {loaded.canonical_id!r}"
        )

    mappings = loaded.api_hints.llm_field_mappings
    if len(mappings) != 1:
        raise PersistenceProbeError(
            f"channel=llm_field_mappings: expected 1 entry, got {len(mappings)}"
        )
    m = mappings[0]
    if m.api_url_pattern != "https://probe.example.com/api/units":
        raise PersistenceProbeError(
            f"channel=llm_field_mappings: api_url_pattern lost (got {m.api_url_pattern!r})"
        )
    if m.json_paths.get("rent") != "units.0.rent":
        raise PersistenceProbeError(
            f"channel=llm_field_mappings: json_paths corrupted (got {m.json_paths!r})"
        )
    if m.response_envelope != "units":
        raise PersistenceProbeError(
            f"channel=llm_field_mappings: response_envelope lost (got {m.response_envelope!r})"
        )

    blocked = loaded.api_hints.blocked_endpoints
    if not any(b.url_pattern == "https://probe.example.com/api/chatbot" for b in blocked):
        raise PersistenceProbeError(
            f"channel=blocked_endpoints: probe entry not found (got {len(blocked)} entries)"
        )

    known = loaded.api_hints.known_endpoints
    if not any(k.url_pattern == "https://probe.example.com/api/floorplans" for k in known):
        raise PersistenceProbeError(
            f"channel=known_endpoints: probe entry not found (got {len(known)} entries)"
        )

    fs = loaded.dom_hints.field_selectors
    if fs is None or fs.container != ".probe-unit":
        raise PersistenceProbeError(
            f"channel=dom_hints.field_selectors: container lost (got {fs.container if fs else None!r})"
        )

    # Channel 6 — field_patches (PR 2)
    patches = loaded.api_hints.field_patches
    if not any(
        p.api_url_pattern == "https://probe.example.com/api/units"
        and p.field_name == "rent_low"
        and p.json_path == "units[*].rent"
        for p in patches
    ):
        raise PersistenceProbeError(
            f"channel=field_patches: probe entry not found (got {len(patches)} entries)"
        )

    if loaded.navigation.winning_page_url != "https://probe.example.com/floorplans":
        raise PersistenceProbeError(
            "channel=navigation.winning_page_url: lost"
        )
    if "/probe-hint" not in loaded.navigation.last_navigation_hints:
        raise PersistenceProbeError(
            "channel=navigation.last_navigation_hints: probe hint missing"
        )


def _safe_emit(kind_name: str, **payload: Any) -> None:
    """Emit a startup-probe event without taking down the runner if the ledger isn't ready."""
    try:
        from ma_poc.observability.events import EventKind, emit
        kind = getattr(EventKind, kind_name)
        emit(kind, "", **payload)
    except Exception:
        pass


def run_sentinel_probe(store: Any) -> None:
    """Round-trip a sentinel profile through ``store`` to verify persistence.

    Args:
        store: Any object satisfying the ``IProfileStore`` contract —
            i.e. exposes ``get(canonical_id) -> ScrapeProfile | None``,
            ``put(profile)``, and ``delete(canonical_id) -> bool``.
            Both the FS ``ProfileStore`` (with ``save``/``load``) and the
            data-provider-backed ``SqlProfileStore`` (with ``put``/``get``)
            are accepted via the small adapter at the call site (see
            ``_put_via_store`` / ``_get_via_store``).

    Raises:
        PersistenceProbeError: round-trip detected a regression.
        Other exceptions propagate; the runner converts them into a
            non-zero exit so the shard's PG sync is gated.
    """
    if not _enabled():
        log.info("Persistence probe disabled by env (ENABLE_PERSISTENCE_PROBE=false); skipping.")
        return

    sentinel = _build_sentinel_profile()
    try:
        _put_via_store(store, sentinel)
    except Exception as exc:
        _safe_emit("STARTUP_PROBE_FAILED", phase="put", error=str(exc)[:200])
        raise PersistenceProbeError(f"sentinel put failed: {exc}") from exc

    try:
        loaded = _get_via_store(store, SENTINEL_CANONICAL_ID)
    except Exception as exc:
        _safe_emit("STARTUP_PROBE_FAILED", phase="get", error=str(exc)[:200])
        raise PersistenceProbeError(f"sentinel get failed: {exc}") from exc

    if loaded is None:
        _safe_emit("STARTUP_PROBE_FAILED", phase="get", error="returned None")
        raise PersistenceProbeError("sentinel put succeeded but get returned None")

    try:
        _assert_round_trip(loaded)
    except PersistenceProbeError as exc:
        _safe_emit("STARTUP_PROBE_FAILED", phase="assert", error=str(exc)[:200])
        raise

    # Cleanup is best-effort — a left-over sentinel profile is harmless
    # (next run overwrites it). Don't let cleanup failure mask success.
    try:
        _delete_via_store(store, SENTINEL_CANONICAL_ID)
    except Exception as exc:
        log.warning("Persistence probe sentinel cleanup failed (non-fatal): %s", exc)

    _safe_emit("STARTUP_PROBE_OK")
    log.info("Persistence probe OK: all writeable channels round-tripped (mappings, blocked, known, dom_selectors, navigation, field_patches).")


# --- store adapter helpers ------------------------------------------------
# The runner's _SimpleProfileStore wraps a backing IProfileStore but exposes
# get_profile/save (legacy names from the old FS ProfileStore). The probe
# accepts either contract so it can run against the wrapper or the raw
# backing store directly.


def _put_via_store(store: Any, profile: ScrapeProfile) -> None:
    if hasattr(store, "put"):
        store.put(profile)
    elif hasattr(store, "save"):
        store.save(profile)
    else:
        raise PersistenceProbeError(
            f"store {type(store).__name__} has neither put() nor save()"
        )


def _get_via_store(store: Any, canonical_id: str) -> ScrapeProfile | None:
    if hasattr(store, "get_profile"):
        return store.get_profile(canonical_id)
    if hasattr(store, "get"):
        return store.get(canonical_id)
    if hasattr(store, "load"):
        return store.load(canonical_id)
    raise PersistenceProbeError(
        f"store {type(store).__name__} has none of get_profile/get/load"
    )


def _delete_via_store(store: Any, canonical_id: str) -> bool:
    if hasattr(store, "delete"):
        return bool(store.delete(canonical_id))
    # Some FS stores write to a path; cleanup is optional.
    log.debug("store %s has no delete(); leaving sentinel in place", type(store).__name__)
    return False
