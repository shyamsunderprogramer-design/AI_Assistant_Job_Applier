"""Additive schema migration for an existing SQLite database.

`Base.metadata.create_all` creates missing TABLES but never adds a column to a
table that already exists, so a model change silently breaks every query
against a DB that predates it. This closes that gap the only way that is safe
without a migration framework: it ADDs missing columns and does nothing else.

Deliberately additive-only. It never drops, renames, retypes, or reorders a
column, so it can be run on every startup and can never lose data. A change
that needs more than an ADD COLUMN is a real migration and should be written by
hand.

Indexes too (25 Oct 2026): create_all makes a new table's indexes but never one
added to a table that exists, and two of them guard against applying twice. A
unique index added to a table with rows first gets room made for it (MAKE_ROOM),
the way the app would have settled the duplicates itself; nothing is deleted.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from data_engineering.db.models import Base

log = logging.getLogger(__name__)


def ensure_history_outbox(engine: Engine) -> None:
    """The Phase 3 history repair only: add its empty table/indexes, never backfill.

    Normal startup's create_all also creates this table. This narrow entry point
    permits a backed-up live installation to apply only this approved migration.
    Older code ignores the table; a code rollback must retain pending events.
    """
    from data_engineering.db.models import ApplyHistoryOutbox
    ApplyHistoryOutbox.__table__.create(engine, checkfirst=True)


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


FINISHED = "'applied', 'failed', 'skipped', 'cancelled'"

# Rows that stand in the way of a unique index added to a table that already has rows, settled first.
MAKE_ROOM = {
    # One live run: any but the newest is interrupted, as recovery would mark a dead one (and
    # recovery, at the app's start, looks at the newest).
    "uq_apply_runs_one_live":
        "UPDATE apply_runs SET state = 'interrupted', finished_at = CURRENT_TIMESTAMP, "
        "note = 'Interrupted: another run was going at the same time (found when the app was updated)' "
        "WHERE state IN ('running', 'pausing') AND id <> "
        "(SELECT max(id) FROM apply_runs WHERE state IN ('running', 'pausing'))",
    # One live entry per job: the first added stays, later copies are cancelled (kept, with their history).
    "uq_apply_items_live_job":
        f"UPDATE apply_items SET state = 'cancelled', outcome = 'Cancelled: on the list twice.', "
        f"finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
        f"WHERE state NOT IN ({FINISHED}) AND id NOT IN "
        f"(SELECT min(id) FROM apply_items WHERE state NOT IN ({FINISHED}) GROUP BY job_id)",
}


def ensure_schema(engine: Engine) -> list[str]:
    """Add any model column, then any model index, missing from the live DB. Returns what it added."""
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

    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue
        # Read from SQLite's own catalogue: the inspector skips an index on an expression, and
        # uq_apply_runs_one_live is one -- it would be "missing", and created again, at every start.
        if engine.dialect.name == "sqlite":
            with engine.connect() as conn:
                present = {name for (name,) in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table.name,))}
        else:
            present = {i["name"] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            with engine.begin() as conn:
                if index.name in MAKE_ROOM:
                    conn.execute(text(MAKE_ROOM[index.name]))
                index.create(conn)
            added.append(f"{table.name}:{index.name}")
            log.info("Schema: added index %s on %s", index.name, table.name)

    return added
