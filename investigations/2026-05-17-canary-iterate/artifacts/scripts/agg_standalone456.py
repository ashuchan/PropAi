"""agg_standalone456.py <run-dir-name> — aggregate sharded standalone results.

Pulls every shard's results.jsonl from
gs://jugnu-canary/runs/<run-dir>/shard_*/results.jsonl, tallies
UNIT/FLOORPLAN/NONE/DEAD, and cross-checks the 50 user-validated
eyeball verdicts (artifacts/eyeball/batch3_verdicts.csv) so we can
compare measured recovery against the ~86% eyeball estimate.
"""
import csv
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

run_dir = sys.argv[1]
base = Path(__file__).resolve().parents[2]  # investigations/2026-05-17-canary-iterate

ls = subprocess.run(
    ["bash", "-c", f"gsutil ls gs://jugnu-canary/runs/{run_dir}/shard_*/results.jsonl 2>/dev/null"],
    capture_output=True, text=True,
).stdout.split()

rows: list[dict] = []
for p in ls:
    try:
        body = subprocess.run(["gsutil", "cat", p], capture_output=True, timeout=120).stdout
        for line in body.decode().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception as e:
        print(f"skip {p}: {e}", file=sys.stderr)

klass = Counter(r["klass"] for r in rows)
phase = Counter(r.get("phase") or "none" for r in rows)
n = len(rows)
unit = klass.get("UNIT", 0)
fp = klass.get("FLOORPLAN", 0)
recover_pct = round(100 * unit / n) if n else 0

# Eyeball cross-check: how do measured klasses line up with the 50 verdicts?
def norm(u: str) -> str:
    return (u or "").lower().replace("https://", "").replace("http://", "").replace(
        "www.", ""
    ).rstrip("/").split("/")[0].split("#")[0]


by_host = {norm(r["url"]): r for r in rows}
eb = base / "artifacts" / "eyeball" / "batch3_verdicts.csv"
agree = Counter()
eyeball_n = 0
if eb.exists():
    with eb.open() as f:
        for row in csv.DictReader(f):
            url = row.get("url") or row.get("URL") or ""
            verdict = (row.get("verdict") or row.get("U/F/D") or "").strip().upper()[:1]
            if not verdict:
                continue
            eyeball_n += 1
            m = by_host.get(norm(url))
            if not m:
                agree["NOT_IN_RUN"] += 1
                continue
            k = m["klass"]
            if verdict == "U" and k == "UNIT":
                agree["U_hit"] += 1
            elif verdict == "U" and k != "UNIT":
                agree["U_miss"] += 1
            elif verdict == "F":
                agree["F_" + k] += 1
            else:
                agree["D_" + k] += 1

out = {
    "run_dir": run_dir,
    "props": n,
    "klass": dict(klass),
    "phase": dict(phase),
    "unit_recover_pct": recover_pct,
    "unit": unit,
    "floorplan": fp,
    "eyeball_n": eyeball_n,
    "eyeball_crosscheck": dict(agree),
}
print(json.dumps(out, indent=2))
(base / "artifacts" / "analysis" / "standalone456_agg.json").write_text(
    json.dumps(out, indent=2)
)
