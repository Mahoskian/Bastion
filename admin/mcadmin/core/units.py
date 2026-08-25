"""Unit formatting shared by the analysis code and the UI.

Pure number-to-string conversion with no rendering opinions, so `core` can
describe its own findings without depending on how they get displayed.
"""

from __future__ import annotations

import re
from datetime import timedelta

WINDOW_RE = re.compile(r"^(?P<count>\d+)(?P<unit>[mhd])$")
WINDOW_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


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


def parse_duration(text: str) -> timedelta:
    """`6h`, `90m`, `3d` -- the window syntax every --since flag accepts."""
    match = WINDOW_RE.match(text.strip())
    if match is None:
        raise ValueError(f"expected something like 6h, 90m or 3d -- got {text!r}")
    return timedelta(**{WINDOW_UNITS[match["unit"]]: int(match["count"])})
