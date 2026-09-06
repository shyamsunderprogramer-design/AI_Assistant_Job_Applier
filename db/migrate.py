"""Additive schema migration for an existing SQLite database.

`Base.metadata.create_all` creates missing TABLES but never adds a column to a
table that already exists, so a model change silently breaks every query
against a DB that predates it. This closes that gap the only way that is safe
without a migration framework: it ADDs missing columns and does nothing else.

Deliberately additive-only. It never drops, renames, retypes, or reorders a
column, so it can be run on every startup and can never lose data. A change
that needs more than an ADD COLUMN is a real migration and should be written by
hand.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from db.models import Base

log = logging.getLogger(__name__)


def _sql_default(column) -> str:
    """Render a column's scalar default as a SQL literal, or NULL."""
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return "NULL"
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def ensure_schema(engine: Engine) -> list[str]:
    """Add any model column missing from the live DB. Returns what it added."""
    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    added: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in live_tables:
                continue  # create_all handles a brand-new table
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = (
                    f'ALTER TABLE {table.name} '
                    f'ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)} '
                    f"DEFAULT {_sql_default(column)}"
                )
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
                log.info("Schema: added column %s.%s", table.name, column.name)

    return added
