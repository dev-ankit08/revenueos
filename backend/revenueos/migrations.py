"""Schema migrations (Alembic), applied to every workspace database.

On startup each database is brought to the latest revision: a database that has never been stamped
(created by create_all, or older than migrations) is stamped at the baseline first, then upgraded.

Commands (from backend/):
    python -m revenueos.migrations upgrade              # every workspace to head
    python -m revenueos.migrations current              # revision per workspace
    python -m revenueos.migrations revision -m "rename x"   # new revision file (autogenerate against default DB)
"""
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "migrations"
BASELINE = "0001_baseline"


def _config(connection=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def upgrade_engine(eng) -> None:
    """Stamp an unversioned database at the baseline, then upgrade it to head."""
    with eng.begin() as conn:
        cfg = _config(conn)
        if "alembic_version" not in inspect(conn).get_table_names():
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")


def _all_engines():
    from .db import _factory_for
    from .services import workspaces

    for ctx in workspaces.all_context_ids():
        yield ctx, _factory_for(ctx).kw["bind"]


def main(argv: list[str]) -> None:
    from .db import init_db

    init_db()
    cmd = argv[0] if argv else "upgrade"
    if cmd == "upgrade":
        for ctx, eng in _all_engines():
            upgrade_engine(eng)
            print(f"workspace {ctx or 'default'}: up to date")
    elif cmd == "current":
        for ctx, eng in _all_engines():
            with eng.connect() as conn:
                print(f"workspace {ctx or 'default'}:", end=" ")
                command.current(_config(conn))
    elif cmd == "revision":
        message = argv[argv.index("-m") + 1] if "-m" in argv else "change"
        from .db import engine

        with engine.begin() as conn:
            command.revision(_config(conn), message=message, autogenerate=True)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
