"""
scripts/runners/dispatcher.py — On-demand dispatcher for the jugnu-adhoc Cloud Run Job.

Lets an operator pick any script in ``ma_poc/scripts/`` from the Cloud Run
console and run it with arbitrary CLI arguments — without redeploying the
image or adding a new Job for every one-off task. Inherits the same image
(``jugnu``), service account, VPC connector, Cloud SQL Connector wiring,
GCS bucket, and secrets as the scrape/retry Jobs, so any script can talk
to Cloud SQL and Cloud Storage out of the box.

Inputs (precedence: env > argv):
  SCRIPT_NAME    Module stem inside ``ma_poc.scripts`` (e.g. ``validate_outputs``).
                 Required. May also be passed as the first positional argv.
  SCRIPT_ARGS    Shell-quoted argument string parsed via ``shlex`` (e.g.
                 ``--csv config/properties.csv --limit 5``). Optional. May
                 also be passed as the remaining positional argv.

Console flow:
  Cloud Run console → Jobs → ``jugnu-adhoc-{env}`` → EXECUTE →
  Container, variables & secrets → set SCRIPT_NAME (+ SCRIPT_ARGS) → Run.

Safety:
  - Allowlist: SCRIPT_NAME must resolve to an existing ``ma_poc/scripts/<name>.py``.
  - Underscore-prefixed modules (``__init__``, ``_trigger_common``) and
    anything containing path separators or ``..`` are rejected.
  - Markdown / batch / PowerShell helpers in this directory are skipped
    because they are not ``.py`` files.

Exit code is forwarded from the dispatched script.
"""

from __future__ import annotations

import os
import platform
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_PACKAGE = "ma_poc.scripts"

# Cloud Run injects these at runtime — surfacing them in the banner makes
# it possible to correlate console output with a specific job execution
# (and a specific task index when parallelism > 1).
_CLOUD_RUN_VARS = (
    "CLOUD_RUN_JOB",
    "CLOUD_RUN_EXECUTION",
    "CLOUD_RUN_TASK_INDEX",
    "CLOUD_RUN_TASK_COUNT",
    "CLOUD_RUN_TASK_ATTEMPT",
)

# Wiring envs that determine which DB / bucket / provider the dispatched
# script will see. Values are logged verbatim because none of these are
# secrets — secrets are injected as separate env vars (e.g. ANTHROPIC_API_KEY)
# and are explicitly NOT logged.
_WIRING_VARS = (
    "DATA_PROVIDER",
    "DATABASE_URL",
    "CLOUD_SQL_INSTANCE",
    "CLOUD_SQL_IP_TYPE",
    "BUCKET_NAME",
    "CSV_GCS_URI",
    "SCHEMA_VERSION",
    "LLM_PROVIDER",
    "DATA_DIR",
    "CONFIG_DIR",
    # Email transport — surfaces in the banner so operators can debug
    # send-failures from logs alone (which transport ran, who it tried
    # to impersonate, and where mail was being sent from/to).
    "EMAIL_TRANSPORT",
    "GMAIL_EMAILER_SA",
    "GMAIL_DELEGATED_USER",
    "REPORT_RECIPIENTS",
    "REPORT_SENDER_NAME",
)

# Substrings that, if present in an env var name, cause the value to be
# masked. Keep matching liberal — false positives are harmless, false
# negatives leak credentials into Cloud Logging.
_SECRET_NAME_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


_USAGE = """\
Usage:
  Set env var SCRIPT_NAME (and optionally SCRIPT_ARGS), or pass them as argv:
    python -m ma_poc.scripts.runners.dispatcher <script_name> [args...]

  SCRIPT_NAME is the dotted module path under ma_poc/scripts/, e.g.
  'checks.outputs' (resolves to ma_poc/scripts/checks/outputs.py) or
  'failure_debug_summary' (top-level scripts/ entrypoints stay flat).
  SCRIPT_ARGS is a shell-quoted string, e.g. '--csv config/properties.csv --limit 5'.
"""


def _log(msg: str) -> None:
    """Single timestamped line to stdout. Cloud Logging picks this up
    automatically; the timestamp is also useful when reading the raw
    container output locally."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    print(f"[run_script {ts}] {msg}", flush=True)


def _is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in _SECRET_NAME_HINTS)


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "***"
    return f"***{value[-4:]} (len={len(value)})"


def _log_environment(script_name: str) -> None:
    """Banner with everything an operator typically needs to debug a run:
    when, where, what image, what wiring, what arguments. Secrets are
    masked. Output goes to stdout so it lands in Cloud Logging."""
    _log("=" * 70)
    _log(f"jugnu-adhoc dispatcher start — script={script_name!r}")
    _log("-" * 70)

    _log(f"python      : {sys.version.split(chr(10))[0]}")
    _log(f"executable  : {sys.executable}")
    _log(f"platform    : {platform.platform()}")
    try:
        _log(f"hostname    : {socket.gethostname()}")
    except OSError:
        pass
    _log(f"cwd         : {os.getcwd()}")
    _log(f"scripts_dir : {_SCRIPTS_DIR}")

    _log("-- Cloud Run --")
    for var in _CLOUD_RUN_VARS:
        _log(f"  {var:24s} = {os.environ.get(var, '<unset>')}")

    _log("-- Wiring --")
    for var in _WIRING_VARS:
        val = os.environ.get(var)
        _log(f"  {var:24s} = {val if val is not None else '<unset>'}")

    _log("-- Secrets (masked) --")
    secret_names = sorted(n for n in os.environ if _is_secret_name(n))
    if not secret_names:
        _log("  (none present)")
    else:
        for name in secret_names:
            _log(f"  {name:24s} = {_mask(os.environ[name])}")

    _log("=" * 70)


def _resolve_inputs(argv: list[str]) -> tuple[str, list[str]]:
    """Return (script_name, args). Env wins over argv when both are set."""
    script_name = os.environ.get("SCRIPT_NAME", "").strip()
    raw_args = os.environ.get("SCRIPT_ARGS", "")

    if not script_name and argv:
        script_name = argv[0]
        argv = argv[1:]

    args: list[str]
    if raw_args:
        args = shlex.split(raw_args)
    else:
        args = list(argv)

    return script_name, args


def _validate_script(name: str) -> Path:
    """Reject path traversal, private modules, and missing files.

    Accepts dotted submodule names (e.g. ``backfills.artifacts``) — the
    Phase 2/3 reorg moved most scripts into subdirectories, so a flat
    ``backfill_artifacts_pg``-style name is no longer valid. Each segment
    of the dotted path is checked against the same private/hidden rule
    so ``email._client`` is rejected (private library), but
    ``email.daily`` resolves to ``ma_poc/scripts/email/daily.py``.
    """
    if not name:
        _log("FATAL: SCRIPT_NAME is required (env var or first argv)")
        raise SystemExit(f"error: SCRIPT_NAME is required\n\n{_USAGE}")
    if "/" in name or "\\" in name or ".." in name:
        _log(f"FATAL: SCRIPT_NAME must be a dotted module name, got {name!r}")
        raise SystemExit(f"error: SCRIPT_NAME must be a dotted module name, got {name!r}")
    if name.endswith(".py"):
        name = name[:-3]

    segments = name.split(".")
    for seg in segments:
        if not seg or seg.startswith("_") or seg.startswith("."):
            _log(f"FATAL: refusing to run private/hidden module {name!r}")
            raise SystemExit(f"error: refusing to run private/hidden module {name!r}")

    target = _SCRIPTS_DIR.joinpath(*segments).with_suffix(".py")
    if not target.is_file():
        # Walk every .py under scripts/ (skipping private dirs/files) and
        # surface dotted-path matches whose stem fuzzy-contains the typo.
        candidates: list[str] = []
        for p in _SCRIPTS_DIR.rglob("*.py"):
            rel = p.relative_to(_SCRIPTS_DIR)
            if any(part.startswith("_") for part in rel.parts):
                continue
            dotted = rel.with_suffix("").as_posix().replace("/", ".")
            if name.split(".")[-1].lower() in p.stem.lower():
                candidates.append(dotted)
        candidates.sort()
        hint = (
            f"\nDid you mean: {', '.join(candidates[:5])}"
            if candidates
            else ""
        )
        rel_target = target.relative_to(_SCRIPTS_DIR).as_posix()
        _log(f"FATAL: no such script ma_poc/scripts/{rel_target}{hint}")
        raise SystemExit(
            f"error: no such script ma_poc/scripts/{rel_target}\n"
            f"hint: pass the dotted module name without the .py extension{hint}"
        )
    return target


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    script_name, args = _resolve_inputs(argv)

    # Banner is logged BEFORE validation so a fatal on a bad SCRIPT_NAME
    # still leaves a debuggable trail (the operator sees the wiring they
    # were trying to use).
    _log_environment(script_name or "<unset>")

    target = _validate_script(script_name)
    _log(f"resolved    : {target}")
    _log(f"args        : {args!r}")

    cmd = [sys.executable, "-m", f"{_PACKAGE}.{script_name}", *args]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    _log(f"dispatching : {pretty}")
    _log("-" * 70)

    started = time.monotonic()
    started_wall = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(cmd, check=False)
        rc = completed.returncode
    except KeyboardInterrupt:
        _log("interrupted by SIGINT — exit 130")
        return 130
    except OSError as exc:
        _log(f"FATAL: failed to spawn child process: {exc}")
        return 1

    elapsed = time.monotonic() - started
    finished_wall = datetime.now(timezone.utc)
    _log("-" * 70)
    _log(f"started     : {started_wall.isoformat()}")
    _log(f"finished    : {finished_wall.isoformat()}")
    _log(f"elapsed     : {elapsed:.2f}s")
    _log(f"exit_code   : {rc}")
    _log("=" * 70)
    return rc


if __name__ == "__main__":
    sys.exit(main())
