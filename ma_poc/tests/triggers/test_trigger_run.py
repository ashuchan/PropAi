"""Unit tests for trigger_run.py and _trigger_common.plan_tasks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
_here = Path(__file__).resolve().parent.parent.parent
_app = _here.parent
for _p in (_app, _here):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts._common.trigger import (  # noqa: E402
    ABSOLUTE_MAX_TASKS,
    MAX_TASKS_F1_MICRO,
    MAX_TASKS_G1_SMALL,
    check_job_exists,
    emit_structured_result,
    plan_tasks,
    project_for_env,
    run_gcloud,
    verify_gcloud_auth,
)

# ── plan_tasks() ─────────────────────────────────────────────────────────────


class TestPlanTasks:
    def test_rejects_both_flags(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            plan_tasks(total_properties=1000, target_hours=2.0, explicit_tasks=5)

    def test_rejects_neither_flag(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            plan_tasks(total_properties=1000)

    def test_rejects_zero_target_hours(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            plan_tasks(total_properties=1000, target_hours=0)

    def test_rejects_negative_explicit_tasks(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            plan_tasks(total_properties=1000, explicit_tasks=-1)

    def test_ceiling_division_two_hours(self) -> None:
        # 5001 properties / 2h → ceil(5001 / (2000*2)) = ceil(1.25) = 2
        plan = plan_tasks(total_properties=5001, target_hours=2.0)
        assert plan.tasks == 2

    def test_ceiling_division_one_task_minimum(self) -> None:
        # Very small property list always yields at least 1 task
        plan = plan_tasks(total_properties=1, target_hours=4.0)
        assert plan.tasks == 1

    def test_explicit_tasks_respected(self) -> None:
        plan = plan_tasks(total_properties=5000, explicit_tasks=7)
        assert plan.tasks == 7

    def test_estimated_hours_calculation(self) -> None:
        plan = plan_tasks(total_properties=2000, explicit_tasks=1)
        assert abs(plan.estimated_hours - 1.0) < 0.01

    def test_clamps_to_f1_micro_ceiling(self) -> None:
        # 5000 props / 0.25h → 10; f1-micro ceiling is 10, so result is 10
        plan = plan_tasks(total_properties=5000, target_hours=0.25, db_tier="f1-micro")
        assert plan.tasks <= MAX_TASKS_F1_MICRO

    def test_warns_above_f1_micro_threshold(self) -> None:
        # Explicit 12 tasks on f1-micro should produce a warning
        plan = plan_tasks(total_properties=1000, explicit_tasks=12, db_tier="f1-micro")
        assert plan.warning is not None
        assert "f1-micro" in plan.warning

    def test_no_warning_within_safe_range(self) -> None:
        plan = plan_tasks(total_properties=1000, explicit_tasks=5, db_tier="f1-micro")
        assert plan.warning is None

    def test_g1_small_ceiling(self) -> None:
        plan = plan_tasks(total_properties=100000, target_hours=0.1, db_tier="g1-small")
        assert plan.tasks <= MAX_TASKS_G1_SMALL

    def test_larger_tier_ceiling(self) -> None:
        plan = plan_tasks(total_properties=100000, target_hours=0.1, db_tier="larger")
        assert plan.tasks <= ABSOLUTE_MAX_TASKS

    def test_ceiling_of_ceiling_produces_warning(self) -> None:
        # explicit_tasks > ceiling should set a warning
        plan = plan_tasks(total_properties=1000, explicit_tasks=15, db_tier="f1-micro")
        assert plan.warning is not None


# ── trigger_run.py argument validation ───────────────────────────────────────


class TestTriggerRunArgs:
    def test_dry_run_does_not_call_gcloud(self) -> None:
        """--dry-run must not invoke any gcloud subprocess."""
        with patch("subprocess.run") as mock_run:
            with pytest.raises(SystemExit) as exc_info:
                # Simulate: python trigger_run.py --env staging --tasks 3 --dry-run --yes
                sys.argv = [
                    "trigger_run.py",
                    "--env",
                    "staging",
                    "--tasks",
                    "3",
                    "--dry-run",
                    "--yes",
                ]
                from scripts import trigger_run  # noqa: PLC0415

                trigger_run.main()
            assert exc_info.value.code == 0
            # gcloud must not have been called
            mock_run.assert_not_called()

    def test_rejects_without_tasks_or_target_hours(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = ["trigger_run.py", "--env", "staging"]
            from scripts import trigger_run  # noqa: PLC0415

            trigger_run.main()
        assert exc_info.value.code == 2

    def test_safety_clamp_target_hours_too_low(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = [
                "trigger_run.py",
                "--env",
                "staging",
                "--target-hours",
                "0.2",
                "--yes",
            ]
            from scripts import trigger_run  # noqa: PLC0415

            trigger_run.main()
        assert exc_info.value.code == 4

    def test_safety_clamp_target_hours_too_high(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = [
                "trigger_run.py",
                "--env",
                "staging",
                "--target-hours",
                "9",
                "--yes",
            ]
            from scripts import trigger_run  # noqa: PLC0415

            trigger_run.main()
        assert exc_info.value.code == 4

    def test_safety_clamp_tasks_too_high_on_f1_micro(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = [
                "trigger_run.py",
                "--env",
                "staging",
                "--tasks",
                "16",
                "--db-tier",
                "f1-micro",
                "--yes",
            ]
            from scripts import trigger_run  # noqa: PLC0415

            trigger_run.main()
        assert exc_info.value.code == 4

    def test_safety_clamp_absolute_max_tasks(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = [
                "trigger_run.py",
                "--env",
                "staging",
                "--tasks",
                "41",
                "--yes",
            ]
            from scripts import trigger_run  # noqa: PLC0415

            trigger_run.main()
        assert exc_info.value.code == 4


# ── emit_structured_result ────────────────────────────────────────────────────


class TestEmitStructuredResult:
    def test_emits_valid_json_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        emit_structured_result({"status": "SUCCESS", "tasks": 5, "env": "staging"})
        captured = capsys.readouterr()
        line = captured.err.strip()
        assert line.startswith("RESULT:")
        payload = json.loads(line[len("RESULT:") :])
        assert payload["status"] == "SUCCESS"
        assert payload["tasks"] == 5
        assert payload["env"] == "staging"

    def test_emits_all_expected_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = {
            "status": "FAILED",
            "execution_name": "jugnu-scrape-prod-abc",
            "tasks": 10,
            "env": "prod",
            "console_url": "https://console.cloud.google.com/...",
        }
        emit_structured_result(result)
        captured = capsys.readouterr()
        payload = json.loads(captured.err.strip()[len("RESULT:") :])
        for key in result:
            assert key in payload


# ── project_for_env ───────────────────────────────────────────────────────────


class TestProjectForEnv:
    def test_staging_returns_project(self) -> None:
        result = project_for_env("staging")
        assert "staging" in result

    def test_prod_returns_project(self) -> None:
        result = project_for_env("prod")
        assert "prod" in result

    def test_unknown_env_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown environment"):
            project_for_env("development")


# ── verify_gcloud_auth ────────────────────────────────────────────────────────


class TestVerifyGcloudAuth:
    def test_exit_3_when_gcloud_fails(self) -> None:
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SystemExit) as exc_info:
                verify_gcloud_auth("jugnu-staging-abc")
            assert exc_info.value.code == 3

    def test_exit_3_when_wrong_project(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="wrong-project\n")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SystemExit) as exc_info:
                verify_gcloud_auth("jugnu-staging-abc")
            assert exc_info.value.code == 3

    def test_passes_when_project_matches(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="jugnu-staging-abc\n")
        with patch("subprocess.run", return_value=mock_result):
            verify_gcloud_auth("jugnu-staging-abc")  # must not raise


# ── check_job_exists ──────────────────────────────────────────────────────────


class TestCheckJobExists:
    def test_exit_3_when_job_not_found(self) -> None:
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(SystemExit) as exc_info:
                check_job_exists("jugnu-scrape-staging", "us-central1", "jugnu-staging-abc")
            assert exc_info.value.code == 3

    def test_passes_when_job_exists(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="jugnu-scrape-staging\n")
        with patch("subprocess.run", return_value=mock_result):
            check_job_exists("jugnu-scrape-staging", "us-central1", "jugnu-staging-abc")


# ── run_gcloud ────────────────────────────────────────────────────────────────


class TestRunGcloud:
    def test_returns_completed_process(self) -> None:
        mock_result = MagicMock(returncode=0, stdout="output\n")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = run_gcloud("gcloud", "config", "list")
            mock_run.assert_called_once_with(
                ["gcloud", "config", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0
