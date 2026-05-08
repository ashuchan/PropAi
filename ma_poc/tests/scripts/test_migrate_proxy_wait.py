"""Verify ma_poc.scripts.migrations.alembic.cloud_sql_proxy against a mock subprocess.

Real cloud-sql-proxy v2 writes its ready message to stdout. v1 wrote to
stderr. We cover both, plus the two hang paths (silent process, process
exits early) that the old readline-based wait couldn't distinguish.

No network, no real gcloud.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Capture the real Popen up front; tests monkeypatch migrate.subprocess.Popen
# and the fake Popen needs to spawn a *real* child process to emit scripted
# output. Going through a captured reference avoids recursion.
_REAL_POPEN = subprocess.Popen

# Import directly from the script file to avoid scripts/ package surgery.
_here = Path(__file__).resolve().parent
_ma_poc = _here.parent.parent
sys.path.insert(0, str(_ma_poc))

from scripts.migrations import alembic as migrate  # noqa: E402


def _python_subproc(stdout_lines: list[str], stderr_lines: list[str], exit_code: int | None = None, linger_sec: float = 0.5) -> list[str]:
    """Build a python -c command that prints scripted lines with small delays.

    Returns a list of args suitable for subprocess.Popen. Python process
    is used (not a shell here-doc) for Windows portability.
    """
    # Interleave in submission order: stdout first, then stderr, each line
    # flushed. Sleep between to simulate real wake-up timing.
    script = [
        "import sys, time",
    ]
    max_len = max(len(stdout_lines), len(stderr_lines))
    for i in range(max_len):
        if i < len(stdout_lines):
            script.append(f"sys.stdout.write({stdout_lines[i]!r} + '\\n'); sys.stdout.flush()")
        if i < len(stderr_lines):
            script.append(f"sys.stderr.write({stderr_lines[i]!r} + '\\n'); sys.stderr.flush()")
        script.append("time.sleep(0.05)")
    if exit_code is not None:
        script.append(f"sys.exit({exit_code})")
    else:
        # Linger so the ready-wait has time to find the signal.
        script.append(f"time.sleep({linger_sec})")
    return [sys.executable, "-u", "-c", "\n".join(script)]


class _FakePopen:
    """subprocess.Popen stand-in that runs a scripted python helper.

    Lets the test control exactly which stream the ready message lands on
    and whether the process exits early.
    """

    def __init__(self, argv: list[str]) -> None:
        self._proc = _REAL_POPEN(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.stdout = self._proc.stdout
        self.stderr = self._proc.stderr

    def poll(self) -> int | None:
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    @property
    def returncode(self) -> int:
        return self._proc.returncode


def _run_with_fake(monkeypatch: pytest.MonkeyPatch, argv: list[str], timeout_sec: float = 3.0) -> None:
    """Invoke cloud_sql_proxy() with subprocess.Popen swapped for a scripted helper."""
    def fake_popen(cmd: Any, *args: Any, **kwargs: Any) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(argv)

    monkeypatch.setattr(migrate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(migrate, "_PROXY_READY_TIMEOUT_SEC", timeout_sec)

    with migrate.cloud_sql_proxy("p", "i", "us-central1"):
        pass


def test_ready_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """cloud-sql-proxy v2 writes ready to stdout — must be detected."""
    argv = _python_subproc(
        stdout_lines=[
            "2025/01/01 starting up",
            "The proxy has started successfully and is ready for new connections!",
        ],
        stderr_lines=[],
    )
    _run_with_fake(monkeypatch, argv)


def test_ready_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """cloud-sql-proxy v1 wrote ready to stderr — must still be detected."""
    argv = _python_subproc(
        stdout_lines=[],
        stderr_lines=[
            "2020/01/01 starting up",
            "Ready for new connections",
        ],
    )
    _run_with_fake(monkeypatch, argv)


def test_silent_proxy_times_out_with_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy that prints nothing must time out within the deadline."""
    # No lines, lingers 5s — longer than our timeout.
    argv = _python_subproc(stdout_lines=[], stderr_lines=[], linger_sec=5.0)
    start = time.monotonic()
    with pytest.raises(SystemExit) as exc:
        _run_with_fake(monkeypatch, argv, timeout_sec=1.0)
    elapsed = time.monotonic() - start
    # Should hit the timeout branch, not hang indefinitely.
    assert elapsed < 3.0, f"hung for {elapsed:.1f}s"
    assert "did not become ready" in str(exc.value)


def test_proxy_crashes_early_surfaces_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the proxy exits before ready, the stderr and stdout are surfaced."""
    argv = _python_subproc(
        stdout_lines=["got a small banner"],
        stderr_lines=["permission denied: cloudsql.instances.connect"],
        exit_code=1,
    )
    with pytest.raises(SystemExit) as exc:
        _run_with_fake(monkeypatch, argv, timeout_sec=5.0)
    msg = str(exc.value)
    assert "exited with code 1" in msg
    assert "permission denied" in msg
    assert "got a small banner" in msg


def test_ready_message_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = _python_subproc(
        stdout_lines=["READY FOR NEW CONNECTIONS"],
        stderr_lines=[],
    )
    _run_with_fake(monkeypatch, argv)


def _run_main_capturing_db_url(
    monkeypatch: pytest.MonkeyPatch, gcloud_account: str
) -> str:
    """Drive migrate.main('prod up'), stub out subprocess/proxy, return DATABASE_URL."""
    captured_env: dict[str, str] = {}

    def fake_call(cmd: list[str], env: dict[str, str], **kwargs: Any) -> int:  # noqa: ARG001
        captured_env.update(env)
        return 0

    def fake_gcloud(cmd: list[str], **kwargs: Any) -> Any:  # noqa: ARG001
        class R:
            returncode = 0
            stdout = gcloud_account + "\n"
            stderr = ""

        return R()

    class _NoopCM:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(migrate, "ensure_sql_running", lambda *a, **kw: None)
    monkeypatch.setattr(migrate, "cloud_sql_proxy", lambda *a, **kw: _NoopCM())
    monkeypatch.setattr(migrate.subprocess, "run", fake_gcloud)
    monkeypatch.setattr(migrate.subprocess, "call", fake_call)
    monkeypatch.setattr(sys, "argv", ["migrate.py", "--env", "prod", "up"])

    try:
        migrate.main()
    except SystemExit:
        pass

    return captured_env.get("DATABASE_URL", "")


def test_database_url_uses_psycopg_v3_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the URL SQLAlchemy sees must select psycopg v3, not psycopg2.

    The project installs psycopg[binary] (v3), not psycopg2. A bare
    postgresql:// scheme resolves to the psycopg2 dialect in SQLAlchemy and
    fails with ModuleNotFoundError before even opening a connection.
    """
    from sqlalchemy.engine.url import make_url

    url = _run_main_capturing_db_url(monkeypatch, "deployer@proj.iam")
    assert url, "migrate.main did not set DATABASE_URL"

    dialect = make_url(url).get_dialect()
    assert dialect.driver == "psycopg", (
        f"Expected psycopg v3 dialect, got driver={dialect.driver!r} "
        f"from URL={url!r}. This will ModuleNotFoundError on psycopg2 in CI."
    )


def test_database_url_trims_gserviceaccount_suffix_for_sa_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: SA emails must be trimmed to match the Postgres IAM user.

    The terraform cloud_sql module creates SA users with
    ``trimsuffix(email, ".gserviceaccount.com")``, so when ``gcloud config
    get-value account`` returns the full SA email the migration must strip
    the suffix before using it as the Postgres user. Otherwise the DB
    closes the connection as an unknown user.
    """
    from sqlalchemy.engine.url import make_url

    url = _run_main_capturing_db_url(
        monkeypatch, "github-deployer@jugnu-494013.iam.gserviceaccount.com"
    )
    assert url, "migrate.main did not set DATABASE_URL"

    username = make_url(url).username
    assert username == "github-deployer@jugnu-494013.iam", (
        f"Expected SA suffix stripped; got username={username!r}"
    )


def test_database_url_preserves_human_iam_user_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLOUD_IAM_USER emails are NOT trimmed — the user name is the email."""
    from sqlalchemy.engine.url import make_url

    url = _run_main_capturing_db_url(monkeypatch, "ashu@surgexdigital.com")
    assert make_url(url).username == "ashu@surgexdigital.com"
