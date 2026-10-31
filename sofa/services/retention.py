"""A caller's recording is kept for a while and then deleted. What was said stays in the call record as text; the sound does not.

`recording_retention_days` sets how long (0 keeps recordings). The scheduler runs this about once an hour: it deletes the file of every turn older than that, clears
the path in the call record, and sweeps the folder for recordings no turn points to.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..models import CallTurn

log = logging.getLogger("sofa.retention")
EVERY_SECONDS = 3600
_last = [0.0]


def purge_recordings(svc, now: datetime | None = None) -> int:
    """Delete the recordings that are past their time. Returns how many files were removed."""
    days = svc.settings.recording_retention_days
    if days <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    removed = 0
    with svc.session_factory() as db:
        for turn in db.scalars(select(CallTurn).where(CallTurn.audio_path.is_not(None))):
            made = turn.created_at if turn.created_at.tzinfo else turn.created_at.replace(tzinfo=timezone.utc)
            if made >= cutoff:
                continue
            try:
                Path(turn.audio_path).unlink(missing_ok=True)
            except OSError:
                log.warning("a recording could not be deleted: %s", turn.audio_path)
                continue
            turn.audio_path = None
            removed += 1
        db.commit()
    folder = Path(svc.settings.storage_dir) / "audio_in"
    if folder.exists():
        for f in folder.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
                f.unlink(missing_ok=True)
                removed += 1
    if removed:
        log.info("recordings older than %d days deleted: %d", days, removed)
    return removed


def run_if_due(svc) -> int:
    """For the scheduler's tick: purge, but no more than once an hour."""
    if time.monotonic() - _last[0] < EVERY_SECONDS and _last[0]:
        return 0
    _last[0] = time.monotonic()
    return purge_recordings(svc)
