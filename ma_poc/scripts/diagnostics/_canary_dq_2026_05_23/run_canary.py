"""Quick DQ canary — replays cloud-run 2026-05-22 outputs through the
shipped + proposed changes from the 2026-05-23 DOM-quality investigation.

Pure post-process replay (no scraping). Compares:
  * What today's cloud run shipped (baseline)
  * What the shipped fixes would have produced (Pass 5, Pass 1 sibling
    guard, .floorplan-slide DOM extractor) — applied to live-fetched HTML
    where available.
  * What the proposed gates would change (T1.A available_date predicate,
    T1.C same-rent guard, T1.D WAITLIST canonicalisation, T1.E unit_id
    equals fpn fix).

Outputs per-PID before/after table to stdout + a JSON summary.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

RUN_DIR = Path("c:/tmp/run-2026-05-22")
LIVE_SAMPLES = Path("c:/tmp/llm_dom_samples")
OUT_DIR = Path(__file__).resolve().parent

# ── PID basket: random sample across defect categories + sentinels ──────────

# Category A: deposit-leak (rent < $500)
DEPOSIT_LEAK_PIDS = ["226980", "11762", "232870", "229736", "232788"]
# Category B: all-same-rent (some legit, some leak)
SAME_RENT_PIDS = ["11727", "232870", "22187", "266792", "282648"]  # 282648 IS legit
# Category C: junk available_date
JUNK_AVAIL_DATE_PIDS = ["10496", "19535", "1973", "11797", "12318", "20943"]
# Category D: WAITLIST status
WAITLIST_PIDS = ["220976"]  # contains "Sign Waitlist" in available_date
# Category E: unit_id == floor_plan_name
UID_EQ_FPN_PIDS = ["229986", "254187"]
# Category F: long junk fpn
LONG_FPN_PIDS = []  # collected dynamically below
# Sentinels — should KEEP working unchanged
SENTINEL_PIDS = [
    "10629",   # livebh (Pass 5 + .floorplan-slide cohort)
    "12133",   # livebh
    "15541",   # livebh
    "282648",  # apolloridge — uniform rent with REAL unit_ids — must NOT fire same-rent guard
    "20736",   # weidner.com — 38 units extracted by LLM_DOM, real per-unit data
    "227054",  # random success
]

ALL_PIDS = sorted(set(
    DEPOSIT_LEAK_PIDS + SAME_RENT_PIDS + JUNK_AVAIL_DATE_PIDS
    + WAITLIST_PIDS + UID_EQ_FPN_PIDS + SENTINEL_PIDS
))


def find_shard(pid: str) -> str | None:
    for s in sorted(os.listdir(RUN_DIR)):
        if not s.startswith("shard_"):
            continue
        pj = RUN_DIR / s / "properties.json"
        if not pj.exists():
            continue
        try:
            data = json.load(open(pj, encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for p in data:
            if str(p.get("apartment_id") or "") == pid:
                return s
    return None


def load_property(pid: str, shard: str) -> dict[str, Any] | None:
    pj = RUN_DIR / shard / "properties.json"
    data = json.load(open(pj, encoding="utf-8", errors="ignore"))
    for p in data:
        if str(p.get("apartment_id") or "") == pid:
            return p
    return None


# ── Proposed gates (T1 from the playbook) ───────────────────────────────────


_MONTH_NAME_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.IGNORECASE,
)
_DATE_NUMERIC_RE = re.compile(r"\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,4})?\b")
_SEASON_RE = re.compile(r"\b(?:spring|summer|fall|autumn|winter)\b", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"\b(?:early|mid|late|end[ -]of[ -](?:the[ -])?(?:month|year|week)|"
    r"this[ -](?:week|month|weekend)|next[ -](?:week|month))\b",
    re.IGNORECASE,
)
_NOW_RE = re.compile(
    r"\b(?:now|today|asap|immediate|immediately|soon|currently|"
    r"available|ready)\b",
    re.IGNORECASE,
)


def looks_date_like(s: str) -> bool:
    """Proposed predicate for T1.A: returns True only if the raw string has
    a plausibly date-shaped token."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    if _MONTH_NAME_RE.search(s):
        return True
    if _DATE_NUMERIC_RE.search(s):
        return True
    if _SEASON_RE.search(s):
        return True
    if _RELATIVE_RE.search(s):
        return True
    # Require NOW context to be in a "available now" / "ready now" shape,
    # not a bare word. Otherwise "Date: Available" matches "available" alone.
    if _NOW_RE.search(s):
        # extra check — must be at least 4 chars and contain something
        # besides just the token (to reject "Available" alone, which is
        # ambiguous on the page).
        toks = s.split()
        if len(toks) >= 2:
            return True
    return False


def t1_a_date_gate(unit: dict[str, Any]) -> tuple[Any, str]:
    """Apply T1.A: gate the raw fallback by looks_date_like(). Return
    (new_available_date, reason)."""
    from ma_poc.extraction.dates import format_loose_date

    raw = unit.get("available_date_raw") or unit.get("available_date") or unit.get("_date_placeholder")
    if not raw:
        return None, "no_raw"
    parsed = format_loose_date(raw)
    if parsed:
        return parsed, "parsed_ok"
    # Parser couldn't parse — check shape gate.
    if looks_date_like(raw):
        return raw[:32], "raw_fallback_shape_ok"
    return None, "raw_rejected_not_date_shape"


def t1_c_same_rent_guard(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply T1.C: when ≥3 rows share rent AND no real unit_ids AND rent < $1000,
    flag for plan_summaries demotion. Returns {"trigger": bool, "demote": [idx]}."""
    if len(units) < 3:
        return {"trigger": False, "demote": []}
    rents = []
    for u in units:
        try:
            r = u.get("rent_low")
            if r is not None:
                rents.append(float(r))
            else:
                rents.append(None)
        except (ValueError, TypeError):
            rents.append(None)
    # Find the most common rent value
    from collections import Counter
    rc = Counter(r for r in rents if r is not None)
    if not rc:
        return {"trigger": False, "demote": []}
    most_common_rent, count = rc.most_common(1)[0]
    if count < 3:
        return {"trigger": False, "demote": []}
    if most_common_rent >= 1000:
        # Plausible market rent — do not fire
        return {"trigger": False, "demote": [], "reason": "rent_above_floor"}
    # Check unit_id presence on the same-rent rows
    demote = []
    real_uid_count = 0
    for i, (u, r) in enumerate(zip(units, rents)):
        if r != most_common_rent:
            continue
        uid = u.get("unit_id")
        if uid and not str(uid).startswith("inferred_"):
            real_uid_count += 1
            continue
        demote.append(i)
    if real_uid_count > 0:
        # At least one row has a real unit_id — likely a legitimate uniform
        # pricing case. Skip the gate.
        return {"trigger": False, "demote": [], "reason": "has_real_unit_ids"}
    return {"trigger": True, "demote": demote, "rent_value": most_common_rent}


def t1_d_status_canon(status: Any) -> tuple[str | None, str | None]:
    """Apply T1.D: canonicalize availability_status. Returns (canonical_status, subtype)."""
    if status is None:
        return None, None
    s = str(status).strip().upper().replace(" ", "_").replace("-", "_")
    canonical_map = {
        "AVAILABLE": ("AVAILABLE", None),
        "UNAVAILABLE": ("UNAVAILABLE", None),
        "UNKNOWN": ("UNKNOWN", None),
        # Subtype-bearing
        "WAITLIST": ("UNAVAILABLE", "WAITLIST"),
        "WAIT_LIST": ("UNAVAILABLE", "WAITLIST"),
        "COMING_SOON": ("UNAVAILABLE", "COMING_SOON"),
        "COMING": ("UNAVAILABLE", "COMING_SOON"),
        "RESERVED": ("UNAVAILABLE", "RESERVED"),
        "LEASED": ("UNAVAILABLE", "LEASED"),
        "RENTED": ("UNAVAILABLE", "LEASED"),
        "OCCUPIED": ("UNAVAILABLE", "OCCUPIED"),
        "MODEL_UNIT": ("UNAVAILABLE", "MODEL"),
        "MODEL": ("UNAVAILABLE", "MODEL"),
        "PENDING": ("UNAVAILABLE", "PENDING"),
        "ON_HOLD": ("UNAVAILABLE", "ON_HOLD"),
        "MAINTENANCE": ("UNAVAILABLE", "MAINTENANCE"),
        "OFF_MARKET": ("UNAVAILABLE", "OFF_MARKET"),
    }
    if s in canonical_map:
        return canonical_map[s]
    return status, None  # unknown — pass through unchanged


def t1_e_uid_equals_fpn(unit: dict[str, Any]) -> bool:
    """Apply T1.E: returns True if unit_id case-insensitively equals fpn."""
    uid = unit.get("unit_id")
    fpn = unit.get("floor_plan_name")
    if not uid or not fpn:
        return False
    return str(uid).strip().lower() == str(fpn).strip().lower()


# ── Pass 5 / .floorplan-slide live test ─────────────────────────────────────


def replay_jsonld_pass5(pid: str) -> dict[str, Any] | None:
    """Run extract_jsonld_from_html against the live-fetched HTML if available."""
    from ma_poc.pms.adapters._html_extract import extract_jsonld_from_html

    # Map of PID to live-fetch filename (in LIVE_SAMPLES)
    pid_to_file = {
        "10629": "livebh-1.html",
        "12133": "livebh-2.html",
    }
    fn = pid_to_file.get(pid)
    if not fn or not (LIVE_SAMPLES / fn).exists():
        return None
    html = (LIVE_SAMPLES / fn).read_text(encoding="utf-8", errors="replace")
    units = extract_jsonld_from_html(html, f"https://livebh.com/{fn}")
    return {"n_units": len(units), "units": units}


def replay_floorplan_slide(pid: str) -> dict[str, Any] | None:
    """Run extract_units_from_dom against live HTML — verifies the
    .floorplan-slide extractor fires."""
    from ma_poc.pms.adapters._html_extract import extract_units_from_dom

    pid_to_file = {
        "10629": "livebh-1.html",
        "12133": "livebh-2.html",
    }
    fn = pid_to_file.get(pid)
    if not fn or not (LIVE_SAMPLES / fn).exists():
        return None
    html = (LIVE_SAMPLES / fn).read_text(encoding="utf-8", errors="replace")
    units, mode = extract_units_from_dom(html, f"https://livebh.com/{fn}")
    return {"n_units": len(units), "mode": mode}


# ── Per-PID report ──────────────────────────────────────────────────────────


def process_pid(pid: str) -> dict[str, Any]:
    shard = find_shard(pid)
    if shard is None:
        return {"pid": pid, "error": "not_found_in_run"}
    p = load_property(pid, shard)
    if p is None:
        return {"pid": pid, "error": "property_load_failed"}
    url = p.get("website", "")
    units = p.get("units") or []
    floor_plans = p.get("floor_plans") or []
    tier = (p.get("_extract_result") or {}).get("tier_used", "?")

    # T1.A: simulate the date-gate on each unit row
    date_changes = []
    for i, u in enumerate(units):
        raw_ad = u.get("available_date_raw") or u.get("available_date")
        shipped = u.get("available_date")
        proposed, reason = t1_a_date_gate(u)
        if shipped != proposed:
            date_changes.append({
                "idx": i,
                "fpn": u.get("floor_plan_name"),
                "raw": raw_ad,
                "shipped": shipped,
                "proposed": proposed,
                "reason": reason,
            })

    # T1.C: same-rent guard
    sr_decision = t1_c_same_rent_guard(units)

    # T1.D: status canonicalisation
    status_changes = []
    for i, u in enumerate(units):
        st = u.get("availability_status")
        canon, sub = t1_d_status_canon(st)
        if canon != st:
            status_changes.append({"idx": i, "shipped": st, "proposed_status": canon, "proposed_subtype": sub})

    # T1.E: uid==fpn
    uid_changes = []
    for i, u in enumerate(units):
        if t1_e_uid_equals_fpn(u):
            uid_changes.append({"idx": i, "uid": u.get("unit_id"), "fpn": u.get("floor_plan_name")})

    # Shipped checks (Pass 5 + .floorplan-slide) on PIDs where I have live HTML
    pass5 = replay_jsonld_pass5(pid)
    fp_slide = replay_floorplan_slide(pid)

    return {
        "pid": pid,
        "url": url,
        "shipped_tier": tier,
        "shipped_n_units": len(units),
        "shipped_n_floor_plans": len(floor_plans),
        "t1a_date_changes": date_changes,
        "t1c_same_rent": sr_decision,
        "t1d_status_changes": status_changes,
        "t1e_uid_equals_fpn": uid_changes,
        "pass5_jsonld_result": pass5,
        "fp_slide_result": fp_slide,
    }


def category_of(pid: str) -> str:
    cats = []
    if pid in DEPOSIT_LEAK_PIDS: cats.append("A")
    if pid in SAME_RENT_PIDS: cats.append("B")
    if pid in JUNK_AVAIL_DATE_PIDS: cats.append("C")
    if pid in WAITLIST_PIDS: cats.append("D")
    if pid in UID_EQ_FPN_PIDS: cats.append("E")
    if pid in SENTINEL_PIDS: cats.append("SENT")
    return ",".join(cats) or "?"


def main() -> int:
    print(f"== DQ canary — replaying {len(ALL_PIDS)} PIDs from run 2026-05-22 ==\n")
    results = {}
    for pid in ALL_PIDS:
        r = process_pid(pid)
        r["category"] = category_of(pid)
        results[pid] = r
        cat = r["category"]
        url = r.get("url", "?")
        print(f"\n── PID {pid} ({cat}) — {url[:60]}")
        if r.get("error"):
            print(f"   ERROR: {r['error']}")
            continue
        print(f"   tier={r['shipped_tier']}  n_units={r['shipped_n_units']}  n_floor_plans={r['shipped_n_floor_plans']}")
        if r["t1a_date_changes"]:
            print(f"   T1.A (date-gate): {len(r['t1a_date_changes'])} row(s) would change")
            for c in r["t1a_date_changes"][:3]:
                print(f"      fpn={c['fpn']!r:25} raw={c['raw']!r:25} shipped={c['shipped']!r:25} → proposed={c['proposed']!r} ({c['reason']})")
        if r["t1c_same_rent"].get("trigger"):
            print(f"   T1.C (same-rent guard): TRIGGER — demote {len(r['t1c_same_rent']['demote'])} rows at ${r['t1c_same_rent']['rent_value']}")
        elif r["t1c_same_rent"].get("reason"):
            print(f"   T1.C (same-rent guard): no-fire — {r['t1c_same_rent']['reason']}")
        if r["t1d_status_changes"]:
            print(f"   T1.D (status canon): {len(r['t1d_status_changes'])} row(s) change")
            for c in r["t1d_status_changes"][:3]:
                print(f"      {c['shipped']} → {c['proposed_status']} (subtype={c['proposed_subtype']})")
        if r["t1e_uid_equals_fpn"]:
            print(f"   T1.E (uid==fpn): {len(r['t1e_uid_equals_fpn'])} row(s) flagged")
            for c in r["t1e_uid_equals_fpn"][:3]:
                print(f"      uid={c['uid']!r} == fpn={c['fpn']!r}")
        if r["pass5_jsonld_result"] is not None:
            print(f"   Pass5 JSON-LD: {r['pass5_jsonld_result']['n_units']} units (live HTML)")
        if r["fp_slide_result"] is not None:
            print(f"   .floorplan-slide DOM: {r['fp_slide_result']['n_units']} units (mode={r['fp_slide_result']['mode']})")

    # Summary
    print("\n\n══════════ SUMMARY ══════════")
    by_category = {}
    for pid, r in results.items():
        cat = r["category"]
        if cat not in by_category:
            by_category[cat] = {"count": 0, "t1a_hits": 0, "t1c_triggers": 0, "t1d_hits": 0, "t1e_hits": 0}
        by_category[cat]["count"] += 1
        by_category[cat]["t1a_hits"] += len(r.get("t1a_date_changes", []))
        if r.get("t1c_same_rent", {}).get("trigger"):
            by_category[cat]["t1c_triggers"] += 1
        by_category[cat]["t1d_hits"] += len(r.get("t1d_status_changes", []))
        by_category[cat]["t1e_hits"] += len(r.get("t1e_uid_equals_fpn", []))

    print(f"{'Cat':<8}{'PIDs':>5}{'T1A_row_changes':>18}{'T1C_triggers':>14}{'T1D_row_changes':>17}{'T1E_row_flags':>15}")
    for cat in sorted(by_category):
        st = by_category[cat]
        print(f"{cat:<8}{st['count']:>5}{st['t1a_hits']:>18}{st['t1c_triggers']:>14}{st['t1d_hits']:>17}{st['t1e_hits']:>15}")

    # Sentinel regression check
    print(f"\n── Sentinel regression check ──")
    for pid in SENTINEL_PIDS:
        r = results.get(pid, {})
        if r.get("error"):
            print(f"   PID {pid}: ERROR {r['error']}")
            continue
        n_changes = (
            len(r.get("t1a_date_changes", []))
            + (1 if r.get("t1c_same_rent", {}).get("trigger") else 0)
            + len(r.get("t1d_status_changes", []))
            + len(r.get("t1e_uid_equals_fpn", []))
        )
        status = "OK_NO_CHANGE" if n_changes == 0 else f"CHANGES_{n_changes}"
        print(f"   PID {pid} ({r.get('url','')[:50]}): {status}")

    # Write JSON output
    out_json = OUT_DIR / "canary_results.json"
    out_json.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nFull JSON written to {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
