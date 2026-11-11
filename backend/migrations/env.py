"""Alembic environment. Runs against one workspace database at a time.

revenueos.migrations passes the connection in `config.attributes["connection"]` (one call per workspace).
Used directly (`alembic upgrade head` from backend/), it targets DATABASE_URL, the default workspace.
"""
from alembic import context

from revenueos import models  # noqa: F401  (register tables)
from revenueos.config import settings
from revenueos.db import Base, _engine

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite",  # SQLite needs batch ALTERs
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
    else:
        with _engine(settings.database_url).connect() as conn:
            _run(conn)
            conn.commit()
