"""Unit formatting shared by the analysis code and the UI.

Pure number-to-string conversion with no rendering opinions, so `core` can
describe its own findings without depending on how they get displayed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

WINDOW_RE = re.compile(r"^(?P<count>\d+)(?P<unit>[mhd])$")
WINDOW_UNITS = {"m": "minutes", "h": "hours", "d": "days"}

# How a person names a moment when they are asking about one: "was 6pm
# yesterday bad?". Tried in order; the first that parses wins.
MOMENT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%H:%M:%S",
    "%H:%M",
    "%I%p",
    "%I:%M%p",
)


def human_bytes(n: float, precision: int = 1) -> str:
    value = float(n)
    for unit in ("B", "K", "M"):
        if value < 1024:
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.2f}G" if value < 10 else f"{value:.{precision}f}G"


def human_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def human_distance(centimetres: float) -> str:
    """The game measures travel in centimetres; nobody thinks in them."""
    metres = centimetres / 100
    if metres < 1000:
        return f"{metres:.0f}m"
    return f"{metres / 1000:,.1f}km"


def parse_duration(text: str) -> timedelta:
    """`6h`, `90m`, `3d` -- the window syntax every --since flag accepts."""
    match = WINDOW_RE.match(text.strip())
    if match is None:
        raise ValueError(f"expected something like 6h, 90m or 3d -- got {text!r}")
    return timedelta(**{WINDOW_UNITS[match["unit"]]: int(match["count"])})


def parse_moment(text: str, now: datetime | None = None) -> datetime:
    """A point in time, written the way someone would say it.

    Accepts a full date, a bare clock time, and `yesterday`/`today` in front of
    either. A bare clock time means the most recent time that has already
    happened -- asking about "6pm" at 3pm means yesterday evening, not one that
    has not arrived yet.
    """
    now = now or datetime.now()
    cleaned = " ".join(text.strip().lower().split())
    offset = 0
    for word, days in (("yesterday", 1), ("today", 0)):
        if cleaned.startswith(word):
            cleaned, offset = cleaned[len(word) :].strip(), days
            break

    for pattern in MOMENT_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
        if "%Y" in pattern:
            moment = parsed
        else:
            moment = datetime.combine(now.date(), parsed.time())
            if offset == 0 and moment > now:
                offset = 1  # a clock time still ahead of us means yesterday
        return moment - timedelta(days=offset)
    raise ValueError(
        f"cannot read {text!r} as a time -- try 18:00, 6pm, "
        "'yesterday 18:00' or 2026-08-24 18:00"
    )
