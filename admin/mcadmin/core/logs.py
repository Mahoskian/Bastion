"""Parsing the server log into records and player events.

A "record" is one timestamped line plus any continuation lines that belong to
it -- stack traces and the mod-loader's indented warnings are part of the entry
above them, not entries of their own.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Paths

# [17:30:13] [Server thread/INFO]: Steve joined the game
LINE_RE = re.compile(
    r"^\[(?P<time>\d{2}:\d{2}:\d{2})\] \[(?P<thread>[^\]]+?)/(?P<level>[A-Z]+)\]:? ?"
    r"(?P<message>.*)$"
)
# 2026-08-23-4.log.gz
DATED_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-\d+\.log(\.gz)?$")

COLOUR_CODE = re.compile(r"§.")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
NUMBER_RE = re.compile(r"-?\d+(\.\d+)?")

TRACE_PREFIXES = ("\tat ", "    at ", "Caused by:", "\t... ", "Suppressed:")
EXCEPTION_RE = re.compile(r"^[a-z][\w.]*\.[A-Z]\w*(Exception|Error|Throwable)\b")


class Level(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"
    DEBUG = "DEBUG"
    TRACE = "TRACE"

    @property
    def is_problem(self) -> bool:
        return self in {Level.WARN, Level.ERROR, Level.FATAL}


class EventKind(StrEnum):
    SERVER_START = "server_start"
    SERVER_STOP = "server_stop"
    JOIN = "join"
    MOB_DEATH = "mob_death"
    LEAVE = "leave"
    CHAT = "chat"
    DEATH = "death"
    ADVANCEMENT = "advancement"
    DISCONNECT = "disconnect"


# Vanilla death vocabulary. Broad on purpose -- a missed death is worse than a
# false positive, and the player name still has to match a known player.
DEATH_RE = re.compile(
    r"\b("
    r"was (slain|shot|killed|blown up|pricked|impaled|squashed|skewered|fireballed|"
    r"stung|poked|doomed|frozen|burnt|obliterated|struck)"
    r"|drowned|starved|suffocated|withered away|blew up|burned to death"
    r"|went up in flames|hit the ground|fell (from|off|out of|into)"
    r"|tried to swim in lava|walked into (a cactus|fire|danger)"
    r"|discovered the floor was lava|experienced kinetic energy"
    r"|didn't want to live|died|froze to death|left the confines"
    r")\b"
)
ADVANCEMENT_RE = re.compile(
    r"^(?P<player>\S+) has (made the advancement|completed the challenge|reached the goal) "
    r"\[(?P<title>.+)\]$"
)
CHAT_RE = re.compile(r"^<(?P<player>[^>]+)> (?P<text>.*)$")
# Villager Villager['Unemployed'/7605, l='ServerLevel[world]', x=..] died, message: '..'
MOB_DEATH_RE = re.compile(
    r"^(?P<kind>\w+) \w+\['[^']*'/\d+, l='ServerLevel\[(?P<dimension>[^\]]+)\]', "
    r"x=(?P<x>-?[\d.]+), y=(?P<y>-?[\d.]+), z=(?P<z>-?[\d.]+)\] died, "
    r"message: '(?P<cause>.*)'$"
)
SERVER_SAY_RE = re.compile(r"^(?:\[Not Secure\] )?\[Server\] (?P<text>.*)$")
JOIN_RE = re.compile(r"^(?P<player>\S+) joined the game$")
LEAVE_RE = re.compile(r"^(?P<player>\S+) left the game$")
LOGIN_RE = re.compile(r"^(?P<player>\S+)\[/(?P<address>[^\]]+)\] logged in with entity id")
LOST_RE = re.compile(r"^(?P<player>\S+) lost connection: (?P<reason>.*)$")
# A restart never emits "left the game" for whoever was online, so the server's
# own lifecycle has to close those sessions or playtime runs away to now.
SERVER_START_RE = re.compile(r"^Starting minecraft server version")
SERVER_STOP_RE = re.compile(r"^Stopping server$")


class Record(BaseModel):
    """One log entry: a timestamped line plus its continuation lines."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    thread: str
    level: Level
    message: str
    continuation: tuple[str, ...] = ()
    source: str = ""

    @property
    def fingerprint(self) -> str:
        """Identity of this *kind* of entry, ignoring the varying details.

        The first stack frame is folded in so two different failures that share
        an exception type stay distinct.
        """
        parts = [self.level.value, normalize(self.message)]
        for line in self.continuation:
            stripped = line.strip()
            if EXCEPTION_RE.match(stripped) or stripped.startswith("at "):
                parts.append(normalize(stripped))
                break
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    @property
    def summary(self) -> str:
        """A one-line rendering, with the exception type if there is one."""
        for line in self.continuation:
            stripped = line.strip()
            if EXCEPTION_RE.match(stripped):
                return f"{self.message}  ({stripped.split(':')[0]})"
        return self.message


class Event(BaseModel):
    """Something a player did."""

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    at: datetime
    player: str = ""
    detail: str = ""


def normalize(text: str) -> str:
    """Collapse the parts that vary between otherwise identical entries."""
    text = COLOUR_CODE.sub("", text)
    text = UUID_RE.sub("<uuid>", text)
    text = HEX_RE.sub("<hex>", text)
    text = NUMBER_RE.sub("<n>", text)
    return " ".join(text.split())


def log_files(directory: Path | None = None) -> list[Path]:
    """Server logs oldest-first, with latest.log last."""
    directory = directory or Paths.from_env().logs_dir
    dated = sorted(p for p in directory.glob("*.log.gz") if DATED_NAME_RE.match(p.name))
    latest = directory / "latest.log"
    return [*dated, *([latest] if latest.exists() else [])]


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return path.open(errors="replace")


def file_date(path: Path) -> date:
    """The day a log file covers.

    Rotated files carry it in the name; latest.log only has clock times, so
    its start is taken from the file's own timestamp.
    """
    match = DATED_NAME_RE.match(path.name)
    if match:
        return date.fromisoformat(match["date"])
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def parse_file(path: Path) -> Iterator[Record]:
    """Records from one log file, continuation lines folded in."""
    day = file_date(path)
    if path.name == "latest.log":
        # Only the end time is known, so walk back over any midnight rollover.
        day = _start_date_of_latest(path, day)

    pending: dict | None = None
    previous: datetime | None = None
    with _open(path) as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            match = LINE_RE.match(line)
            if match is None:
                if pending is not None and line.strip():
                    pending["continuation"].append(line)
                continue
            if pending is not None:
                yield _build(pending, path)
            clock = datetime.strptime(match["time"], "%H:%M:%S").time()
            at = datetime.combine(day, clock)
            if previous is not None and at < previous - timedelta(hours=1):
                day += timedelta(days=1)  # rolled past midnight
                at = datetime.combine(day, clock)
            previous = at
            pending = {
                "at": at,
                "thread": match["thread"],
                "level": match["level"],
                "message": match["message"],
                "continuation": [],
            }
    if pending is not None:
        yield _build(pending, path)


def _start_date_of_latest(path: Path, end_day: date) -> date:
    """latest.log may have begun the previous day; step back if the clock says so."""
    first: str | None = None
    last: str | None = None
    with _open(path) as handle:
        for raw in handle:
            match = LINE_RE.match(raw)
            if match:
                first = first or match["time"]
                last = match["time"]
    if first and last and first > last:
        return end_day - timedelta(days=1)
    return end_day


def _build(pending: dict, path: Path) -> Record:
    level = pending["level"]
    return Record(
        at=pending["at"],
        thread=pending["thread"],
        level=level if level in set(Level) else Level.INFO,
        message=pending["message"],
        continuation=tuple(pending["continuation"]),
        source=path.name,
    )


def parse(paths: list[Path]) -> Iterator[Record]:
    for path in paths:
        yield from parse_file(path)


def is_trace(line: str) -> bool:
    stripped = line.strip()
    return line.startswith(TRACE_PREFIXES) or bool(EXCEPTION_RE.match(stripped))


def extract_events(records: list[Record], players: set[str] | None = None) -> list[Event]:
    """Player-facing events. Deaths need the player set to avoid false matches."""
    known = set(players or ())
    # Joins tell us who is real, so collect them before classifying deaths.
    for record in records:
        match = JOIN_RE.match(record.message) or LOGIN_RE.match(record.message)
        if match:
            known.add(match["player"])

    events: list[Event] = []
    for record in records:
        if record.level is not Level.INFO:
            continue
        message = record.message
        if SERVER_STOP_RE.match(message):
            events.append(Event(kind=EventKind.SERVER_STOP, at=record.at))
        elif SERVER_START_RE.match(message):
            events.append(Event(kind=EventKind.SERVER_START, at=record.at))
        elif (match := JOIN_RE.match(message)) is not None:
            events.append(Event(kind=EventKind.JOIN, at=record.at, player=match["player"]))
        elif (match := LEAVE_RE.match(message)) is not None:
            events.append(Event(kind=EventKind.LEAVE, at=record.at, player=match["player"]))
        elif (match := LOST_RE.match(message)) is not None:
            events.append(
                Event(
                    kind=EventKind.DISCONNECT,
                    at=record.at,
                    player=match["player"],
                    detail=match["reason"],
                )
            )
        elif (match := ADVANCEMENT_RE.match(message)) is not None:
            events.append(
                Event(
                    kind=EventKind.ADVANCEMENT,
                    at=record.at,
                    player=match["player"],
                    detail=match["title"],
                )
            )
        elif (match := CHAT_RE.match(message)) is not None:
            events.append(
                Event(
                    kind=EventKind.CHAT,
                    at=record.at,
                    player=match["player"],
                    detail=match["text"],
                )
            )
        elif (match := SERVER_SAY_RE.match(message)) is not None:
            events.append(
                Event(
                    kind=EventKind.CHAT, at=record.at, player="Server", detail=match["text"]
                )
            )
        elif (match := MOB_DEATH_RE.match(message)) is not None:
            # A village drowning in the same spot over and over is a broken
            # village, not noise -- but it is an aggregate, not a headline.
            cause = match["cause"].split(" ", 1)[-1]
            where = f"{float(match['x']):.0f},{float(match['y']):.0f},{float(match['z']):.0f}"
            events.append(
                Event(
                    kind=EventKind.MOB_DEATH,
                    at=record.at,
                    player=match["kind"],
                    detail=f"{cause} @ {where}",
                )
            )
        else:
            first, _, rest = message.partition(" ")
            if first in known and rest and DEATH_RE.search(rest):
                events.append(
                    Event(kind=EventKind.DEATH, at=record.at, player=first, detail=rest)
                )
    return events
