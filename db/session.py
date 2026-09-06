"""Engine/session setup. SQLite file lives in data/ (gitignored)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.loader import PROJECT_ROOT
from db.migrate import ensure_schema
from db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def init_engine(database_url: str) -> Engine:
    global _engine, _SessionFactory

    if database_url.startswith("sqlite:///") and ":memory:" not in database_url:
        # Resolve relative SQLite paths against the project root and ensure the
        # directory exists, so runs work from any cwd.
        raw_path = database_url[len("sqlite:///") :]
        path = Path(raw_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{path}"

    _engine = create_engine(database_url, future=True)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    # create_all won't add a column to a table that already exists, so an
    # existing DB needs the additive pass too.
    ensure_schema(_engine)
    return _engine


@contextmanager
def get_session() -> Iterator[Session]:
    if _SessionFactory is None:
        raise RuntimeError("init_engine() must be called before get_session()")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
