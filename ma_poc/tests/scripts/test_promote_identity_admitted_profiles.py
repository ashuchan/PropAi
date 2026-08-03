from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.scripts.backfills.promote_identity_admitted_profiles import (
    freeze_local_candidate,
    verify_reviewed_candidate,
)
from ma_poc.scripts.backfills.promote_strict_canary_profiles import FrozenProfile, promote_one


def _candidate(tmp_path: Path, property_id: int = 7) -> tuple[Path, Path, bytes]:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = ScrapeProfile(canonical_id=str(property_id))
    profile.navigation.winning_page_url = f"https://property-{property_id}.example.com/units"
    raw = (profile.model_dump_json(indent=2) + "\n").encode()
    (profiles / f"{property_id}.json").write_bytes(raw)
    ledger = tmp_path / "strict-profile-ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "property_id": str(property_id),
                "status": "ADMIT",
                "sanitized_profile_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return profiles, ledger, raw


def test_freeze_local_candidate_requires_admit_status_and_exact_hash(tmp_path: Path) -> None:
    profiles, ledger, raw = _candidate(tmp_path)

    frozen = freeze_local_candidate(profiles, ledger)

    assert set(frozen) == {7}
    assert frozen[7].raw == raw
    assert frozen[7].route_signals == ("winning_page_url",)


def test_freeze_local_candidate_rejects_tampered_profile(tmp_path: Path) -> None:
    profiles, ledger, _ = _candidate(tmp_path)
    (profiles / "7.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate_profile_sha256_mismatch:7"):
        freeze_local_candidate(profiles, ledger)


def test_freeze_local_candidate_rejects_non_admitted_profile(tmp_path: Path) -> None:
    profiles, ledger, _ = _candidate(tmp_path)
    row = json.loads(ledger.read_text(encoding="utf-8"))
    row["status"] = "REVIEW"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="profile_without_admit_ledger:7"):
        freeze_local_candidate(profiles, ledger)


def test_review_manifest_pins_candidate_hashes(tmp_path: Path) -> None:
    profiles, ledger, _ = _candidate(tmp_path)
    frozen = freeze_local_candidate(profiles, ledger)
    manifest = tmp_path / "promotion_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source_profiles": {
                    "7": {
                        "sha256": frozen[7].sha256,
                        "route_signals": ["winning_page_url"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert verify_reviewed_candidate(manifest, frozen)["source_profiles"]["7"]
    manifest.write_text(json.dumps({"source_profiles": {}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity_candidate_changed_since_review"):
        verify_reviewed_candidate(manifest, frozen)


def test_promote_one_rejects_overlap_changed_after_review() -> None:
    target = ScrapeProfile(canonical_id="7")
    target.navigation.winning_page_url = "https://property-7.example.com/old"
    target_raw = target.model_dump_json().encode()
    incoming = ScrapeProfile(canonical_id="7")
    incoming.navigation.winning_page_url = "https://property-7.example.com/new"
    incoming_raw = incoming.model_dump_json().encode()

    class Blob:
        generation = 12
        metadata: dict[str, str] = {}
        uploaded = False

        def reload(self) -> None:
            return None

        def download_as_bytes(self, **_kwargs: object) -> bytes:
            return target_raw

        def upload_from_string(self, *_args: object, **_kwargs: object) -> None:
            self.uploaded = True

    blob = Blob()

    class Bucket:
        def blob(self, _name: str) -> Blob:
            return blob

    result = promote_one(
        Bucket(),
        "profiles/",
        FrozenProfile(
            property_id=7,
            raw=incoming_raw,
            generation=0,
            sha256=hashlib.sha256(incoming_raw).hexdigest(),
            route_signals=("winning_page_url",),
        ),
        existed_before=True,
        expected_target_generation=11,
    )

    assert result["status"] == "generation_conflict_or_missing"
    assert result["error"] == "TargetGenerationChangedSinceReview"
    assert result["expected_generation"] == 11
    assert result["actual_generation"] == 12
    assert blob.uploaded is False
