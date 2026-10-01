"""Small shared helpers."""
from datetime import timezone
from typing import Optional


def iso_utc(dt) -> Optional[str]:
    """
    Timestamps are stored as naive UTC by SQLite. Tag them explicitly so the
    browser does not parse them as local time (which shifted every date by the
    local UTC offset).
    """
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
