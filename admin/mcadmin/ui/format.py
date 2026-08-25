"""Value formatting for display."""

from __future__ import annotations

from datetime import datetime

from ..core.units import human_bytes, human_seconds

__all__ = ["human_age", "human_bytes", "human_seconds", "human_size"]

# `mc backup` wants coarser sizes than the GC report does.
human_size = human_bytes


def human_age(when: datetime, now: datetime | None = None) -> str:
    seconds = ((now or datetime.now()) - when).total_seconds()
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
