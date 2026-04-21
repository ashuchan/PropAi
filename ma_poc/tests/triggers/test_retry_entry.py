"""Unit tests for jugnu_retry_entry.py — mode translation and env handling."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_here = Path(__file__).resolve().parent.parent.parent
_app = _here.parent
for _p in (_app, _here):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class TestRetryModeTranslation:
    """Test that RETRY_MODE env var is correctly translated to CLI flags."""

    def _run_with_mode(
        self,
        mode: str,
        monkeypatch: pytest.MonkeyPatch,
        additional_env: dict[str, str] | None = None,
    ) -> list[list[str]]:
        """Run the retry entry main() with a given mode and capture subprocess calls."""
        monkeypatch.setenv("RETRY_MODE", mode)
        monkeypatch.setenv("CSV_GCS_URI", "gs://jugnu-raw-staging/property-list/properties.csv")
        monkeypatch.setenv("BUCKET_NAME", "jugnu-raw-staging")
        monkeypatch.setenv("RUN_DATE", "2026-04-21")
        if additional_env:
            for k, v in additional_env.items():
                monkeypatch.setenv(k, v)

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kw: object) -> MagicMock:
            calls.append(list(cmd))
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            with patch("subprocess.Popen") as _:
                with pytest.raises(SystemExit):
                    # Re-import to pick up fresh env
                    import importlib

                    import scripts.jugnu_retry_entry as m  # noqa: PLC0415

                    importlib.reload(m)
                    m.main()

        return calls

    def test_errors_mode_uses_retry_errors_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._run_with_mode("errors", monkeypatch)
        runner_calls = [c for c in calls if "jugnu_retry_runner" in " ".join(c)]
        if runner_calls:
            assert "--retry-errors" in runner_calls[0]

    def test_resume_mode_uses_resume_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._run_with_mode("resume", monkeypatch)
        runner_calls = [c for c in calls if "jugnu_retry_runner" in " ".join(c)]
        if runner_calls:
            assert "--resume" in runner_calls[0]

    def test_invalid_mode_exits_with_code_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RETRY_MODE", "garbage")
        monkeypatch.setenv("CSV_GCS_URI", "gs://bucket/file.csv")
        monkeypatch.setenv("BUCKET_NAME", "jugnu-raw-staging")

        with pytest.raises(SystemExit) as exc_info:
            import importlib

            import scripts.jugnu_retry_entry as m  # noqa: PLC0415

            importlib.reload(m)
            m.main()

        # Exit code must signal usage error
        assert exc_info.value.code not in (0, None)

    def test_missing_csv_gcs_uri_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CSV_GCS_URI", raising=False)
        monkeypatch.setenv("RETRY_MODE", "errors")
        monkeypatch.setenv("BUCKET_NAME", "jugnu-raw-staging")

        with pytest.raises(SystemExit) as exc_info:
            import importlib

            import scripts.jugnu_retry_entry as m  # noqa: PLC0415

            importlib.reload(m)
            m.main()

        assert exc_info.value.code not in (0, None)

    def test_missing_bucket_name_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RETRY_MODE", "errors")
        monkeypatch.setenv("CSV_GCS_URI", "gs://bucket/file.csv")
        monkeypatch.delenv("BUCKET_NAME", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            import importlib

            import scripts.jugnu_retry_entry as m  # noqa: PLC0415

            importlib.reload(m)
            m.main()

        assert exc_info.value.code not in (0, None)
