"""SQL backend for the data-provider layer.

Dialect-agnostic: the same code runs against Postgres (`postgresql+psycopg://`)
and SQLite (`sqlite://`). Postgres is the production target; SQLite is the
test surrogate so the contract suite runs without a live database.

Public surface is `SqlDataProvider` + the SQLAlchemy `Base` (for Alembic).
"""

from data_provider.sql.models import Base
from data_provider.sql.provider import SqlDataProvider

__all__ = ["Base", "SqlDataProvider"]
