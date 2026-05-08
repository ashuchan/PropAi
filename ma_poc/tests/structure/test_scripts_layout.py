"""Structural enforcement of the scripts/ refactor described in
``ma_poc/docs/SCRIPTS_REFACTOR.md``.

Adding a new subdirectory to ``scripts/`` is a 3-step change:
  1. Add the directory + ``__init__.py`` whose first-line docstring is
     the directory's charter.
  2. Add the charter to ``ma_poc/docs/SCRIPTS_REFACTOR.md`` §3.
  3. Add the directory to ``EXPECTED_LAYOUT`` below.

Skip steps 1–3 → CI fails. Which is the point.

Until the refactor lands, every assertion is wrapped in ``xfail(strict=False)``.
Each xfail is removed in the same commit that performs the corresponding move,
so the suite is a binary "is the refactor done yet" indicator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Layout source of truth — must match SCRIPTS_REFACTOR.md §2 and §3
# ---------------------------------------------------------------------------

# This file lives at ma_poc/tests/structure/test_scripts_layout.py, so
# parents[2] == ma_poc/.  parents[3] == repo root.
MA_POC = Path(__file__).resolve().parents[2]
REPO_ROOT = MA_POC.parent
SCRIPTS = MA_POC / "scripts"
CORE = MA_POC / "core"
TESTS = MA_POC / "tests"


# Charter docstrings — first line of the package __init__.py must match exactly.
CHARTERS: dict[Path, str] = {
    CORE: "Library modules used by runners and tests; never executed directly.",
    SCRIPTS / "_common": "Private helpers shared across script subdirectories; not for external import.",
    SCRIPTS / "runners": "Entrypoints that drive a full scrape pass over a set of properties.",
    SCRIPTS / "triggers": "Submit jobs to a remote executor (Cloud Run today).",
    SCRIPTS / "reports": "Produce a human-consumable artefact (json, markdown, html) from stored data.",
    SCRIPTS / "email": "Ship a report artefact to recipients; always depends on `scripts.reports`.",
    SCRIPTS / "gates": "Pass/fail audits of code and structure invariants used in CI.",
    SCRIPTS / "checks": "Pass/fail audits of runtime preconditions (deploy state, env, data shape).",
    SCRIPTS / "smoke": "End-to-end 'does it wake up' tests run before/after deploy.",
    SCRIPTS / "baselines": "Capture metrics from current code so a refactor can be measured.",
    SCRIPTS / "diagnostics": "One-off probes that answer 'why is X broken right now?'.",
    SCRIPTS / "migrations": "Destructive, one-shot changes to data shape on disk or DB.",
    SCRIPTS / "backfills": "Idempotent re-derivation of values into existing rows under a fixed schema.",
    SCRIPTS / "sync": "Cross-store data movement that preserves shape (no schema change).",
    SCRIPTS / "floor_plans": "CSV ⇄ DB floor-plan comparison toolchain.",
}


# Expected file layout — keys are NEW paths, values are OLD paths (or None
# for files that already lived at the new location).
# Encoded as (new_relpath_under_ma_poc, old_relpath_under_ma_poc_or_None).
EXPECTED_MOVES: list[tuple[str, str | None]] = [
    # ---- Phase 1: libraries → core/ -----------------------------------------
    ("core/identity.py", "scripts/identity_fallback.py"),
    ("core/state_store.py", "scripts/state_store.py"),
    ("core/schema_v2.py", "scripts/schema_v2.py"),
    ("core/concurrency.py", "scripts/concurrency.py"),
    ("core/replay.py", "scripts/replay.py"),
    ("core/issue_log.py", "scripts/validation.py"),

    # ---- Phase 2: runners ----------------------------------------------------
    ("scripts/_common/trigger.py", "scripts/_trigger_common.py"),
    ("scripts/runners/jugnu.py", "scripts/jugnu_runner.py"),
    ("scripts/runners/jugnu_retry.py", "scripts/jugnu_retry_runner.py"),
    ("scripts/runners/jugnu_retry_merge.py", "scripts/jugnu_retry_merge.py"),
    ("scripts/runners/shard_entry.py", "scripts/jugnu_shard_entry.py"),
    ("scripts/runners/retry_entry.py", "scripts/jugnu_retry_entry.py"),
    ("scripts/runners/dispatcher.py", "scripts/run_script.py"),
    ("scripts/runners/jugnu_scheduled.ps1", "scripts/jugnu_scheduled_runner.ps1"),

    # ---- Phase 2: triggers ---------------------------------------------------
    ("scripts/triggers/run.py", "scripts/trigger_run.py"),
    ("scripts/triggers/retry.py", "scripts/trigger_retry.py"),
    ("scripts/triggers/smoke.py", "scripts/trigger_smoke.py"),
    ("scripts/triggers/proxy_smoke.py", "scripts/trigger_proxy_smoke.py"),

    # ---- Phase 2: reports ----------------------------------------------------
    ("scripts/reports/daily.py", "scripts/generate_daily_report.py"),
    ("scripts/reports/analysis.py", "scripts/generate_analysis_report.py"),
    ("scripts/reports/health.py", "scripts/health_report.py"),
    ("scripts/reports/per_property.py", "scripts/scrape_report.py"),
    ("scripts/reports/escalation.py", "scripts/escalation_report.py"),
    ("scripts/reports/floor_plan_comparison.py", "scripts/report_floor_plan_comparison.py"),

    # ---- Phase 2: email ------------------------------------------------------
    ("scripts/email/_client.py", "scripts/email_html.py"),
    ("scripts/email/daily.py", "scripts/email_daily_report.py"),
    ("scripts/email/daily_failures.py", "scripts/email_daily_failures_report.py"),
    ("scripts/email/merge_analysis.py", "scripts/email_merge_analysis.py"),
    ("scripts/email/refactor_plan.py", "scripts/email_refactor_plan.py"),
    ("scripts/email/send_daily_failures.bat", "scripts/send_daily_failures_report.bat"),

    # ---- Phase 3: gates ------------------------------------------------------
    ("scripts/gates/jugnu.py", "scripts/gate_jugnu.py"),
    ("scripts/gates/pr23.py", "scripts/gate_pr23.py"),
    ("scripts/gates/pr24.py", "scripts/gate_pr24.py"),
    ("scripts/gates/pr25.py", "scripts/gate_pr25.py"),
    ("scripts/gates/refactor.py", "scripts/gate_refactor.py"),
    ("scripts/gates/xsource.py", "scripts/gate_xsource.py"),
    ("scripts/gates/escalation.py", "scripts/gate_escalation.py"),
    ("scripts/gates/antibot_fixes.py", "scripts/gate_antibot_fixes.py"),
    ("scripts/gates/stealth_pr1.py", "scripts/gate_stealth_pr1.py"),
    ("scripts/gates/prompts_merge_resilience.py", "scripts/gate_prompts_merge_resilience.py"),

    # ---- Phase 3: checks -----------------------------------------------------
    ("scripts/checks/deployment.py", "scripts/validate_deployment.py"),
    ("scripts/checks/outputs.py", "scripts/validate_outputs.py"),
    ("scripts/checks/adapters_live.py", "scripts/validate_adapters_live.py"),
    ("scripts/checks/csv_mapping.py", "scripts/verify_csv_mapping.py"),

    # ---- Phase 3: smoke ------------------------------------------------------
    ("scripts/smoke/imports.py", "scripts/smoke_test.py"),
    ("scripts/smoke/rentcafe_direct.py", "scripts/smoke_rentcafe_direct.py"),

    # ---- Phase 3: baselines --------------------------------------------------
    ("scripts/baselines/jugnu.py", "scripts/jugnu_baseline.py"),
    ("scripts/baselines/refactor.py", "scripts/refactor_baseline.py"),
    ("scripts/baselines/escalation.py", "scripts/escalation_baseline.py"),

    # ---- Phase 3: diagnostics ------------------------------------------------
    ("scripts/diagnostics/proxy.py", "scripts/check_proxy.py"),
    ("scripts/diagnostics/fetch_probe.py", "scripts/fetch_probe.py"),
    ("scripts/diagnostics/cluster_retry.py", "scripts/cluster_retry.py"),
    ("scripts/diagnostics/tls_vs_ip.py", "scripts/diagnostics/tls_vs_ip_diagnostic.py"),

    # ---- Phase 3: migrations -------------------------------------------------
    ("scripts/migrations/alembic.py", "scripts/migrate.py"),
    ("scripts/migrations/inferred_ids_v1_to_v2.py", "scripts/migrate_inferred_ids_v1_to_v2.py"),
    ("scripts/migrations/profiles_v1_to_v2.py", "scripts/migrate_profiles_v1_to_v2.py"),
    ("scripts/migrations/profiles_add_fetch.py", "scripts/migrate_profiles_add_fetch.py"),
    ("scripts/migrations/profiles_xsource.py", "scripts/migrate_profiles_xsource.py"),

    # ---- Phase 3: backfills --------------------------------------------------
    ("scripts/backfills/postgres.py", "scripts/backfill_pg.py"),
    ("scripts/backfills/artifacts.py", "scripts/backfill_artifacts_pg.py"),
    ("scripts/backfills/floor_plan_id.py", "scripts/backfill_floor_plan_id.py"),
    ("scripts/backfills/units_bed_bath.py", "scripts/backfill_units_bed_bath.py"),

    # ---- Phase 3: sync -------------------------------------------------------
    ("scripts/sync/run_to_pg.py", "scripts/sync_run_to_pg.py"),
    ("scripts/sync/cloud_to_local.py", "scripts/sync_cloud_to_local.py"),
    ("scripts/sync/properties_from_snapshots.py", "scripts/sync_properties_from_snapshots.py"),
    ("scripts/sync/csv_to_gcs.py", "scripts/deploy_csv_sync.py"),

    # ---- Phase 3: floor_plans ------------------------------------------------
    ("scripts/floor_plans/compare.py", "scripts/compare_floor_plans_csv.py"),
    ("scripts/floor_plans/export_disagreements.py", "scripts/export_floor_plan_disagreements.py"),
]


# Files allowed to remain at the scripts/ root. Anything else there fails.
SCRIPTS_ROOT_ALLOWED_FILES = {
    "__init__.py",
    "CLAUDE.md",
    "JUGNU_ALGORITHM.md",
    "llm_fallbacks.md",
    # One-shot operator tools that don't fit any of the categorised
    # subdirectories. Both predate the Phase 2/3 reorg and are invoked
    # directly via `python -m ma_poc.scripts.<name>`.
    "failure_debug_summary.py",
    "ingest_properties_csv.py",
}

# Subdirectories allowed at the scripts/ root.
SCRIPTS_ROOT_ALLOWED_DIRS = {
    "_common",
    "runners",
    "triggers",
    "reports",
    "email",
    "gates",
    "checks",
    "smoke",
    "baselines",
    "diagnostics",
    "migrations",
    "backfills",
    "sync",
    "floor_plans",
    "__pycache__",
}

# Module-name fragments that, if found in any directory name under ma_poc/,
# fail the build. These are the names rejected in SCRIPTS_REFACTOR.md §1.
BANNED_DIRECTORY_NAMES = frozenset({
    "utils",
    "helpers",
    "misc",
    "common",            # exception: scripts/_common (private, leading underscore) is fine
    "validators",
    "report_generators",
    "data_handlers",
})

# Old module names whose imports must not survive the refactor.
LEGACY_IMPORT_NAMES = sorted({
    Path(old).stem
    for _new, old in EXPECTED_MOVES
    if old is not None and old.endswith(".py")
})


# ---------------------------------------------------------------------------
# Test 1 — every new path exists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "new_relpath",
    [new for new, _old in EXPECTED_MOVES],
    ids=[new for new, _old in EXPECTED_MOVES],
)
def test_target_files_exist(new_relpath: str) -> None:
    target = MA_POC / new_relpath
    assert target.is_file(), (
        f"Expected {new_relpath} to exist after refactor "
        f"(see SCRIPTS_REFACTOR.md §4)."
    )


# ---------------------------------------------------------------------------
# Test 2 — every old path is gone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "old_relpath",
    [old for _new, old in EXPECTED_MOVES if old is not None],
    ids=[old for _new, old in EXPECTED_MOVES if old is not None],
)
def test_old_paths_are_gone(old_relpath: str) -> None:
    legacy = MA_POC / old_relpath
    assert not legacy.exists(), (
        f"Legacy file {old_relpath} still present — must be removed by "
        f"the refactor (see SCRIPTS_REFACTOR.md §4). git mv preserves history."
    )


# ---------------------------------------------------------------------------
# Test 3 — scripts/ root contains only the allowed files and subdirs
# ---------------------------------------------------------------------------

def test_no_unexpected_files_in_scripts_root() -> None:
    if not SCRIPTS.exists():
        pytest.fail(f"{SCRIPTS} does not exist")
    actual = {entry.name for entry in SCRIPTS.iterdir()}
    allowed = SCRIPTS_ROOT_ALLOWED_FILES | SCRIPTS_ROOT_ALLOWED_DIRS
    unexpected = actual - allowed
    assert not unexpected, (
        f"Unexpected entries at scripts/ root: {sorted(unexpected)}. "
        f"Either move them into a subdirectory or, if they truly belong at "
        f"the root, update SCRIPTS_ROOT_ALLOWED_* in this test AND document "
        f"why in SCRIPTS_REFACTOR.md §4o."
    )


# ---------------------------------------------------------------------------
# Test 4 — each subdir __init__.py opens with its charter docstring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pkg_dir, expected_charter",
    list(CHARTERS.items()),
    ids=[str(p.relative_to(MA_POC)) for p in CHARTERS],
)
def test_subdir_init_charters(pkg_dir: Path, expected_charter: str) -> None:
    init = pkg_dir / "__init__.py"
    assert init.is_file(), f"Missing {init.relative_to(MA_POC)}"
    text = init.read_text(encoding="utf-8").strip()
    # Accept either '"""charter"""' on one line or a multi-line docstring
    # whose first non-blank line is the charter.
    match = re.match(r'^[ru]?["\']{3}\s*(.+?)(?:["\']{3}|$)', text, re.DOTALL)
    assert match is not None, (
        f"{init.relative_to(MA_POC)} must open with a docstring. "
        f"Found: {text[:80]!r}"
    )
    first_line = match.group(1).splitlines()[0].strip()
    assert first_line == expected_charter, (
        f"{init.relative_to(MA_POC)} charter mismatch.\n"
        f"  Expected: {expected_charter!r}\n"
        f"  Found:    {first_line!r}\n"
        f"Update either the file or SCRIPTS_REFACTOR.md §3 — they must agree."
    )


# ---------------------------------------------------------------------------
# Test 5 — banned directory names do not exist anywhere under ma_poc/
# ---------------------------------------------------------------------------

# The ban applies to Python packages we own. node_modules/, the TS frontend,
# and the TS services package use `utils/` idiomatically and are out of scope.
_PY_OWNED_ROOTS = ("scripts", "core", "tests", "fetch", "discovery", "pms",
                   "validation", "observability", "reporting", "services",
                   "data_provider", "models", "extraction", "templates",
                   "storage", "schema", "state", "concurrency")
_PY_SCAN_EXCLUDES = ("node_modules", "__pycache__", ".venv", "venv",
                     ".pytest_cache", ".mypy_cache", "dist", "build",
                     ".git", "frontend")


def _is_in_python_owned_tree(path: Path) -> bool:
    """True iff `path` lives under a directory we treat as Python-owned."""
    try:
        rel = path.relative_to(MA_POC)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    if parts[0] not in _PY_OWNED_ROOTS:
        return False
    return not any(p in _PY_SCAN_EXCLUDES for p in parts)


def test_no_banned_directory_names() -> None:
    """This one is NOT xfail — banned names are banned starting now,
    even before the refactor lands. Scope: Python-owned directories only."""
    offenders: list[Path] = []
    for path in MA_POC.rglob("*"):
        if not path.is_dir():
            continue
        if not _is_in_python_owned_tree(path):
            continue
        # `_common` (with leading underscore) is allowed — signals private.
        if path.name in BANNED_DIRECTORY_NAMES:
            offenders.append(path)
    assert not offenders, (
        f"Banned directory names found in Python-owned tree: "
        f"{[str(p.relative_to(MA_POC)) for p in offenders]}.\n"
        f"See SCRIPTS_REFACTOR.md §1 — these names create graveyards or "
        f"collide with existing packages."
    )


# ---------------------------------------------------------------------------
# Test 6 — every script under ma_poc/scripts/ has a __main__ guard
# ---------------------------------------------------------------------------

def _iter_scripts_python_files() -> list[Path]:
    if not SCRIPTS.exists():
        return []
    files: list[Path] = []
    for p in SCRIPTS.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        if p.name == "__init__.py":
            continue
        # Files starting with `_` OR living inside a `_`-prefixed directory
        # (e.g. scripts/_common/trigger.py) are private helpers, not entrypoints.
        if p.name.startswith("_"):
            continue
        if any(part.startswith("_") for part in p.relative_to(SCRIPTS).parts[:-1]):
            continue
        files.append(p)
    return files


def test_scripts_files_have_main_guard() -> None:
    missing: list[str] = []
    for path in _iter_scripts_python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if 'if __name__ == "__main__":' not in text and "if __name__ == '__main__':" not in text:
            missing.append(str(path.relative_to(MA_POC)))
    assert not missing, (
        f"Files under scripts/ without a `if __name__ == \"__main__\":` "
        f"guard: {missing}. Either give them a CLI entrypoint or move them "
        f"to ma_poc/core/ (see SCRIPTS_REFACTOR.md §1, §4a)."
    )


# ---------------------------------------------------------------------------
# Test 7 — files under ma_poc/core/ have NO __main__ guard
# ---------------------------------------------------------------------------

def test_core_files_have_no_main_guard() -> None:
    if not CORE.exists():
        pytest.fail(
            f"{CORE} does not exist — Phase 1 of the refactor is not done."
        )
    offenders: list[str] = []
    for path in CORE.rglob("*.py"):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if 'if __name__ == "__main__":' in text or "if __name__ == '__main__':" in text:
            offenders.append(str(path.relative_to(MA_POC)))
    assert not offenders, (
        f"core/ files declare a __main__ guard: {offenders}. core/ is for "
        f"library code only — move executables to scripts/ (see "
        f"SCRIPTS_REFACTOR.md §1)."
    )


# ---------------------------------------------------------------------------
# Test 8 — no legacy script-import names survive in any .py under ma_poc/
# ---------------------------------------------------------------------------

# Two regexes catch both top-level and ma_poc-prefixed forms.
_IMPORT_PATTERNS = [
    # `from scripts.<name>` and `from ma_poc.scripts.<name>`
    re.compile(r"^\s*from\s+(?:ma_poc\.)?scripts\.([A-Za-z_][A-Za-z0-9_]*)"),
    # `import scripts.<name>` and `import ma_poc.scripts.<name>`
    re.compile(r"^\s*import\s+(?:ma_poc\.)?scripts\.([A-Za-z_][A-Za-z0-9_]*)"),
]


def _iter_python_files_under(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in _PY_SCAN_EXCLUDES for part in p.parts):
            continue
        if not _is_in_python_owned_tree(p.parent):
            continue
        # Don't scan this test file itself — it lists the legacy names.
        if p.resolve() == Path(__file__).resolve():
            continue
        out.append(p)
    return out


def test_no_legacy_imports() -> None:
    legacy = set(LEGACY_IMPORT_NAMES)
    offenders: list[str] = []
    for path in _iter_python_files_under(MA_POC):
        try:
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                for pat in _IMPORT_PATTERNS:
                    m = pat.match(line)
                    if m and m.group(1) in legacy:
                        rel = path.relative_to(MA_POC)
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        except OSError:
            continue
    assert not offenders, (
        f"Legacy script imports still present:\n  "
        + "\n  ".join(offenders)
        + "\nRewrite each to its new path under ma_poc.core or scripts.<subdir> "
          "(see SCRIPTS_REFACTOR.md §5a)."
    )


# ---------------------------------------------------------------------------
# Test 9 — tests/conftest.py no longer contains the validation.py workaround
# ---------------------------------------------------------------------------

def test_conftest_workaround_removed() -> None:
    conftest = TESTS / "conftest.py"
    text = conftest.read_text(encoding="utf-8", errors="replace")
    forbidden = [
        # The pre-bind block keys off these specific tokens.
        "scripts/validation.py",
        'spec_from_file_location("validation"',
        "spec_from_file_location('validation'",
    ]
    found = [tok for tok in forbidden if tok in text]
    assert not found, (
        f"tests/conftest.py still contains the scripts/validation.py "
        f"workaround tokens: {found}. After core/issue_log.py lands, the "
        f"collision is gone — delete the workaround "
        f"(see SCRIPTS_REFACTOR.md §5c)."
    )


# ---------------------------------------------------------------------------
# Test 10 — the move table covers every .py currently in scripts/ root
# (this catches new files added to scripts/ root that nobody categorised)
# ---------------------------------------------------------------------------

def test_move_table_covers_scripts_root() -> None:
    """NOT xfail — runs from day one. If someone adds a new .py to scripts/
    root without updating EXPECTED_MOVES + SCRIPTS_REFACTOR.md, this fails."""
    if not SCRIPTS.exists():
        pytest.skip("scripts/ does not exist")
    accounted_for_old = {
        old for _new, old in EXPECTED_MOVES if old is not None
    }
    # Anything currently in scripts/ root that ISN'T in the move table is
    # a new uncategorised file or a doc stayed-here file.
    unaccounted: list[str] = []
    for entry in SCRIPTS.iterdir():
        if entry.is_dir():
            continue
        if entry.name in SCRIPTS_ROOT_ALLOWED_FILES:
            continue
        rel = f"scripts/{entry.name}"
        if rel not in accounted_for_old:
            unaccounted.append(rel)
    assert not unaccounted, (
        f"Files in scripts/ root not categorised by the move table: "
        f"{unaccounted}. Either:\n"
        f"  (a) add them to EXPECTED_MOVES in this test + SCRIPTS_REFACTOR.md §4, or\n"
        f"  (b) add them to SCRIPTS_ROOT_ALLOWED_FILES if they truly belong at root.\n"
        f"No file in scripts/ may be uncategorised."
    )
