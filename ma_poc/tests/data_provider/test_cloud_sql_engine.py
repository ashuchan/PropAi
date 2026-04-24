"""Unit tests for ``data_provider.sql.engine._make_cloud_sql_engine``.

The connector path can't be driven end-to-end in CI without a live
Cloud SQL instance (and on ARM64 Windows the psycopg v3 binary wheel
doesn't exist, so we can't even call ``create_engine`` with the
psycopg dialect). These tests mock both the connector module and
SQLAlchemy's ``create_engine`` and assert the builder:
  - only fires when ``CLOUD_SQL_INSTANCE`` is set
  - passes the parsed user + db to the connector
  - forwards the connector's connection object to SQLAlchemy as a
    ``creator=`` callable
  - reads ``CLOUD_SQL_IP_TYPE`` (default PRIVATE; PUBLIC when set)

Covers every failure mode that would land zero rows in prod with a
green shard.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


def _install_connector_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a fake ``google.cloud.sql.connector`` into sys.modules.

    Returns a dict the caller can introspect for connect() args — lets
    tests assert the connector got the right instance/user/db.
    """
    recorded: dict[str, Any] = {"connect_calls": []}

    class _IPTypes:
        PRIVATE = "PRIVATE"
        PUBLIC = "PUBLIC"

    class _Connector:
        def __init__(self) -> None:
            pass

        def connect(self, instance: str, driver: str, **kw: Any) -> Any:
            recorded["connect_calls"].append({"instance": instance, "driver": driver, **kw})
            return object()  # opaque; never executed in these tests

    fake_mod = types.ModuleType("google.cloud.sql.connector")
    fake_mod.Connector = _Connector  # type: ignore[attr-defined]
    fake_mod.IPTypes = _IPTypes  # type: ignore[attr-defined]

    for parent in ("google", "google.cloud", "google.cloud.sql"):
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    monkeypatch.setitem(sys.modules, "google.cloud.sql.connector", fake_mod)

    return recorded


def _stub_create_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``create_engine`` inside the engine module with a recorder.

    We don't exercise real SQLAlchemy engine creation — that would
    require psycopg to load. We only care that the builder passed the
    right scheme + creator + kwargs. Returns the recorded call dict.
    """
    recorded: dict[str, Any] = {"url": None, "kwargs": None}

    def _fake_create_engine(url: str, **kw: Any) -> Any:
        recorded["url"] = url
        recorded["kwargs"] = kw
        return object()

    import data_provider.sql.engine as eng_mod

    monkeypatch.setattr(eng_mod, "create_engine", _fake_create_engine)
    return recorded


def test_connector_path_ignored_when_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default path: no CLOUD_SQL_INSTANCE → builder falls through to the URL path.

    Critical: this is the local-dev and migrate.py invariant. Breaking
    it would mean every alembic run tries to import the connector.
    """
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)
    recorded = _stub_create_engine(monkeypatch)

    from data_provider.sql.engine import make_engine

    make_engine("sqlite:///:memory:")
    assert recorded["url"] == "sqlite:///:memory:"
    # The fallback path never wires a ``creator`` — that's only the
    # connector branch.
    assert "creator" not in recorded["kwargs"]


def test_connector_path_activates_on_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting CLOUD_SQL_INSTANCE routes through the connector branch.

    Asserts the right scheme is used, a ``creator`` is wired, and that
    calling the creator invokes the connector with user/db from
    DATABASE_URL + IAM auth on + driver = psycopg.
    """
    rec_conn = _install_connector_stub(monkeypatch)
    rec_eng = _stub_create_engine(monkeypatch)
    monkeypatch.setenv("CLOUD_SQL_INSTANCE", "jugnu-494013:us-central1:jugnu-db-production")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://jugnu-worker-production%40jugnu-494013.iam@10.0.0.3:5432/jugnu?sslmode=require",
    )

    from data_provider.sql.engine import make_engine

    make_engine()

    # Scheme carries no host/port — the connector overrides the transport.
    assert rec_eng["url"] == "postgresql+psycopg://"
    assert callable(rec_eng["kwargs"]["creator"])
    assert rec_eng["kwargs"]["pool_pre_ping"] is True

    # Creator hasn't run yet — run it now and inspect connector args.
    rec_eng["kwargs"]["creator"]()
    assert len(rec_conn["connect_calls"]) == 1
    call = rec_conn["connect_calls"][0]
    assert call["instance"] == "jugnu-494013:us-central1:jugnu-db-production"
    assert call["driver"] == "psycopg"
    assert call["enable_iam_auth"] is True
    assert call["ip_type"] == "PRIVATE"
    assert call["db"] == "jugnu"
    assert "jugnu-worker-production" in call["user"]


def test_connector_path_honors_ip_type_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLOUD_SQL_IP_TYPE=PUBLIC is respected — needed for staging smoke from an outside network."""
    rec_conn = _install_connector_stub(monkeypatch)
    rec_eng = _stub_create_engine(monkeypatch)
    monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:us-central1:db")
    monkeypatch.setenv("CLOUD_SQL_IP_TYPE", "PUBLIC")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u@h:5432/d")

    from data_provider.sql.engine import make_engine

    make_engine()
    rec_eng["kwargs"]["creator"]()
    assert rec_conn["connect_calls"][0]["ip_type"] == "PUBLIC"


def test_connector_path_fails_if_user_or_db_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ``postgresql://host/`` URL can't tell the connector who to connect as.

    Terraform is authoritative on DATABASE_URL; we fail loudly rather
    than silently connecting as an empty user.
    """
    _install_connector_stub(monkeypatch)
    _stub_create_engine(monkeypatch)
    monkeypatch.setenv("CLOUD_SQL_INSTANCE", "p:us-central1:db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://@h/")  # no user, no db

    from data_provider.sql.engine import make_engine

    with pytest.raises(RuntimeError, match="DATABASE_URL must supply"):
        make_engine()
