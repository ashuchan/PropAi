#!/usr/bin/env bash
# Resumable grind over all 4 buckets. Per-har notes are written to
# per-har/{bucket}/{stem}.md. Worklist is appended to worklist.jsonl.
# Re-runs skip already-probed HARs (per-har/{bucket}/{stem}.md exists).
set -euo pipefail
cd "$(dirname "$0")/.."

WORKLIST=worklist.jsonl
SCRIPT=scripts/deep_probe.py
PROGRESS=progress.txt

# Clear progress / worklist only on --reset
if [ "${1:-}" = "--reset" ]; then
  rm -f "$WORKLIST" "$PROGRESS"
  rm -rf per-har
  echo "reset done"
  exit 0
fi

total=0
done=0
for bucket_dir in raw/*/; do
  bucket=$(basename "$bucket_dir")
  for h in "$bucket_dir"*.har; do
    [ -f "$h" ] || continue
    total=$((total + 1))
    stem=$(basename "$h" .har)
    out="per-har/$bucket/$stem.md"
    if [ -f "$out" ]; then
      done=$((done + 1))
      continue
    fi
    python3 "$SCRIPT" "$h" --out "$out" --jsonl "$WORKLIST" \
            --stem "$stem" --bucket "$bucket" 2>&1 | tail -1 \
            | sed "s|^|[$bucket] |"
    done=$((done + 1))
    echo "$done/$total" > "$PROGRESS"
  done
done

echo ""
echo "============================"
echo "grind complete: $done HARs"
echo "============================"
