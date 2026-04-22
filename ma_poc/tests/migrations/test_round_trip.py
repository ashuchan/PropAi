"""Round-trip migration tests using testcontainers.

Every migration must upgrade and downgrade cleanly. This is the gate that
blocks merging a broken migration. Tests run against an ephemeral Postgres
container — no external database needed.

Requires: testcontainers[postgres], alembic, sqlalchemy, psycopg[binary]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the repo root is in path for alembic config resolution
_repo_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

ALEMBIC_INI = _repo_root / "infra" / "sql" / "alembic.ini"

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    HAS_TESTCONTAINERS = True
except ImportError:
    HAS_TESTCONTAINERS = False

pytestmark = pytest.mark.skipif(
    not HAS_TESTCONTAINERS,
    reason="testcontainers[postgres] not installed",
)


@pytest.fixture(scope="module")
def pg():  # type: ignore[no-untyped-def]
    """Ephemeral Postgres container for the full module."""
    with PostgresContainer("postgres:15-alpine") as container:
        yield container


def _url(pg) -> str:  # type: ignore[no-untyped-def]
    """Return a psycopg (v3) dialect URL — the driver in requirements.txt."""
    url: str = pg.get_connection_url()
    # testcontainers defaults to psycopg2 dialect; swap to v3 since that's
    # what we ship. Plain `postgresql://` would make SQLAlchemy import
    # psycopg2 by default, which is not installed.
    return url.replace("postgresql+psycopg2://", "postgresql+psycopg://")


def _alembic(pg, *args: str) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
    env = os.environ.copy()
    env["DATABASE_URL"] = _url(pg)
    return subprocess.run(
        ["alembic", "-c", str(ALEMBIC_INI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_upgrade_head(pg) -> None:  # type: ignore[no-untyped-def]
    """Upgrading to head from a clean database must succeed."""
    result = _alembic(pg, "upgrade", "head")
    assert result.returncode == 0, f"upgrade head failed:\n{result.stderr}"


def test_downgrade_base(pg) -> None:  # type: ignore[no-untyped-def]
    """Downgrading all the way to base must succeed."""
    # Ensure we're at head first
    _alembic(pg, "upgrade", "head")
    result = _alembic(pg, "downgrade", "base")
    assert result.returncode == 0, f"downgrade base failed:\n{result.stderr}"


def test_full_round_trip(pg) -> None:  # type: ignore[no-untyped-def]
    """Up → down → up must be idempotent."""
    assert _alembic(pg, "upgrade", "head").returncode == 0
    assert _alembic(pg, "downgrade", "base").returncode == 0
    assert _alembic(pg, "upgrade", "head").returncode == 0


def test_per_revision_round_trip(pg) -> None:  # type: ignore[no-untyped-def]
    """For each revision: upgrade → downgrade one step → upgrade again."""
    versions_dir = _repo_root / "infra" / "sql" / "versions"
    revision_files = sorted(versions_dir.glob("*.py"))
    revisions = [f.stem.split("_")[0] for f in revision_files if not f.name.startswith("_")]

    # Start clean
    _alembic(pg, "downgrade", "base")

    for rev in revisions:
        up = _alembic(pg, "upgrade", rev)
        assert up.returncode == 0, f"upgrade {rev} failed:\n{up.stderr}"

        down = _alembic(pg, "downgrade", "-1")
        assert down.returncode == 0, f"downgrade -1 from {rev} failed:\n{down.stderr}"

        up2 = _alembic(pg, "upgrade", rev)
        assert up2.returncode == 0, f"re-upgrade {rev} failed:\n{up2.stderr}"


def test_migration_is_deterministic(pg) -> None:  # type: ignore[no-untyped-def]
    """Running the full round-trip three times should always succeed."""
    for run in range(3):
        assert _alembic(pg, "upgrade", "head").returncode == 0, f"Run {run}: upgrade failed"
        assert _alembic(pg, "downgrade", "base").returncode == 0, f"Run {run}: downgrade failed"
    # Leave at head
    _alembic(pg, "upgrade", "head")


def test_current_shows_revision_after_upgrade(pg) -> None:  # type: ignore[no-untyped-def]
    """'alembic current' must show a non-empty revision after upgrade."""
    _alembic(pg, "upgrade", "head")
    result = _alembic(pg, "current")
    assert result.returncode == 0
    # Output should mention the revision
    assert "000_initial_schema" in result.stdout or "000_initial_schema" in result.stderr
