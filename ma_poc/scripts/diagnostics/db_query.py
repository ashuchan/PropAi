#!/usr/bin/env python3
"""Read-only SQL runner for diagnostics queries.

Usage:
    python scripts/diagnostics/db_query.py profile_persistence_health.sql
    python scripts/diagnostics/db_query.py profile_persistence_health.sql --query Q4_channel_row_counts
    python scripts/diagnostics/db_query.py --inline "SELECT count(*) FROM scrape_profiles"
    python scripts/diagnostics/db_query.py --list profile_persistence_health.sql

Defense-in-depth: every statement is parsed for a leading SELECT/WITH/EXPLAIN
keyword before execution. Anything else (INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/
TRUNCATE/GRANT) is rejected with a clear error. Schema changes belong in
alembic migrations, not in diagnostics.

DB connection:
    Reads DATABASE_URL from env (or ma_poc/.env via python-dotenv). Replaces
    the asyncpg driver with pg8000 (sync) so this CLI works without an event
    loop. Local cloud-sql-proxy + IAM auth must be configured per
    project_postgres_setup.md / project_cloud_sql_connector_constraints.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── SQL parsing helpers ────────────────────────────────────────────────────


# A statement is a read-only diagnostics query iff its first non-comment,
# non-whitespace keyword is one of these. Case-insensitive. CTE-style
# `WITH ... SELECT` is supported (the WITH itself is read-only here because
# we forbid the `INSERT/UPDATE/DELETE INTO ... RETURNING` flavour below).
_READ_ONLY_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW")

# Tokens that, anywhere in the statement (outside string literals), mark it
# as not-read-only. We deliberately lean strict: any one of these aborts.
# This blocks CTE-INSERT (`WITH foo AS (...) INSERT INTO ...`) and similar.
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "DROP", "CREATE", "ALTER", "GRANT", "REVOKE",
    "VACUUM", "REINDEX", "REFRESH", "COPY",
)


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line and /* ... */ block comments. Preserves string literals."""
    # Block comments first (non-nested — Postgres allows nesting, but we
    # don't, since none of our queries use it).
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Line comments — match -- to end of line.
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _is_read_only(sql: str) -> tuple[bool, str]:
    """Return (ok, reason). ``ok=False`` blocks execution."""
    stripped = _strip_sql_comments(sql).strip()
    if not stripped:
        return False, "empty statement"
    head = stripped.split(None, 1)[0].upper()
    if head not in _READ_ONLY_PREFIXES:
        return False, f"first keyword {head!r} is not in {_READ_ONLY_PREFIXES}"
    # Word-boundary match on forbidden keywords. Only check OUTSIDE
    # string literals; quick approximation by stripping single-quoted
    # string content first.
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", stripped)
    upper = no_strings.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", upper):
            return False, f"contains forbidden keyword {kw!r}"
    return True, ""


# ── Named-query parsing ────────────────────────────────────────────────────


@dataclass
class NamedQuery:
    name: str
    sql: str


def parse_sql_file(path: Path) -> list[NamedQuery]:
    """Split a SQL file by ``-- :: name`` markers.

    Statements without a marker are accumulated under name ``"_default"``.
    Each named query keeps any leading comment lines (descriptive headers)
    so ``--list`` can surface them.
    """
    text = path.read_text(encoding="utf-8")
    queries: list[NamedQuery] = []
    current_name = "_default"
    current_lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*--\s*::\s*(\S+)\s*$", line)
        if m:
            # Flush previous block if non-trivial
            blob = "\n".join(current_lines).strip()
            if blob:
                queries.append(NamedQuery(name=current_name, sql=blob))
            current_name = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)
    blob = "\n".join(current_lines).strip()
    if blob:
        queries.append(NamedQuery(name=current_name, sql=blob))
    # Drop pure-comment trailing blocks (no SELECT/WITH found).
    return [q for q in queries if _strip_sql_comments(q.sql).strip()]


# ── DB connection ──────────────────────────────────────────────────────────


def _resolve_database_url() -> str:
    """Read DATABASE_URL from env (or ma_poc/.env). Convert asyncpg → pg8000."""
    try:
        from dotenv import load_dotenv
        # Import-relative to repo root: works whether run from repo root or ma_poc/.
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL not set. Source ma_poc/.env or set env var. "
            "Local dev: ensure cloud-sql-proxy is running on localhost:5432."
        )
    # Sync runner uses pg8000; project's runtime uses asyncpg.
    return url.replace("+asyncpg", "+pg8000")


def _make_engine() -> Any:
    """Lazy import sqlalchemy + create the engine."""
    from sqlalchemy import create_engine
    return create_engine(_resolve_database_url())


# ── Execution ──────────────────────────────────────────────────────────────


def execute_query(sql: str) -> list[dict[str, Any]]:
    """Run a single read-only SELECT/WITH and return rows as list-of-dicts.

    Raises:
        PermissionError: statement failed the read-only guard.
        sqlalchemy.exc.SQLAlchemyError: DB-side error.
    """
    ok, reason = _is_read_only(sql)
    if not ok:
        raise PermissionError(
            f"Refusing to execute non-read-only SQL via db_query.py: {reason}. "
            "Schema changes belong in alembic migrations."
        )
    from sqlalchemy import text
    engine = _make_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        return [dict(row) for row in result.mappings()]


def run_named(file_path: Path, query_name: str | None) -> dict[str, list[dict[str, Any]]]:
    """Run one named query (if ``query_name`` given) or every named query in the file.

    Returns ``{name: rows}`` so JSON serialisation is straightforward.
    """
    queries = parse_sql_file(file_path)
    if query_name:
        queries = [q for q in queries if q.name == query_name]
        if not queries:
            available = [q.name for q in parse_sql_file(file_path)]
            raise KeyError(
                f"No query named {query_name!r} in {file_path.name}. "
                f"Available: {available}"
            )
    return {q.name: execute_query(q.sql) for q in queries}


# ── CLI ────────────────────────────────────────────────────────────────────


def _format_rows_human(rows: list[dict[str, Any]]) -> str:
    """Pretty-print rows as a markdown table (small result sets only)."""
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sql_file", nargs="?", type=Path,
        help="Path to a .sql file with -- :: name delimited queries.",
    )
    parser.add_argument(
        "--query", default=None,
        help="Run only the named query from the file (skip the others).",
    )
    parser.add_argument(
        "--inline", default=None,
        help="Run an inline SELECT instead of a file. Mutually exclusive with sql_file.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List the named queries in the file without executing them.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of markdown tables (for piping into jq).",
    )
    args = parser.parse_args(argv)

    if args.inline and args.sql_file:
        parser.error("--inline and sql_file are mutually exclusive.")
    if not args.inline and not args.sql_file:
        parser.error("Provide either a sql_file or --inline.")

    if args.list:
        if not args.sql_file:
            parser.error("--list requires a sql_file.")
        for q in parse_sql_file(args.sql_file):
            print(q.name)
        return 0

    try:
        if args.inline:
            results = {"_inline": execute_query(args.inline)}
        else:
            results = run_named(args.sql_file, args.query)
    except PermissionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, default=str, indent=2))
    else:
        for name, rows in results.items():
            print(f"\n=== {name} ===")
            print(_format_rows_human(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
