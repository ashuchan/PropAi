"""measure_cohort.py <run-date-suffix> <label> — one-line conversion stats."""
import json, subprocess, sys
from collections import Counter

run = sys.argv[1]
label = sys.argv[2] if len(sys.argv) > 2 else run


def pull(rd):
    o = subprocess.run(
        ["bash", "-c", f"gsutil ls gs://jugnu-canary/runs/{rd}/ 2>/dev/null"],
        capture_output=True, text=True,
    ).stdout
    recs = []
    for sh in [l for l in o.split() if "shard_" in l]:
        try:
            d = json.loads(
                subprocess.run(
                    ["gsutil", "cat", sh + "properties.json"],
                    capture_output=True, timeout=90,
                ).stdout
            )
            recs += d if isinstance(d, list) else [d]
        except Exception:
            pass
    return recs


recs = pull(run)
tier = Counter()
t1 = 0
ul = 0
for x in recs:
    t = (x.get("_extract_result") or {}).get("tier_used") or "NONE"
    tier[t] += 1
    u = x.get("units") or []
    rr = [
        z for z in u
        if str(z.get("unit_id") or "")
        and not str(z.get("unit_id")).startswith("inferred_")
        and (z.get("rent_low") or z.get("rent_high"))
    ]
    if rr:
        ul += 1
        if t.startswith("TIER_1"):
            t1 += 1
n = len(recs)
out = {
    "cohort": label,
    "props": n,
    "true_tier1_unit": t1,
    "true_tier1_pct": round(100 * t1 / n) if n else 0,
    "any_unit_level": ul,
    "any_unit_pct": round(100 * ul / n) if n else 0,
    "top_tiers": dict(tier.most_common(6)),
}
with open("/tmp/cohort_results.jsonl", "a") as f:
    f.write(json.dumps(out) + "\n")
print(json.dumps(out))
