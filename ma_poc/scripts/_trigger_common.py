"""Shared helpers for trigger scripts.

Exit code contract (documented here; used by all trigger scripts):
  0  Success
  1  Job failed
  2  Usage error
  3  Precondition failed (auth, wrong project, job not found)
  4  Safety clamp rejected
  5  Post-condition failed (smoke only)
  6  Timeout
  130 SIGINT
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Calibration constants (from arch doc §3)
# ---------------------------------------------------------------------------
# ~14.6s weighted per property, 10 browsers per task → ~2465 props/hr sustained.
# Conservative floor (headroom for warm-up, slow pages):
BASELINE_PROPS_PER_TASK_PER_HOUR: int = 2000

MAX_TASKS_F1_MICRO: int = 10  # db-f1-micro: ~25 connections, 2-3 per task
MAX_TASKS_G1_SMALL: int = 20  # db-g1-small: ~50 connections
ABSOLUTE_MAX_TASKS: int = 40  # Cloud Run parallelism ceiling we're willing to pay for

# Environment → GCP project mapping.  Keep in sync with infra/terraform/envs/*.tfvars.
# Resolution order: GCP_PROJECT_ID_{STAGING,PROD} → GCP_PROJECT_ID → placeholder.
# Placeholders are left so `--help` works without env; any real use must override.
_ENV_PROJECTS: dict[str, str] = {
    "staging": os.environ.get(
        "GCP_PROJECT_ID_STAGING",
        os.environ.get("GCP_PROJECT_ID", "jugnu-staging-<unique>"),
    ),
    "prod": os.environ.get(
        "GCP_PROJECT_ID_PROD",
        os.environ.get("GCP_PROJECT_ID", "jugnu-prod-<unique>"),
    ),
}

# The short env name ("prod"/"staging") matches image-tag conventions and
# appears in CI inputs, but Terraform-created resource names embed the
# long form from envs/*.tfvars (`env = "production"` in prod.tfvars).
# Anything that references a TF-created resource (Cloud Run jobs, SQL
# instances, GCS buckets, secrets, etc.) must translate through this map.
_TF_ENV_NAMES: dict[str, str] = {
    "staging": "staging",
    "prod": "production",
}

REGION: str = "us-central1"


@dataclass(frozen=True)
class TaskPlan:
    tasks: int
    estimated_hours: float
    warning: str | None  # human-readable warning; None if clean


def plan_tasks(
    *,
    total_properties: int,
    target_hours: float | None = None,
    explicit_tasks: int | None = None,
    db_tier: Literal["f1-micro", "g1-small", "larger"] = "f1-micro",
) -> TaskPlan:
    """Pick task count.

    Exactly one of *target_hours* or *explicit_tasks* must be provided.

    Raises:
        ValueError: if both or neither are set, or values are out of range.
    """
    if (target_hours is None) == (explicit_tasks is None):
        raise ValueError("exactly one of target_hours or explicit_tasks is required")
    if target_hours is not None and target_hours <= 0:
        raise ValueError("target_hours must be positive")
    if explicit_tasks is not None and explicit_tasks <= 0:
        raise ValueError("explicit_tasks must be positive")

    ceiling = {
        "f1-micro": MAX_TASKS_F1_MICRO,
        "g1-small": MAX_TASKS_G1_SMALL,
        "larger": ABSOLUTE_MAX_TASKS,
    }[db_tier]

    if explicit_tasks is not None:
        tasks = explicit_tasks
    else:
        assert target_hours is not None
        tasks = math.ceil(total_properties / (BASELINE_PROPS_PER_TASK_PER_HOUR * target_hours))
        tasks = max(1, min(tasks, ceiling))

    estimated_hours = total_properties / (BASELINE_PROPS_PER_TASK_PER_HOUR * max(tasks, 1))

    warning: str | None = None
    if tasks > ceiling:
        warning = (
            f"{tasks} tasks requested but db tier '{db_tier}' safely supports "
            f"≤ {ceiling}. Either upgrade the database or lower parallelism."
        )

    return TaskPlan(tasks=tasks, estimated_hours=estimated_hours, warning=warning)


def project_for_env(env: str) -> str:
    """Return the GCP project ID for the given environment."""
    if env not in _ENV_PROJECTS:
        raise ValueError(f"Unknown environment {env!r}; expected 'staging' or 'prod'")
    return _ENV_PROJECTS[env]


def tf_env_for(env: str) -> str:
    """Return the Terraform env suffix used in TF-created resource names.

    Use this when constructing the name of a resource Terraform created —
    e.g., ``jugnu-scrape-{tf_env_for(env)}``. The CLI/CI layer speaks the
    short form ("prod"), but the resource names embed the long form
    ("production") from envs/*.tfvars.
    """
    if env not in _TF_ENV_NAMES:
        raise ValueError(f"Unknown environment {env!r}; expected 'staging' or 'prod'")
    return _TF_ENV_NAMES[env]


def verify_gcloud_auth(required_project: str) -> None:
    """Fail fast (exit 3) if gcloud is not authenticated or pointed at the wrong project."""
    result = subprocess.run(
        ["gcloud", "config", "get-value", "project"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("gcloud not authenticated. Run: gcloud auth login", file=sys.stderr)
        sys.exit(3)
    current = result.stdout.strip()
    if current != required_project:
        print(
            f"gcloud project is '{current}', expected '{required_project}'. "
            f"Run: gcloud config set project {required_project}",
            file=sys.stderr,
        )
        sys.exit(3)


def check_job_exists(job_name: str, region: str, project: str) -> None:
    """Exit 3 with a clear message if the Cloud Run job does not exist."""
    result = subprocess.run(
        [
            "gcloud",
            "run",
            "jobs",
            "describe",
            job_name,
            f"--region={region}",
            f"--project={project}",
            "--format=value(name)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"Cloud Run job '{job_name}' not found in {region}/{project}. "
            f"Run: terraform apply (infra/terraform/) to create it.",
            file=sys.stderr,
        )
        sys.exit(3)


def run_gcloud(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a gcloud command; all gcloud calls go through here so they can be mocked in tests."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def emit_structured_result(result: dict) -> None:  # type: ignore[type-arg]
    """Write a single-line JSON result to stderr for CI / Claude Code parsing."""
    print(f"RESULT:{json.dumps(result)}", file=sys.stderr)
