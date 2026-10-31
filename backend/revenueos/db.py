"""Database access, routed per client workspace.

Each workspace has its own database (a SQLite file next to the default one, or a Postgres
schema), so one client's data can never appear in another's queries. `current_workspace`
(a context variable) picks it for every request and background job; None = the default
workspace, which is DATABASE_URL itself, so a single-company install works as before.

Platform tables (workspaces, users, sign-in sessions) live only in the default database.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

current_workspace: ContextVar[int | None] = ContextVar("current_workspace", default=None)


def _engine(url: str, schema: str | None = None):
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    if schema:
        args["options"] = f"-csearch_path={schema}"
    return create_engine(url, connect_args=args, pool_pre_ping=True)


engine = _engine(settings.database_url)  # the default workspace + platform tables
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
_factories: dict[int, sessionmaker] = {}


class Base(DeclarativeBase):
    """Per-workspace business data (leads, deals, drafts, ...)."""


class ControlBase(DeclarativeBase):
    """Platform data in the default database only: workspaces, users, sessions."""


def workspace_db_location(slug: str) -> tuple[str, str | None]:
    """(database url, postgres schema) for a new workspace."""
    if settings.database_url.startswith("sqlite"):
        default_file = Path(settings.database_url.split("sqlite:///", 1)[1])
        folder = default_file.parent / "workspaces"
        folder.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{folder / f'{slug}.db'}", None
    return settings.database_url, f"ws_{slug.replace('-', '_')}"


def _factory_for(workspace_id: int | None) -> sessionmaker:
    if workspace_id is None:
        return SessionLocal
    if workspace_id not in _factories:
        from .models import Workspace

        with SessionLocal() as control:
            ws = control.get(Workspace, workspace_id)
            if not ws:
                raise LookupError(f"Workspace {workspace_id} does not exist.")
            url, schema = ws.db_url, ws.db_schema
        eng = _engine(url, schema)
        if schema:
            with eng.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        _prepare(eng)
        _factories[workspace_id] = sessionmaker(bind=eng, expire_on_commit=False)
    return _factories[workspace_id]


def _prepare(eng) -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(eng)
    _add_missing_columns(eng)


def init_db() -> None:
    _prepare(engine)
    ControlBase.metadata.create_all(engine)
    _add_missing_columns(engine, ControlBase)


def _add_missing_columns(eng, base=Base) -> None:
    """Additive-only schema sync: new nullable columns on existing tables.

    create_all() never alters existing tables, so this keeps older databases working
    as models grow. Anything beyond adding nullable columns needs a real migration tool.
    """
    inspector = inspect(eng)
    existing_tables = set(inspector.get_table_names())
    with eng.begin() as conn:
        for table in base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have and col.nullable:
                    col_type = col.type.compile(dialect=eng.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}'))


def new_session() -> Session:
    return _factory_for(current_workspace.get())()


def get_db() -> Iterator[Session]:
    """FastAPI dependency (current workspace)."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For agents and background jobs (current workspace): commit on success, roll back on error."""
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def control_scope() -> Iterator[Session]:
    """Platform tables (always the default database)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def use_workspace(workspace_id: int | None):
    """Run a block as a given workspace (scheduler, OAuth callbacks, tests)."""
    token = current_workspace.set(workspace_id)
    try:
        yield
    finally:
        current_workspace.reset(token)
