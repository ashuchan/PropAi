#!/usr/bin/env python3
"""Reusable report generator backed by the local Postgres ``proppy`` DB.

Two reports are available:

  * ``health`` (default) — last-N-days success rate, unit data quality,
    day-over-day diffs, and system performance/runtime.
  * ``extraction-quality`` — amenities, concessions, and unit-count
    ("quantity") summaries with failure breakdowns and detail samples.
  * ``all`` — emit both reports.

Uses pg8000 because psycopg-binary has no ARM64 Windows wheel
(see project memory).

    python scripts/reports/health.py                                  # health, today
    python scripts/reports/health.py --report extraction-quality
    python scripts/reports/health.py --report all --run-date 2026-05-04
    python scripts/reports/health.py --days 7 --json                  # 7-day window + JSON
    python scripts/reports/health.py --out -                          # stdout only (single report)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pg8000


# ---------------------------------------------------------------------------
# GCS source (parallel to the DB source)
# ---------------------------------------------------------------------------

DEFAULT_GCS_BUCKET = "jugnu-raw-production"
DEFAULT_GCS_PREFIX = "runs"


def _gcs_client() -> Any:
    """Lazy import so DB-only invocations don't need google-cloud-storage."""
    from google.cloud import storage  # type: ignore[import-untyped]
    return storage.Client()


def _gcs_list_shards(client: Any, bucket: str, prefix: str, run_date: str) -> list[str]:
    """Return sorted shard directory names (e.g. ['shard_0', 'shard_1', …])."""
    base = f"{prefix}/{run_date}/"
    shards: set[str] = set()
    for blob in client.list_blobs(bucket, prefix=base):
        parts = blob.name[len(base):].split("/")
        if parts and parts[0].startswith("shard_"):
            shards.add(parts[0])
    return sorted(shards, key=lambda s: int(s.split("_")[1]) if s.split("_")[1].isdigit() else 9_999)


def _gcs_read_json(client: Any, bucket: str, path: str) -> Any:
    """Download and parse a JSON blob; return None if missing."""
    blob = client.bucket(bucket).blob(path)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_string().decode("utf-8", errors="replace"))


def _gcs_load_run(client: Any, bucket: str, prefix: str, run_date: str) -> dict[str, Any]:
    """Load all shard ``report.json`` + ``properties.json`` for a single run.
    Returns ``{shards: {shard_name: {report, properties}}, run_date}``."""
    shards = _gcs_list_shards(client, bucket, prefix, run_date)
    out: dict[str, Any] = {"run_date": run_date, "shards": {}}
    for s in shards:
        rep = _gcs_read_json(client, bucket, f"{prefix}/{run_date}/{s}/report.json")
        props = _gcs_read_json(client, bucket, f"{prefix}/{run_date}/{s}/properties.json")
        out["shards"][s] = {
            "report": rep or {},
            "properties": props if isinstance(props, list) else [],
        }
    return out


def _gcs_events_wallclock(client: Any, bucket: str, prefix: str, run_date: str,
                          shard: str) -> dict[str, Any]:
    """Stream a single shard's events.jsonl; capture only first/last timestamps
    plus the event count (no per-event retention)."""
    blob_path = f"{prefix}/{run_date}/{shard}/events.jsonl"
    blob = client.bucket(bucket).blob(blob_path)
    if not blob.exists():
        return {"first_ts": None, "last_ts": None, "count": 0}
    data = blob.download_as_string().decode("utf-8", errors="replace")
    first_ts: str | None = None
    last_ts: str | None = None
    count = 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = ev.get("ts")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        count += 1
    return {"first_ts": first_ts, "last_ts": last_ts, "count": count}


def _aggregate_run(run: dict[str, Any]) -> dict[str, Any]:
    """Roll up shard-level totals into a single run summary equivalent to the
    DB ``run_reports.totals`` shape."""
    props = succ = fail = cf = 0
    llm_cost = 0.0
    tier_dist: dict[str, int] = {}
    gen_ats: list[str] = []
    for s, payload in run.get("shards", {}).items():
        rep = payload.get("report") or {}
        t = rep.get("totals") or {}
        props += int(t.get("properties") or 0)
        succ += int(t.get("succeeded") or 0)
        fail += int(t.get("failed") or 0)
        cf += int(t.get("carry_forward") or 0)
        llm_cost += float((rep.get("cost") or {}).get("llm") or 0.0)
        for k, v in (rep.get("tier_distribution") or {}).items():
            tier_dist[k] = tier_dist.get(k, 0) + int(v)
        if rep.get("generated_at"):
            gen_ats.append(rep["generated_at"])
    rate = round(100.0 * succ / props, 2) if props else 0.0
    return {
        "run_date": run["run_date"],
        "totals": {
            "properties": props,
            "succeeded": succ,
            "failed": fail,
            "carry_forward": cf,
            "success_rate_pct": rate,
        },
        "tier_distribution": tier_dist,
        "cost": {"llm": round(llm_cost, 4)},
        "shard_generated_ats": gen_ats,
        "shard_count": len(run.get("shards", {})),
    }


def _flatten_properties(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``shards.*.properties`` to a single list of property dicts."""
    out: list[dict[str, Any]] = []
    for _, payload in run.get("shards", {}).items():
        for p in payload.get("properties") or []:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _connect() -> pg8000.Connection:
    """Open a pg8000 connection. Reads DATABASE_URL from env (.env-style) or
    falls back to local proppy defaults. Strips the SQLAlchemy ``+driver`` suffix."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DATABASE_URL=") and not line.startswith("#"):
                    url = line.split("=", 1)[1].strip()
                    break
    if not url:
        url = "postgresql://postgres:Ashu%40007saxe@localhost:5432/proppy"

    if "://" in url:
        scheme, rest = url.split("://", 1)
        scheme = scheme.split("+", 1)[0]
        url = f"{scheme}://{rest}"

    p = urlparse(url)
    return pg8000.connect(
        user=unquote(p.username or ""),
        password=unquote(p.password or ""),
        host=p.hostname or "localhost",
        port=p.port or 5432,
        database=(p.path or "/proppy").lstrip("/") or "proppy",
    )


# ---------------------------------------------------------------------------
# Metric collectors
# ---------------------------------------------------------------------------

@dataclass
class HealthReport:
    run_date: str
    generated_at: str
    success_window_days: int
    source_label: str = "Postgres (proppy)"
    success_history: list[dict[str, Any]] = field(default_factory=list)
    unit_quality: dict[str, Any] = field(default_factory=dict)
    diff_yesterday: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionQualityReport:
    """Summary, failure breakdown, and details for the three semi-extracted
    fields: amenities, concessions, and unit-count ("quantity")."""
    run_date: str
    generated_at: str
    source_label: str = "Postgres (proppy)"
    amenities: dict[str, Any] = field(default_factory=dict)
    concessions: dict[str, Any] = field(default_factory=dict)
    quantity: dict[str, Any] = field(default_factory=dict)


def _collect_success_history(cur: Any, end_date: str, days: int) -> list[dict[str, Any]]:
    """Per-day property success/failure counts from ``run_reports.totals``."""
    end = date.fromisoformat(end_date)
    start = end - timedelta(days=days - 1)
    cur.execute(
        "SELECT run_date, totals FROM run_reports "
        "WHERE run_date >= %s AND run_date <= %s "
        "ORDER BY run_date DESC",
        (start.isoformat(), end.isoformat()),
    )
    rows: list[dict[str, Any]] = []
    for d, totals in cur.fetchall():
        if not isinstance(totals, dict):
            continue
        props = int(totals.get("properties") or 0)
        succ = int(totals.get("succeeded") or 0)
        fail = int(totals.get("failed") or 0)
        rate = round(100.0 * succ / props, 2) if props else 0.0
        rows.append({
            "run_date": d,
            "properties": props,
            "succeeded": succ,
            "failed": fail,
            "success_pct": rate,
            "failure_pct": round(100.0 - rate, 2) if props else 0.0,
        })
    return rows


def _collect_unit_quality(cur: Any, run_date: str) -> dict[str, Any]:
    """Unit data-quality metrics. Counts come from ``property_snapshots`` for the
    target run plus the last 3/2 runs of presence."""

    # Latest run unit roster (canonical_id, unit_id) — extract from snapshot payloads
    cur.execute(
        "SELECT run_date, canonical_id, payload->'units' AS units "
        "FROM property_snapshots WHERE run_date = %s",
        (run_date,),
    )
    today_units: list[tuple[str, str, dict[str, Any]]] = []  # (cid, uid, unit_dict)
    for d, cid, units in cur.fetchall():
        if not units:
            continue
        for idx, u in enumerate(units):
            uid = u.get("unit_id") or u.get("floor_plan_name") or f"_idx{idx}"
            today_units.append((cid, str(uid), u))

    total = len(today_units)
    with_rent = sum(1 for _, _, u in today_units if u.get("rent_low") or u.get("rent_high"))
    with_avail = sum(
        1 for _, _, u in today_units
        if u.get("available_date") or u.get("move_in_date")
    )

    # Build set of (cid, uid) per recent day from property_snapshots — last 3 runs.
    end = date.fromisoformat(run_date)
    recent = [(end - timedelta(days=i)).isoformat() for i in range(3)]

    per_day_keys: dict[str, set[tuple[str, str]]] = {}
    cur.execute(
        "SELECT run_date, canonical_id, payload->'units' AS units "
        "FROM property_snapshots WHERE run_date = ANY(%s)",
        (recent,),
    )
    for d, cid, units in cur.fetchall():
        if not units:
            continue
        per_day_keys.setdefault(d, set())
        for idx, u in enumerate(units):
            uid = u.get("unit_id") or u.get("floor_plan_name") or f"_idx{idx}"
            per_day_keys[d].add((cid, str(uid)))

    today_set = per_day_keys.get(recent[0], set())
    yest_set = per_day_keys.get(recent[1], set())
    day_before = per_day_keys.get(recent[2], set())

    seen_3d = today_set & yest_set & day_before
    seen_2d = today_set & yest_set

    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0
    return {
        "as_of_run_date": run_date,
        "total_units": total,
        "with_rent_count": with_rent,
        "with_rent_pct": pct(with_rent, total),
        "with_availability_count": with_avail,
        "with_availability_pct": pct(with_avail, total),
        "consistency_window_dates": recent,
        "consistency_day_counts": {d: len(per_day_keys.get(d, set())) for d in recent},
        "seen_consistently_3d_count": len(seen_3d),
        "seen_consistently_3d_pct": pct(len(seen_3d), total),
        "seen_consistently_2d_count": len(seen_2d),
        "seen_consistently_2d_pct": pct(len(seen_2d), total),
    }


def _collect_diff_yesterday(cur: Any, run_date: str) -> dict[str, Any]:
    """Day-over-day diff: properties new today, missed today, rent up/down."""
    today = run_date
    yest = (date.fromisoformat(run_date) - timedelta(days=1)).isoformat()

    # Property sets per day from run_ledger (status SUCCESS = scraped this run)
    cur.execute(
        "SELECT canonical_id, status FROM run_ledger WHERE run_date = %s",
        (today,),
    )
    today_rows = cur.fetchall()
    today_props = {cid for cid, _ in today_rows}
    today_success = {cid for cid, st in today_rows if st == "SUCCESS"}

    cur.execute(
        "SELECT canonical_id, status FROM run_ledger WHERE run_date = %s",
        (yest,),
    )
    yest_rows = cur.fetchall()
    yest_props = {cid for cid, _ in yest_rows}
    yest_success = {cid for cid, st in yest_rows if st == "SUCCESS"}

    new_props = sorted(today_props - yest_props)
    missed = sorted(yest_props - today_props)
    today_failed = sorted(today_props - today_success)

    # Rent diffs: median rent per property across both days
    def _rents(d: str) -> dict[str, float]:
        cur.execute(
            "SELECT canonical_id, payload->'units' AS units "
            "FROM property_snapshots WHERE run_date = %s",
            (d,),
        )
        out: dict[str, float] = {}
        for cid, units in cur.fetchall():
            if not units:
                continue
            vals = []
            for u in units:
                r = u.get("rent_low") or u.get("rent_high")
                if r is not None:
                    try:
                        vals.append(float(r))
                    except (TypeError, ValueError):
                        pass
            if vals:
                vals.sort()
                mid = vals[len(vals) // 2]
                out[cid] = mid
        return out

    today_rent = _rents(today)
    yest_rent = _rents(yest)

    rent_up: list[dict[str, Any]] = []
    rent_down: list[dict[str, Any]] = []
    rent_unchanged = 0
    for cid, t in today_rent.items():
        y = yest_rent.get(cid)
        if y is None:
            continue
        delta = round(t - y, 2)
        if delta > 0:
            rent_up.append({"canonical_id": cid, "yesterday": y, "today": t, "delta": delta})
        elif delta < 0:
            rent_down.append({"canonical_id": cid, "yesterday": y, "today": t, "delta": delta})
        else:
            rent_unchanged += 1
    rent_up.sort(key=lambda r: -r["delta"])
    rent_down.sort(key=lambda r: r["delta"])

    return {
        "today": today,
        "yesterday": yest,
        "today_property_count": len(today_props),
        "yesterday_property_count": len(yest_props),
        "new_properties_count": len(new_props),
        "new_properties_sample": new_props[:25],
        "missed_today_count": len(missed),
        "missed_today_sample": missed[:25],
        "failed_today_count": len(today_failed),
        "rent_up_count": len(rent_up),
        "rent_up_top10": rent_up[:10],
        "rent_down_count": len(rent_down),
        "rent_down_top10": rent_down[:10],
        "rent_unchanged_count": rent_unchanged,
        "compared_property_count": len(set(today_rent) & set(yest_rent)),
    }


def _collect_performance(cur: Any, run_date: str, lookback_days: int = 5) -> dict[str, Any]:
    """Wall-clock runtime per shard count, plus avg seconds per property."""
    end = date.fromisoformat(run_date)
    start = end - timedelta(days=lookback_days - 1)

    cur.execute(
        "SELECT run_date, extra FROM run_reports "
        "WHERE run_date >= %s AND run_date <= %s ORDER BY run_date DESC",
        (start.isoformat(), end.isoformat()),
    )
    by_shard_count: dict[int, list[dict[str, Any]]] = {}
    daily: list[dict[str, Any]] = []
    for d, extra in cur.fetchall():
        if not isinstance(extra, dict):
            continue
        shards = extra.get("shards") or {}
        if not shards:
            continue
        gens = []
        for v in shards.values():
            ts = v.get("generated_at")
            if not ts:
                continue
            try:
                gens.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except ValueError:
                pass
        if not gens:
            continue
        spread_min = (max(gens) - min(gens)).total_seconds() / 60.0
        n_shards = len(shards)

        # Wall-clock from scrape_events on the same run (covers actual scrape duration)
        cur.execute(
            "SELECT min(scrape_timestamp), max(scrape_timestamp), count(*) "
            "FROM scrape_events WHERE date(scrape_timestamp) = %s",
            (d,),
        )
        s_min, s_max, n_events = cur.fetchone()
        wall_min = (s_max - s_min).total_seconds() / 60.0 if s_min and s_max else None
        avg_sec_per_prop = round(((s_max - s_min).total_seconds() / n_events), 3) \
            if s_min and s_max and n_events else None

        entry = {
            "run_date": d,
            "shard_count": n_shards,
            "shards_gen_at_spread_min": round(spread_min, 1),
            "scrape_events_wallclock_min": round(wall_min, 1) if wall_min else None,
            "scrape_events_count": n_events,
            "avg_seconds_per_property": avg_sec_per_prop,
        }
        daily.append(entry)
        by_shard_count.setdefault(n_shards, []).append(entry)

    # Aggregate by shard-count bucket
    aggregates: dict[str, Any] = {}
    for nshards, entries in sorted(by_shard_count.items()):
        wall = [e["scrape_events_wallclock_min"] for e in entries if e["scrape_events_wallclock_min"]]
        avg = [e["avg_seconds_per_property"] for e in entries if e["avg_seconds_per_property"]]
        aggregates[str(nshards)] = {
            "runs_observed": len(entries),
            "wallclock_min_avg": round(sum(wall) / len(wall), 1) if wall else None,
            "wallclock_min_min": round(min(wall), 1) if wall else None,
            "wallclock_min_max": round(max(wall), 1) if wall else None,
            "avg_seconds_per_property": round(sum(avg) / len(avg), 3) if avg else None,
        }

    return {
        "lookback_days": lookback_days,
        "daily": daily,
        "by_shard_count": aggregates,
    }


# ---------------------------------------------------------------------------
# Extraction-quality collectors
# ---------------------------------------------------------------------------

def _has_amenity_fields(d: dict[str, Any]) -> bool:
    """A property/unit dict has amenity content if any of these keys hold a
    non-empty list/string."""
    for k in ("amenities", "property_amenities", "community_amenities", "unit_amenities"):
        v = d.get(k)
        if isinstance(v, list) and v:
            return True
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, dict) and v:
            return True
    return False


def _has_concession_content(value: Any) -> bool:
    """A concessions value is non-empty if it's a non-empty list/dict/str."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _collect_amenities(cur: Any, run_date: str) -> dict[str, Any]:
    """Coverage and details for amenities. Walks property_snapshots payload —
    the only source that carries the unit dict shape end-to-end."""
    cur.execute(
        "SELECT canonical_id, payload FROM property_snapshots WHERE run_date = %s",
        (run_date,),
    )
    rows = cur.fetchall()

    total_props = len(rows)
    props_with_amen = 0
    total_units = 0
    units_with_amen = 0
    sample_with: list[dict[str, Any]] = []
    keys_observed_property: set[str] = set()
    keys_observed_unit: set[str] = set()

    for cid, payload in rows:
        if not isinstance(payload, dict):
            continue
        keys_observed_property.update(payload.keys())
        if _has_amenity_fields(payload):
            props_with_amen += 1
            if len(sample_with) < 10:
                amen_value = next(
                    (payload.get(k) for k in
                     ("amenities", "property_amenities", "community_amenities")
                     if payload.get(k)),
                    None,
                )
                sample_with.append({"canonical_id": cid, "value": amen_value})
        units = payload.get("units") or []
        for u in units:
            if not isinstance(u, dict):
                continue
            total_units += 1
            keys_observed_unit.update(u.keys())
            if _has_amenity_fields(u):
                units_with_amen += 1

    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0
    return {
        "as_of_run_date": run_date,
        "total_properties": total_props,
        "properties_with_amenities_count": props_with_amen,
        "properties_with_amenities_pct": pct(props_with_amen, total_props),
        "properties_without_amenities_count": total_props - props_with_amen,
        "total_units": total_units,
        "units_with_amenities_count": units_with_amen,
        "units_with_amenities_pct": pct(units_with_amen, total_units),
        "property_keys_observed": sorted(keys_observed_property),
        "unit_keys_observed": sorted(keys_observed_unit),
        "samples_with_amenities": sample_with,
        "failure_summary": {
            "primary_cause": (
                "No extractor writes amenity strings into property/unit records."
                " Phase 6 amenities reader sums an absent field, so coverage is 0%"
                " by construction even when amenity pages are crawled."
            ) if props_with_amen == 0 else None,
            "missing_property_field": "amenities" not in keys_observed_property,
            "missing_unit_field": "amenities" not in keys_observed_unit,
        },
    }


def _collect_concessions(cur: Any, run_date: str) -> dict[str, Any]:
    """Coverage and details for property/unit concessions."""
    # Property snapshots view (authoritative for the run)
    cur.execute(
        "SELECT canonical_id, payload FROM property_snapshots WHERE run_date = %s",
        (run_date,),
    )
    rows = cur.fetchall()

    total_props = len(rows)
    props_with_conc = 0
    total_units = 0
    units_with_conc = 0
    samples: list[dict[str, Any]] = []
    schema_field_present_props = 0
    schema_field_present_units = 0

    for cid, payload in rows:
        if not isinstance(payload, dict):
            continue
        if "concessions" in payload:
            schema_field_present_props += 1
        if _has_concession_content(payload.get("concessions")):
            props_with_conc += 1
            if len(samples) < 10:
                samples.append({
                    "canonical_id": cid,
                    "value": payload.get("concessions"),
                })
        units = payload.get("units") or []
        for u in units:
            if not isinstance(u, dict):
                continue
            total_units += 1
            if "concessions" in u:
                schema_field_present_units += 1
            if _has_concession_content(u.get("concessions")):
                units_with_conc += 1

    # DB current-state mirror
    cur.execute(
        "SELECT count(*), count(*) FILTER (WHERE concessions IS NOT NULL AND concessions != '') "
        "FROM properties"
    )
    p_total, p_with = cur.fetchone()
    cur.execute(
        "SELECT count(*), count(*) FILTER (WHERE concessions IS NOT NULL AND concessions::text NOT IN ('null','[]','{}','\"\"')) "
        "FROM units"
    )
    u_total, u_with = cur.fetchone()

    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0
    return {
        "as_of_run_date": run_date,
        "snapshot_total_properties": total_props,
        "snapshot_properties_with_concessions_count": props_with_conc,
        "snapshot_properties_with_concessions_pct": pct(props_with_conc, total_props),
        "snapshot_total_units": total_units,
        "snapshot_units_with_concessions_count": units_with_conc,
        "snapshot_units_with_concessions_pct": pct(units_with_conc, total_units),
        "schema_field_present_property_pct": pct(schema_field_present_props, total_props),
        "schema_field_present_unit_pct": pct(schema_field_present_units, total_units),
        "db_properties_total": p_total,
        "db_properties_with_concessions": p_with,
        "db_units_total": u_total,
        "db_units_with_concessions": u_with,
        "samples_with_concessions": samples,
        "failure_summary": {
            "primary_cause": (
                "Property records carry the `concessions` key but it is never"
                " populated. Detector flags concession signals (e.g."
                " specialsbanner.js, specials anchors) but no downstream"
                " extractor reads the rendered banner."
            ) if props_with_conc == 0 else None,
            "schema_present_but_unpopulated": (
                schema_field_present_props == total_props and props_with_conc == 0
                and total_props > 0
            ),
        },
    }


def _collect_quantity(cur: Any, run_date: str) -> dict[str, Any]:
    """"Quantity" = number of units captured per property. Distribution and
    breakdown by verdict."""
    cur.execute(
        "SELECT canonical_id, payload FROM property_snapshots WHERE run_date = %s",
        (run_date,),
    )
    snap_rows = cur.fetchall()

    # Map canonical_id -> verdict from run_ledger
    cur.execute(
        "SELECT canonical_id, status FROM run_ledger WHERE run_date = %s",
        (run_date,),
    )
    verdict = {cid: st for cid, st in cur.fetchall()}

    counts: list[int] = []
    counts_success: list[int] = []
    counts_failed: list[int] = []
    by_bucket = {"0": 0, "1": 0, "2-5": 0, "6-20": 0, "21-100": 0, "100+": 0}
    zero_unit_props: list[dict[str, str]] = []
    top_props: list[tuple[str, int]] = []

    for cid, payload in snap_rows:
        units = (payload or {}).get("units") or []
        n = len(units) if isinstance(units, list) else 0
        counts.append(n)
        if verdict.get(cid) == "SUCCESS":
            counts_success.append(n)
        else:
            counts_failed.append(n)
        if n == 0:
            by_bucket["0"] += 1
            if len(zero_unit_props) < 25:
                zero_unit_props.append({
                    "canonical_id": cid,
                    "verdict": verdict.get(cid, "?"),
                })
        elif n == 1:
            by_bucket["1"] += 1
        elif n <= 5:
            by_bucket["2-5"] += 1
        elif n <= 20:
            by_bucket["6-20"] += 1
        elif n <= 100:
            by_bucket["21-100"] += 1
        else:
            by_bucket["100+"] += 1
        top_props.append((cid, n))

    top_props.sort(key=lambda r: -r[1])

    def _pct_stats(values: list[int]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        s = sorted(values)
        def pctl(p: float) -> float:
            if not s:
                return 0
            i = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
            return s[i]
        return {
            "n": len(s),
            "min": s[0],
            "max": s[-1],
            "mean": round(sum(s) / len(s), 2),
            "median": s[len(s) // 2],
            "p25": pctl(25),
            "p75": pctl(75),
            "p95": pctl(95),
            "p99": pctl(99),
        }

    return {
        "as_of_run_date": run_date,
        "interpretation": (
            "'Quantity' here is the per-property unit count captured for the run "
            "(length of the snapshot's `units` array). 0 = full extraction "
            "failure even if the verdict is SUCCESS at the URL level."
        ),
        "total_properties": len(counts),
        "total_units_captured": sum(counts),
        "distribution_buckets": by_bucket,
        "stats_overall": _pct_stats(counts),
        "stats_success_verdict": _pct_stats(counts_success),
        "stats_failed_verdict": _pct_stats(counts_failed),
        "zero_unit_property_count": by_bucket["0"],
        "zero_unit_sample": zero_unit_props,
        "top10_by_unit_count": [
            {"canonical_id": cid, "unit_count": n} for cid, n in top_props[:10]
        ],
    }


# ---------------------------------------------------------------------------
# GCS-source collectors (parallel to the DB collectors above)
# ---------------------------------------------------------------------------

def _canon_id(p: dict[str, Any]) -> str:
    meta = p.get("_meta") or {}
    return str(meta.get("canonical_id") or p.get("apartment_id") or p.get("canonical_id") or "")


def _verdict(p: dict[str, Any]) -> str:
    meta = p.get("_meta") or {}
    return str(meta.get("verdict") or "?")


def _gcs_success_history(client: Any, bucket: str, prefix: str,
                         end_date: str, days: int) -> list[dict[str, Any]]:
    end = date.fromisoformat(end_date)
    rows: list[dict[str, Any]] = []
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        run = _gcs_load_run(client, bucket, prefix, d)
        if not run["shards"]:
            continue
        agg = _aggregate_run(run)
        t = agg["totals"]
        rows.append({
            "run_date": d,
            "properties": t["properties"],
            "succeeded": t["succeeded"],
            "failed": t["failed"],
            "success_pct": t["success_rate_pct"],
            "failure_pct": round(100.0 - t["success_rate_pct"], 2) if t["properties"] else 0.0,
        })
    return rows


def _unit_quality_from_properties(today_props: list[dict[str, Any]],
                                  yest_props: list[dict[str, Any]],
                                  day_before_props: list[dict[str, Any]],
                                  run_date: str) -> dict[str, Any]:
    def _unit_keys(props: list[dict[str, Any]]) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for p in props:
            cid = _canon_id(p)
            for idx, u in enumerate(p.get("units") or []):
                if not isinstance(u, dict):
                    continue
                uid = u.get("unit_id") or u.get("floor_plan_name") or f"_idx{idx}"
                keys.add((cid, str(uid)))
        return keys

    today_set = _unit_keys(today_props)
    yest_set = _unit_keys(yest_props)
    day_before = _unit_keys(day_before_props)

    total = len(today_set)
    with_rent = 0
    with_avail = 0
    for p in today_props:
        for u in p.get("units") or []:
            if not isinstance(u, dict):
                continue
            if u.get("rent_low") or u.get("rent_high"):
                with_rent += 1
            if u.get("available_date") or u.get("move_in_date"):
                with_avail += 1

    seen_3d = today_set & yest_set & day_before
    seen_2d = today_set & yest_set
    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0

    return {
        "as_of_run_date": run_date,
        "total_units": total,
        "with_rent_count": with_rent,
        "with_rent_pct": pct(with_rent, total),
        "with_availability_count": with_avail,
        "with_availability_pct": pct(with_avail, total),
        "consistency_window_dates": [
            run_date,
            (date.fromisoformat(run_date) - timedelta(days=1)).isoformat(),
            (date.fromisoformat(run_date) - timedelta(days=2)).isoformat(),
        ],
        "consistency_day_counts": {
            run_date: len(today_set),
            (date.fromisoformat(run_date) - timedelta(days=1)).isoformat(): len(yest_set),
            (date.fromisoformat(run_date) - timedelta(days=2)).isoformat(): len(day_before),
        },
        "seen_consistently_3d_count": len(seen_3d),
        "seen_consistently_3d_pct": pct(len(seen_3d), total),
        "seen_consistently_2d_count": len(seen_2d),
        "seen_consistently_2d_pct": pct(len(seen_2d), total),
    }


def _diff_from_properties(today_props: list[dict[str, Any]],
                          yest_props: list[dict[str, Any]],
                          today_date: str, yest_date: str) -> dict[str, Any]:
    today_ids = {_canon_id(p) for p in today_props if _canon_id(p)}
    yest_ids = {_canon_id(p) for p in yest_props if _canon_id(p)}
    today_success = {_canon_id(p) for p in today_props if _verdict(p) == "SUCCESS"}

    new_props = sorted(today_ids - yest_ids)
    missed = sorted(yest_ids - today_ids)
    failed_today = sorted(today_ids - today_success)

    def _med_rents(props: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in props:
            cid = _canon_id(p)
            if not cid:
                continue
            vals = []
            for u in p.get("units") or []:
                if not isinstance(u, dict):
                    continue
                r = u.get("rent_low") or u.get("rent_high")
                if r is not None:
                    try:
                        vals.append(float(r))
                    except (TypeError, ValueError):
                        pass
            if vals:
                vals.sort()
                out[cid] = vals[len(vals) // 2]
        return out

    today_rent = _med_rents(today_props)
    yest_rent = _med_rents(yest_props)

    rent_up: list[dict[str, Any]] = []
    rent_down: list[dict[str, Any]] = []
    rent_unchanged = 0
    for cid, t in today_rent.items():
        y = yest_rent.get(cid)
        if y is None:
            continue
        delta = round(t - y, 2)
        if delta > 0:
            rent_up.append({"canonical_id": cid, "yesterday": y, "today": t, "delta": delta})
        elif delta < 0:
            rent_down.append({"canonical_id": cid, "yesterday": y, "today": t, "delta": delta})
        else:
            rent_unchanged += 1
    rent_up.sort(key=lambda r: -r["delta"])
    rent_down.sort(key=lambda r: r["delta"])

    return {
        "today": today_date,
        "yesterday": yest_date,
        "today_property_count": len(today_ids),
        "yesterday_property_count": len(yest_ids),
        "new_properties_count": len(new_props),
        "new_properties_sample": new_props[:25],
        "missed_today_count": len(missed),
        "missed_today_sample": missed[:25],
        "failed_today_count": len(failed_today),
        "rent_up_count": len(rent_up),
        "rent_up_top10": rent_up[:10],
        "rent_down_count": len(rent_down),
        "rent_down_top10": rent_down[:10],
        "rent_unchanged_count": rent_unchanged,
        "compared_property_count": len(set(today_rent) & set(yest_rent)),
    }


def _gcs_performance(client: Any, bucket: str, prefix: str, end_date: str,
                     lookback_days: int, *, scan_events: bool) -> dict[str, Any]:
    """Compute runtime + per-property avg seconds per day from GCS shards.

    ``scan_events=True`` downloads each shard's events.jsonl on the target
    day to compute true wall-clock; expensive but accurate. Older days fall
    back to shard ``generated_at`` spread."""
    end = date.fromisoformat(end_date)
    by_shard_count: dict[int, list[dict[str, Any]]] = {}
    daily: list[dict[str, Any]] = []

    for i in range(lookback_days):
        d = (end - timedelta(days=i)).isoformat()
        run = _gcs_load_run(client, bucket, prefix, d)
        if not run["shards"]:
            continue
        agg = _aggregate_run(run)
        gen_ats = []
        for ts in agg["shard_generated_ats"]:
            try:
                gen_ats.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except ValueError:
                pass
        spread_min = (max(gen_ats) - min(gen_ats)).total_seconds() / 60.0 if gen_ats else None

        wallclock_min: float | None = None
        events_count: int | None = None
        avg_sec_per_prop: float | None = None
        if scan_events and i == 0:
            firsts: list[datetime] = []
            lasts: list[datetime] = []
            ev_total = 0
            for shard in run["shards"]:
                ev = _gcs_events_wallclock(client, bucket, prefix, d, shard)
                ev_total += ev["count"]
                if ev["first_ts"]:
                    try:
                        firsts.append(datetime.fromisoformat(ev["first_ts"].replace("Z", "+00:00")))
                    except ValueError:
                        pass
                if ev["last_ts"]:
                    try:
                        lasts.append(datetime.fromisoformat(ev["last_ts"].replace("Z", "+00:00")))
                    except ValueError:
                        pass
            if firsts and lasts:
                wallclock_min = (max(lasts) - min(firsts)).total_seconds() / 60.0
                if agg["totals"]["properties"]:
                    avg_sec_per_prop = round(
                        (max(lasts) - min(firsts)).total_seconds() / agg["totals"]["properties"],
                        3,
                    )
            events_count = ev_total

        n_shards = agg["shard_count"]
        entry = {
            "run_date": d,
            "shard_count": n_shards,
            "shards_gen_at_spread_min": round(spread_min, 1) if spread_min is not None else None,
            "scrape_events_wallclock_min": round(wallclock_min, 1) if wallclock_min else None,
            "scrape_events_count": events_count,
            "avg_seconds_per_property": avg_sec_per_prop,
        }
        daily.append(entry)
        by_shard_count.setdefault(n_shards, []).append(entry)

    aggregates: dict[str, Any] = {}
    for nshards, entries in sorted(by_shard_count.items()):
        wall = [e["scrape_events_wallclock_min"] for e in entries if e["scrape_events_wallclock_min"]]
        spread = [e["shards_gen_at_spread_min"] for e in entries if e["shards_gen_at_spread_min"]]
        avg = [e["avg_seconds_per_property"] for e in entries if e["avg_seconds_per_property"]]
        aggregates[str(nshards)] = {
            "runs_observed": len(entries),
            "wallclock_min_avg": round(sum(wall) / len(wall), 1) if wall else None,
            "wallclock_min_min": round(min(wall), 1) if wall else None,
            "wallclock_min_max": round(max(wall), 1) if wall else None,
            "shard_spread_min_avg": round(sum(spread) / len(spread), 1) if spread else None,
            "avg_seconds_per_property": round(sum(avg) / len(avg), 3) if avg else None,
        }

    return {
        "lookback_days": lookback_days,
        "daily": daily,
        "by_shard_count": aggregates,
    }


def _amenities_from_properties(props: list[dict[str, Any]],
                               run_date: str) -> dict[str, Any]:
    total_props = len(props)
    props_with_amen = 0
    total_units = 0
    units_with_amen = 0
    sample: list[dict[str, Any]] = []
    pkeys: set[str] = set()
    ukeys: set[str] = set()
    for p in props:
        pkeys.update(p.keys())
        if _has_amenity_fields(p):
            props_with_amen += 1
            if len(sample) < 10:
                amen_value = next(
                    (p.get(k) for k in
                     ("amenities", "property_amenities", "community_amenities")
                     if p.get(k)),
                    None,
                )
                sample.append({"canonical_id": _canon_id(p), "value": amen_value})
        for u in p.get("units") or []:
            if not isinstance(u, dict):
                continue
            total_units += 1
            ukeys.update(u.keys())
            if _has_amenity_fields(u):
                units_with_amen += 1
    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0
    return {
        "as_of_run_date": run_date,
        "total_properties": total_props,
        "properties_with_amenities_count": props_with_amen,
        "properties_with_amenities_pct": pct(props_with_amen, total_props),
        "properties_without_amenities_count": total_props - props_with_amen,
        "total_units": total_units,
        "units_with_amenities_count": units_with_amen,
        "units_with_amenities_pct": pct(units_with_amen, total_units),
        "property_keys_observed": sorted(pkeys),
        "unit_keys_observed": sorted(ukeys),
        "samples_with_amenities": sample,
        "failure_summary": {
            "primary_cause": (
                "No extractor writes amenity strings into property/unit records."
                " Phase 6 amenities reader sums an absent field, so coverage is 0%"
                " by construction even when amenity pages are crawled."
            ) if props_with_amen == 0 else None,
            "missing_property_field": "amenities" not in pkeys,
            "missing_unit_field": "amenities" not in ukeys,
        },
    }


def _concessions_from_properties(props: list[dict[str, Any]],
                                 run_date: str) -> dict[str, Any]:
    total_props = len(props)
    props_with = 0
    total_units = 0
    units_with = 0
    schema_p = 0
    schema_u = 0
    sample: list[dict[str, Any]] = []
    for p in props:
        if "concessions" in p:
            schema_p += 1
        if _has_concession_content(p.get("concessions")):
            props_with += 1
            if len(sample) < 10:
                sample.append({"canonical_id": _canon_id(p), "value": p.get("concessions")})
        for u in p.get("units") or []:
            if not isinstance(u, dict):
                continue
            total_units += 1
            if "concessions" in u:
                schema_u += 1
            if _has_concession_content(u.get("concessions")):
                units_with += 1
    pct = lambda n, d: round(100.0 * n / d, 2) if d else 0.0
    return {
        "as_of_run_date": run_date,
        "snapshot_total_properties": total_props,
        "snapshot_properties_with_concessions_count": props_with,
        "snapshot_properties_with_concessions_pct": pct(props_with, total_props),
        "snapshot_total_units": total_units,
        "snapshot_units_with_concessions_count": units_with,
        "snapshot_units_with_concessions_pct": pct(units_with, total_units),
        "schema_field_present_property_pct": pct(schema_p, total_props),
        "schema_field_present_unit_pct": pct(schema_u, total_units),
        "db_properties_total": None,
        "db_properties_with_concessions": None,
        "db_units_total": None,
        "db_units_with_concessions": None,
        "samples_with_concessions": sample,
        "failure_summary": {
            "primary_cause": (
                "Property records carry the `concessions` key but it is never"
                " populated. Detector flags concession signals (e.g."
                " specialsbanner.js, specials anchors) but no downstream"
                " extractor reads the rendered banner."
            ) if props_with == 0 else None,
            "schema_present_but_unpopulated": (
                schema_p == total_props and props_with == 0 and total_props > 0
            ),
        },
    }


def _quantity_from_properties(props: list[dict[str, Any]],
                              run_date: str) -> dict[str, Any]:
    counts: list[int] = []
    counts_success: list[int] = []
    counts_failed: list[int] = []
    by_bucket = {"0": 0, "1": 0, "2-5": 0, "6-20": 0, "21-100": 0, "100+": 0}
    zero_unit: list[dict[str, str]] = []
    top: list[tuple[str, int]] = []
    for p in props:
        cid = _canon_id(p)
        units = p.get("units") or []
        n = len(units) if isinstance(units, list) else 0
        counts.append(n)
        v = _verdict(p)
        if v == "SUCCESS":
            counts_success.append(n)
        else:
            counts_failed.append(n)
        if n == 0:
            by_bucket["0"] += 1
            if len(zero_unit) < 25:
                zero_unit.append({"canonical_id": cid, "verdict": v})
        elif n == 1:
            by_bucket["1"] += 1
        elif n <= 5:
            by_bucket["2-5"] += 1
        elif n <= 20:
            by_bucket["6-20"] += 1
        elif n <= 100:
            by_bucket["21-100"] += 1
        else:
            by_bucket["100+"] += 1
        top.append((cid, n))
    top.sort(key=lambda r: -r[1])

    def _stats(values: list[int]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        s = sorted(values)
        def pctl(p: float) -> float:
            i = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
            return s[i]
        return {
            "n": len(s), "min": s[0], "max": s[-1],
            "mean": round(sum(s) / len(s), 2),
            "median": s[len(s) // 2],
            "p25": pctl(25), "p75": pctl(75), "p95": pctl(95), "p99": pctl(99),
        }

    return {
        "as_of_run_date": run_date,
        "interpretation": (
            "'Quantity' here is the per-property unit count captured for the run "
            "(length of the snapshot's `units` array). 0 = full extraction "
            "failure even if the verdict is SUCCESS at the URL level."
        ),
        "total_properties": len(counts),
        "total_units_captured": sum(counts),
        "distribution_buckets": by_bucket,
        "stats_overall": _stats(counts),
        "stats_success_verdict": _stats(counts_success),
        "stats_failed_verdict": _stats(counts_failed),
        "zero_unit_property_count": by_bucket["0"],
        "zero_unit_sample": zero_unit,
        "top10_by_unit_count": [
            {"canonical_id": cid, "unit_count": n} for cid, n in top[:10]
        ],
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _md(report: HealthReport) -> str:
    """Render the report as Markdown."""
    L: list[str] = []
    L.append(f"# Operational Health Report — {report.run_date}")
    L.append("")
    L.append(f"_Generated: {report.generated_at}_  ")
    L.append(f"_Source: {report.source_label}_")
    L.append("")

    # 1. Success / failure window
    L.append(f"## 1. Property Success / Failure — last {report.success_window_days} days")
    L.append("")
    L.append("| Run date   | Properties | Succeeded | Failed | Success % | Failure % |")
    L.append("|------------|-----------:|----------:|-------:|----------:|----------:|")
    for row in report.success_history:
        L.append(
            f"| {row['run_date']} | {row['properties']:>10,} | {row['succeeded']:>9,} | "
            f"{row['failed']:>6,} | {row['success_pct']:>9.2f} | {row['failure_pct']:>9.2f} |"
        )
    L.append("")

    # 2. Unit data quality
    uq = report.unit_quality
    L.append(f"## 2. Unit Data Quality — as of {uq.get('as_of_run_date')}")
    L.append("")
    L.append(f"- **Total units:** {uq.get('total_units', 0):,}")
    L.append(
        f"- **With rent:** {uq.get('with_rent_count', 0):,} "
        f"({uq.get('with_rent_pct', 0):.2f}%)"
    )
    L.append(
        f"- **With availability date:** {uq.get('with_availability_count', 0):,} "
        f"({uq.get('with_availability_pct', 0):.2f}%)"
    )
    L.append("")
    L.append("**Cross-day consistency** (same `(canonical_id, unit_id)` present on each day):")
    L.append("")
    win = uq.get("consistency_window_dates", [])
    counts = uq.get("consistency_day_counts", {})
    for d in win:
        L.append(f"  - `{d}`: {counts.get(d, 0):,} units in roster")
    L.append("")
    L.append(
        f"- **Seen consistently last 2 days:** {uq.get('seen_consistently_2d_count', 0):,} "
        f"({uq.get('seen_consistently_2d_pct', 0):.2f}% of today's units)"
    )
    L.append(
        f"- **Seen consistently last 3 days:** {uq.get('seen_consistently_3d_count', 0):,} "
        f"({uq.get('seen_consistently_3d_pct', 0):.2f}% of today's units)"
    )
    L.append("")

    # 3. Diff vs yesterday
    diff = report.diff_yesterday
    L.append(f"## 3. Diffs vs Yesterday ({diff.get('yesterday')} → {diff.get('today')})")
    L.append("")
    L.append("| Metric | Count |")
    L.append("|---|---:|")
    L.append(f"| Properties in today's run | {diff.get('today_property_count', 0):,} |")
    L.append(f"| Properties in yesterday's run | {diff.get('yesterday_property_count', 0):,} |")
    L.append(f"| **New properties (today \\ yesterday)** | {diff.get('new_properties_count', 0):,} |")
    L.append(f"| **Missed today (yesterday \\ today)** | {diff.get('missed_today_count', 0):,} |")
    L.append(f"| Failed in today's run (status ≠ SUCCESS) | {diff.get('failed_today_count', 0):,} |")
    L.append(f"| Properties with rent comparable to yesterday | {diff.get('compared_property_count', 0):,} |")
    L.append(f"| Rent up | {diff.get('rent_up_count', 0):,} |")
    L.append(f"| Rent down | {diff.get('rent_down_count', 0):,} |")
    L.append(f"| Rent unchanged | {diff.get('rent_unchanged_count', 0):,} |")
    L.append("")

    if diff.get("rent_up_top10"):
        L.append("**Top 10 rent increases (median rent per property):**")
        L.append("")
        L.append("| canonical_id | yesterday | today | Δ |")
        L.append("|---|---:|---:|---:|")
        for r in diff["rent_up_top10"]:
            L.append(f"| {r['canonical_id']} | {r['yesterday']:.2f} | {r['today']:.2f} | +{r['delta']:.2f} |")
        L.append("")

    if diff.get("rent_down_top10"):
        L.append("**Top 10 rent decreases (median rent per property):**")
        L.append("")
        L.append("| canonical_id | yesterday | today | Δ |")
        L.append("|---|---:|---:|---:|")
        for r in diff["rent_down_top10"]:
            L.append(f"| {r['canonical_id']} | {r['yesterday']:.2f} | {r['today']:.2f} | {r['delta']:.2f} |")
        L.append("")

    if diff.get("new_properties_sample"):
        L.append(
            f"_Sample of new properties (first {len(diff['new_properties_sample'])} of "
            f"{diff.get('new_properties_count', 0)})_: "
            + ", ".join(f"`{p}`" for p in diff["new_properties_sample"])
        )
        L.append("")

    if diff.get("missed_today_sample"):
        L.append(
            f"_Sample of missed properties (first {len(diff['missed_today_sample'])} of "
            f"{diff.get('missed_today_count', 0)})_: "
            + ", ".join(f"`{p}`" for p in diff["missed_today_sample"])
        )
        L.append("")

    # 4. System performance
    perf = report.performance
    L.append(f"## 4. System Performance — last {perf.get('lookback_days')} days")
    L.append("")
    L.append("**Per-day wall-clock runtime:**")
    L.append("")
    L.append("| Run date | Shards | Shard gen-at spread (min) | Scrape wall-clock (min) | Events | Avg sec / property |")
    L.append("|----------|------:|-------------------------:|------------------------:|-------:|-------------------:|")
    for d in perf.get("daily", []):
        L.append(
            f"| {d['run_date']} | {d['shard_count']} | "
            f"{d['shards_gen_at_spread_min']} | "
            f"{d.get('scrape_events_wallclock_min') if d.get('scrape_events_wallclock_min') is not None else '—'} | "
            f"{d.get('scrape_events_count') or '—'} | "
            f"{d.get('avg_seconds_per_property') if d.get('avg_seconds_per_property') is not None else '—'} |"
        )
    L.append("")
    L.append("**Aggregated by shard count:**")
    L.append("")
    L.append("| Shard count | Runs observed | Wall-clock avg (min) | Min | Max | Avg sec / property |")
    L.append("|------------:|--------------:|---------------------:|----:|----:|-------------------:|")
    for ns, agg in perf.get("by_shard_count", {}).items():
        L.append(
            f"| {ns} | {agg['runs_observed']} | "
            f"{agg.get('wallclock_min_avg') if agg.get('wallclock_min_avg') is not None else '—'} | "
            f"{agg.get('wallclock_min_min') if agg.get('wallclock_min_min') is not None else '—'} | "
            f"{agg.get('wallclock_min_max') if agg.get('wallclock_min_max') is not None else '—'} | "
            f"{agg.get('avg_seconds_per_property') if agg.get('avg_seconds_per_property') is not None else '—'} |"
        )
    L.append("")
    shard_counts = list(perf.get("by_shard_count", {}).keys())
    if "10" in shard_counts and "20" not in shard_counts and not any(
        sc.isdigit() and int(sc) >= 19 for sc in shard_counts
    ):
        L.append(
            "_Note: no 20-shard runs visible in this source. All recent runs use 10 shards "
            "or fewer. 20-shard runs (or other shard counts) appear here automatically when they exist._"
        )
        L.append("")

    return "\n".join(L)


def _md_extraction_quality(report: ExtractionQualityReport) -> str:
    """Render the extraction-quality report as Markdown."""
    L: list[str] = []
    L.append(f"# Extraction Quality Report — {report.run_date}")
    L.append("")
    L.append(f"_Generated: {report.generated_at}_  ")
    L.append(f"_Source: {report.source_label}_")
    L.append("")
    L.append(
        "Covers three semi-extracted dimensions: **amenities**, **concessions**, "
        "and per-property **unit count** (treated here as the \"quantity\" field — "
        "the project schema has no literal `quantity` key)."
    )
    L.append("")

    # 1. Amenities
    a = report.amenities
    L.append("## 1. Amenities")
    L.append("")
    L.append("**Summary**")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|---|---:|")
    L.append(f"| Properties in run | {a.get('total_properties', 0):,} |")
    L.append(
        f"| Properties with amenities populated | "
        f"{a.get('properties_with_amenities_count', 0):,} "
        f"({a.get('properties_with_amenities_pct', 0):.2f}%) |"
    )
    L.append(
        f"| Properties without amenities | "
        f"{a.get('properties_without_amenities_count', 0):,} |"
    )
    L.append(f"| Total units in run | {a.get('total_units', 0):,} |")
    L.append(
        f"| Units with amenities populated | "
        f"{a.get('units_with_amenities_count', 0):,} "
        f"({a.get('units_with_amenities_pct', 0):.2f}%) |"
    )
    L.append("")
    L.append("**Failure breakdown**")
    L.append("")
    fs = a.get("failure_summary", {})
    if fs.get("primary_cause"):
        L.append(f"- Primary cause: {fs['primary_cause']}")
    L.append(
        f"- Property-level `amenities` key absent from emitted payloads: "
        f"{'yes' if fs.get('missing_property_field') else 'no'}"
    )
    L.append(
        f"- Unit-level `amenities` key absent from emitted payloads: "
        f"{'yes' if fs.get('missing_unit_field') else 'no'}"
    )
    L.append("")
    L.append("**Details — keys observed**")
    L.append("")
    L.append(
        "- Property keys: " +
        ", ".join(f"`{k}`" for k in a.get("property_keys_observed", []) or ["(none)"])
    )
    L.append(
        "- Unit keys: " +
        ", ".join(f"`{k}`" for k in a.get("unit_keys_observed", []) or ["(none)"])
    )
    L.append("")
    if a.get("samples_with_amenities"):
        L.append("**Sample of properties with non-empty amenities (up to 10)**")
        L.append("")
        for s in a["samples_with_amenities"]:
            preview = json.dumps(s.get("value"), default=str)[:200]
            L.append(f"- `{s['canonical_id']}` — {preview}")
        L.append("")
    else:
        L.append("_No properties had amenity content; nothing to sample._")
        L.append("")

    # 2. Concessions
    c = report.concessions
    L.append("## 2. Concessions")
    L.append("")
    L.append("**Summary (run snapshot)**")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|---|---:|")
    L.append(f"| Properties in run | {c.get('snapshot_total_properties', 0):,} |")
    L.append(
        f"| Properties with concessions populated | "
        f"{c.get('snapshot_properties_with_concessions_count', 0):,} "
        f"({c.get('snapshot_properties_with_concessions_pct', 0):.2f}%) |"
    )
    L.append(
        f"| Schema field present on property records | "
        f"{c.get('schema_field_present_property_pct', 0):.2f}% |"
    )
    L.append(f"| Total units in run | {c.get('snapshot_total_units', 0):,} |")
    L.append(
        f"| Units with concessions populated | "
        f"{c.get('snapshot_units_with_concessions_count', 0):,} "
        f"({c.get('snapshot_units_with_concessions_pct', 0):.2f}%) |"
    )
    L.append("")
    if c.get("db_properties_total") is not None:
        L.append("**Summary (current DB state — `properties` / `units` tables)**")
        L.append("")
        L.append("| Table | Rows | With non-empty concessions |")
        L.append("|---|---:|---:|")
        L.append(
            f"| `properties` | {c.get('db_properties_total', 0):,} | "
            f"{c.get('db_properties_with_concessions', 0):,} |"
        )
        L.append(
            f"| `units`      | {c.get('db_units_total', 0):,} | "
            f"{c.get('db_units_with_concessions', 0):,} |"
        )
        L.append("")
    L.append("**Failure breakdown**")
    L.append("")
    fs = c.get("failure_summary", {})
    if fs.get("primary_cause"):
        L.append(f"- Primary cause: {fs['primary_cause']}")
    if fs.get("schema_present_but_unpopulated"):
        L.append(
            "- Schema-vs-content gap: every property record carries the "
            "`concessions` key but the value is `null` / empty across the run."
        )
    L.append("")
    if c.get("samples_with_concessions"):
        L.append("**Sample of properties with non-empty concessions (up to 10)**")
        L.append("")
        for s in c["samples_with_concessions"]:
            preview = json.dumps(s.get("value"), default=str)[:200]
            L.append(f"- `{s['canonical_id']}` — {preview}")
        L.append("")
    else:
        L.append("_No properties had concession content; nothing to sample._")
        L.append("")

    # 3. Quantity (unit-count-per-property)
    q = report.quantity
    L.append("## 3. Quantity (unit count per property)")
    L.append("")
    L.append(f"_{q.get('interpretation', '')}_")
    L.append("")
    L.append("**Summary**")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|---|---:|")
    L.append(f"| Properties in run | {q.get('total_properties', 0):,} |")
    L.append(f"| Total units captured (sum) | {q.get('total_units_captured', 0):,} |")
    L.append(f"| Properties with 0 units | {q.get('zero_unit_property_count', 0):,} |")
    L.append("")
    L.append("**Distribution buckets**")
    L.append("")
    L.append("| Units / property | Count |")
    L.append("|---|---:|")
    for bucket, count in q.get("distribution_buckets", {}).items():
        L.append(f"| {bucket} | {count:,} |")
    L.append("")
    L.append("**Statistics**")
    L.append("")
    L.append("| Cohort | n | min | p25 | median | mean | p75 | p95 | p99 | max |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, key in [
        ("All properties", "stats_overall"),
        ("Verdict = SUCCESS", "stats_success_verdict"),
        ("Verdict ≠ SUCCESS", "stats_failed_verdict"),
    ]:
        s = q.get(key, {})
        if s.get("n", 0) == 0:
            L.append(f"| {label} | 0 | — | — | — | — | — | — | — | — |")
            continue
        L.append(
            f"| {label} | {s['n']:,} | {s['min']} | {s['p25']} | {s['median']} | "
            f"{s['mean']} | {s['p75']} | {s['p95']} | {s['p99']} | {s['max']} |"
        )
    L.append("")
    if q.get("top10_by_unit_count"):
        L.append("**Top 10 properties by unit count**")
        L.append("")
        L.append("| canonical_id | units |")
        L.append("|---|---:|")
        for r in q["top10_by_unit_count"]:
            L.append(f"| {r['canonical_id']} | {r['unit_count']:,} |")
        L.append("")
    if q.get("zero_unit_sample"):
        L.append(
            f"**Sample of zero-unit properties (first {len(q['zero_unit_sample'])} of "
            f"{q.get('zero_unit_property_count', 0)})**"
        )
        L.append("")
        L.append("| canonical_id | verdict |")
        L.append("|---|---|")
        for r in q["zero_unit_sample"]:
            L.append(f"| {r['canonical_id']} | {r['verdict']} |")
        L.append("")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_report(run_date: str, success_window_days: int = 5) -> HealthReport:
    """Compute the operational health report. Pure data — no I/O beyond the DB."""
    conn = _connect()
    try:
        cur = conn.cursor()
        return HealthReport(
            run_date=run_date,
            generated_at=datetime.now(timezone.utc).isoformat(),
            success_window_days=success_window_days,
            success_history=_collect_success_history(cur, run_date, success_window_days),
            unit_quality=_collect_unit_quality(cur, run_date),
            diff_yesterday=_collect_diff_yesterday(cur, run_date),
            performance=_collect_performance(cur, run_date, lookback_days=success_window_days),
        )
    finally:
        conn.close()


def build_extraction_quality_report(run_date: str) -> ExtractionQualityReport:
    """Compute the amenities/concessions/quantity report."""
    conn = _connect()
    try:
        cur = conn.cursor()
        return ExtractionQualityReport(
            run_date=run_date,
            generated_at=datetime.now(timezone.utc).isoformat(),
            amenities=_collect_amenities(cur, run_date),
            concessions=_collect_concessions(cur, run_date),
            quantity=_collect_quantity(cur, run_date),
        )
    finally:
        conn.close()


def build_report_gcs(run_date: str, success_window_days: int = 5,
                     bucket: str = DEFAULT_GCS_BUCKET,
                     prefix: str = DEFAULT_GCS_PREFIX,
                     scan_events: bool = True) -> HealthReport:
    """Compute the operational health report from cloud-storage shard outputs."""
    client = _gcs_client()
    today_run = _gcs_load_run(client, bucket, prefix, run_date)
    today_props = _flatten_properties(today_run)
    yest = (date.fromisoformat(run_date) - timedelta(days=1)).isoformat()
    day_before = (date.fromisoformat(run_date) - timedelta(days=2)).isoformat()
    yest_run = _gcs_load_run(client, bucket, prefix, yest)
    yest_props = _flatten_properties(yest_run)
    db_run = _gcs_load_run(client, bucket, prefix, day_before)
    db_props = _flatten_properties(db_run)

    return HealthReport(
        run_date=run_date,
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_label=f"GCS gs://{bucket}/{prefix}/",
        success_window_days=success_window_days,
        success_history=_gcs_success_history(client, bucket, prefix, run_date, success_window_days),
        unit_quality=_unit_quality_from_properties(today_props, yest_props, db_props, run_date),
        diff_yesterday=_diff_from_properties(today_props, yest_props, run_date, yest),
        performance=_gcs_performance(client, bucket, prefix, run_date,
                                     lookback_days=success_window_days,
                                     scan_events=scan_events),
    )


def build_extraction_quality_report_gcs(run_date: str,
                                        bucket: str = DEFAULT_GCS_BUCKET,
                                        prefix: str = DEFAULT_GCS_PREFIX) -> ExtractionQualityReport:
    """Compute amenities/concessions/quantity from cloud-storage shard outputs."""
    client = _gcs_client()
    run = _gcs_load_run(client, bucket, prefix, run_date)
    props = _flatten_properties(run)
    return ExtractionQualityReport(
        run_date=run_date,
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_label=f"GCS gs://{bucket}/{prefix}/",
        amenities=_amenities_from_properties(props, run_date),
        concessions=_concessions_from_properties(props, run_date),
        quantity=_quantity_from_properties(props, run_date),
    )


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


def _write_pair(out_path: Path, md: str, json_payload: dict[str, Any] | None,
                emit_json: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"[health_report] wrote {out_path}", file=sys.stderr)
    if emit_json and json_payload is not None:
        j = out_path.with_suffix(".json")
        j.write_text(
            json.dumps(json_payload, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[health_report] wrote {j}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", choices=("health", "extraction-quality", "all"),
                    default="health", help="Which report to produce (default: health)")
    ap.add_argument("--source", choices=("db", "gcs"), default="db",
                    help="Where to read run data from (default: db = local Postgres)")
    ap.add_argument("--bucket", default=DEFAULT_GCS_BUCKET,
                    help=f"GCS bucket (default: {DEFAULT_GCS_BUCKET})")
    ap.add_argument("--prefix", default=DEFAULT_GCS_PREFIX,
                    help=f"Object prefix under the bucket (default: {DEFAULT_GCS_PREFIX})")
    ap.add_argument("--no-scan-events", action="store_true",
                    help="GCS only: skip events.jsonl scan for wall-clock runtime")
    ap.add_argument("--run-date", default=date.today().isoformat(),
                    help="Run date in YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, default=5,
                    help="Window length for the health report's success/perf history (default: 5)")
    ap.add_argument("--out", default=None,
                    help="Markdown output path. Default: data/runs/{run_date}/{report}.md. "
                         "Use '-' for stdout-only. Ignored when --report=all.")
    ap.add_argument("--json", action="store_true",
                    help="Also write a sibling .json file with the raw data")
    ap.add_argument("--no-email", action="store_true",
                    help="Skip the email send. Markdown/JSON artifacts are still written.")
    ap.add_argument("--email-recipients", default=None,
                    help="Comma-separated override of REPORT_RECIPIENTS.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render the email body to stdout but do not actually send.")
    args = ap.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent.parent
    out_dir = repo_root / "data" / "runs" / args.run_date
    suffix = "" if args.source == "db" else "_gcs"

    def _resolve_out(default_name: str) -> Path | None:
        name = default_name.replace(".md", f"{suffix}.md")
        if args.report == "all":
            return out_dir / name
        if args.out == "-":
            return None
        if args.out:
            return Path(args.out)
        return out_dir / name

    rendered: list[str] = []
    # (subject, summary, body_md, attachments) tuples — one per report
    # generated. ``--report=all`` produces two entries; one email per
    # report keeps each subject line tight and lets the recipient
    # filter on it.
    email_payloads: list[tuple[str, str, str, list[Path]]] = []

    if args.report in ("health", "all"):
        if args.source == "gcs":
            rep = build_report_gcs(args.run_date, success_window_days=args.days,
                                   bucket=args.bucket, prefix=args.prefix,
                                   scan_events=not args.no_scan_events)
        else:
            rep = build_report(args.run_date, success_window_days=args.days)
        md = _md(rep)
        rendered.append(md)
        out_path = _resolve_out("health_report.md")
        attachments: list[Path] = []
        if out_path is not None:
            _write_pair(out_path, md, {
                "run_date": rep.run_date,
                "generated_at": rep.generated_at,
                "source": args.source,
                "success_window_days": rep.success_window_days,
                "success_history": rep.success_history,
                "unit_quality": rep.unit_quality,
                "diff_yesterday": rep.diff_yesterday,
                "performance": rep.performance,
            }, args.json)
            attachments.append(out_path)
            if args.json:
                attachments.append(out_path.with_suffix(".json"))
        email_payloads.append((
            f"PropAi Health Report — {rep.run_date}",
            _build_health_summary(rep),
            md,
            attachments,
        ))

    if args.report in ("extraction-quality", "all"):
        if args.source == "gcs":
            eq = build_extraction_quality_report_gcs(args.run_date,
                                                     bucket=args.bucket,
                                                     prefix=args.prefix)
        else:
            eq = build_extraction_quality_report(args.run_date)
        md = _md_extraction_quality(eq)
        rendered.append(md)
        out_path = _resolve_out("extraction_quality_report.md")
        attachments = []
        if out_path is not None:
            _write_pair(out_path, md, {
                "run_date": eq.run_date,
                "generated_at": eq.generated_at,
                "source": args.source,
                "amenities": eq.amenities,
                "concessions": eq.concessions,
                "quantity": eq.quantity,
            }, args.json)
            attachments.append(out_path)
            if args.json:
                attachments.append(out_path.with_suffix(".json"))
        email_payloads.append((
            f"PropAi Extraction Quality Report — {eq.run_date}",
            _build_extraction_quality_summary(eq),
            md,
            attachments,
        ))

    for md in rendered:
        _safe_print(md)
        if len(rendered) > 1:
            _safe_print("\n\n---\n\n")

    if args.no_email:
        return 0

    from scripts.email.report import email_report

    recipients = (
        [r.strip() for r in args.email_recipients.split(",") if r.strip()]
        if args.email_recipients
        else None
    )
    rc = 0
    for subject, summary, body_md, attachments in email_payloads:
        result = email_report(
            subject=subject,
            summary=summary,
            body_md=body_md,
            attachments=attachments or None,
            recipients=recipients,
            dry_run=args.dry_run,
        )
        if result.get("isError"):
            print(f"[health_report] email send failed for {subject!r}: "
                  f"{result.get('content')}", file=sys.stderr)
            rc = 1
    return rc


def _build_health_summary(rep: "HealthReport") -> str:
    """One-line TL;DR for the health email — anchored on the most recent
    day's success rate from the rolling window."""
    history = rep.success_history or []
    if not history:
        return f"Health report for {rep.run_date} — no success history rows."
    latest = history[-1]
    parts = [
        f"{latest.get('date', rep.run_date)}: "
        f"{latest.get('success', 0)}/{latest.get('total', 0)} succeeded "
        f"({latest.get('success_rate', 0.0):.1f}%)"
    ]
    failed = latest.get("failed", 0)
    if failed:
        parts.append(f"{failed} failed")
    uq = rep.unit_quality or {}
    if uq.get("total_units"):
        parts.append(
            f"{uq['total_units']} units (rent {uq.get('pct_with_rent', 0):.0f}%, "
            f"availability {uq.get('pct_with_availability', 0):.0f}%)"
        )
    diff = rep.diff_yesterday or {}
    if diff.get("rent_increases") or diff.get("rent_decreases"):
        parts.append(
            f"{diff.get('rent_increases', 0)}↑/{diff.get('rent_decreases', 0)}↓ rents vs yesterday"
        )
    return ". ".join(parts) + "."


def _build_extraction_quality_summary(eq: "ExtractionQualityReport") -> str:
    """One-line TL;DR for the extraction-quality email — coverage rates
    for the three semi-extracted fields (amenities, concessions, units)."""
    parts = [f"Run {eq.run_date}"]
    am = eq.amenities or {}
    co = eq.concessions or {}
    qu = eq.quantity or {}
    if am:
        parts.append(f"amenities: {am.get('coverage_pct', 0):.0f}% coverage")
    if co:
        parts.append(f"concessions: {co.get('coverage_pct', 0):.0f}%")
    if qu:
        parts.append(f"unit-count: {qu.get('coverage_pct', 0):.0f}%")
    return ". ".join(parts) + "."


if __name__ == "__main__":
    raise SystemExit(main())
