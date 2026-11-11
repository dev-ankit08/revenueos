"""Scheduler leases: with several server processes, exactly one runs each workspace's scheduled work.

A lease is a row per job name with a holder and an expiry. A worker takes it when it is free or expired,
renews it every tick, and only the holder does the work. Expiry is stored as epoch seconds so the
comparison happens in SQL identically on SQLite and Postgres.
"""
import os
import socket
import time
import uuid

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import SchedulerLease

# Unique per process: host, pid and a random suffix (a restarted process never reuses its old leases).
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def acquire(db: Session, name: str, ttl_seconds: float, holder: str = HOLDER) -> dict | None:
    """Take or renew the lease. Returns its saved state (a dict) when this worker holds it, else None."""
    now = time.time()
    res = db.execute(
        update(SchedulerLease)
        .where(SchedulerLease.name == name,
               or_(SchedulerLease.holder == holder, SchedulerLease.expires_epoch < now))
        .values(holder=holder, expires_epoch=now + ttl_seconds)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount == 1:
        db.commit()
        row = db.get(SchedulerLease, name)
        db.refresh(row)
        return dict(row.state or {})
    if db.get(SchedulerLease, name) is not None:
        db.rollback()
        return None  # someone else holds it
    try:
        db.add(SchedulerLease(name=name, holder=holder, expires_epoch=now + ttl_seconds, state={}))
        db.commit()
        return {}
    except IntegrityError:  # another worker created it first
        db.rollback()
        return None


def save_state(db: Session, name: str, state: dict, holder: str = HOLDER) -> bool:
    """Persist bookkeeping (last hygiene day, last scan...) if this worker still holds the lease."""
    res = db.execute(
        update(SchedulerLease).where(SchedulerLease.name == name, SchedulerLease.holder == holder)
        .values(state=state).execution_options(synchronize_session=False)
    )
    db.commit()
    return res.rowcount == 1


def release(db: Session, name: str, holder: str = HOLDER) -> None:
    db.execute(update(SchedulerLease).where(SchedulerLease.name == name, SchedulerLease.holder == holder)
               .values(expires_epoch=0).execution_options(synchronize_session=False))
    db.commit()
