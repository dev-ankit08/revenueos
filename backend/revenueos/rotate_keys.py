"""Re-encrypt every stored secret with the current SECRET_KEY.

    1. Set SECRET_KEY to a new long random value and SECRET_KEY_OLD to the previous one.
    2. cd backend && python -m revenueos.rotate_keys
    3. Remove SECRET_KEY_OLD.

Covers workspace settings secrets, connected mailbox/calendar tokens in every workspace, and two-factor secrets.
"""
from sqlalchemy import select

from .config import SECRET_KEYS, settings
from .db import _factory_for, control_scope, init_db
from .models import Connection, User, Workspace
from .services import secrets, workspaces


def main() -> None:
    if not settings.secret_key_old:
        raise SystemExit("Set SECRET_KEY_OLD to the previous key first (and SECRET_KEY to the new one).")
    init_db()
    counts = {"workspace_secrets": 0, "tokens": 0, "two_factor": 0}
    with control_scope() as db:
        for ws in db.scalars(select(Workspace)):
            values = dict(ws.settings or {})
            for k in SECRET_KEYS & set(values):
                values[k] = secrets.rotate(values[k])
                counts["workspace_secrets"] += 1
            ws.settings = values
        for u in db.scalars(select(User).where(User.totp_secret.is_not(None))):
            u.totp_secret = secrets.rotate(u.totp_secret)
            counts["two_factor"] += 1
    for ctx in workspaces.all_context_ids():
        session = _factory_for(ctx)()
        try:
            for c in session.scalars(select(Connection)):
                c.access_token_enc = secrets.rotate(c.access_token_enc)
                c.refresh_token_enc = secrets.rotate(c.refresh_token_enc)
                counts["tokens"] += 1
            session.commit()
        finally:
            session.close()
    workspaces._cache.clear()
    print("Re-encrypted:", counts, "- you can now remove SECRET_KEY_OLD.")


if __name__ == "__main__":
    main()
