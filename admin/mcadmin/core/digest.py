"""Distilling parsed log records into what is worth reading.

With 292 mods the log is mostly the same handful of complaints repeated
thousands of times. The useful question is not "what went wrong" but "what
went wrong that I have not already seen", so every problem entry is
fingerprinted and remembered; a digest reports the ones that are new.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .logs import Event, EventKind, Level, Record, extract_events

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    fingerprint TEXT PRIMARY KEY,
    level       TEXT NOT NULL,
    sample      TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
"""


class Problem(BaseModel):
    """One kind of WARN/ERROR entry, however many times it occurred."""

    model_config = ConfigDict(frozen=True)

    fingerprint: str
    level: Level
    count: int
    sample: str
    thread: str = ""
    first_seen: datetime
    last_seen: datetime


class Session(BaseModel):
    """One player's stay on the server."""

    model_config = ConfigDict(frozen=True)

    player: str
    joined: datetime
    left: datetime | None = None

    @property
    def still_online(self) -> bool:
        return self.left is None

    def duration(self, now: datetime | None = None) -> timedelta:
        return (self.left or now or datetime.now()) - self.joined


class Digest(BaseModel):
    """Everything a digest reports."""

    model_config = ConfigDict(frozen=True)

    start: datetime | None = None
    end: datetime | None = None
    records: int = 0
    problem_records: int = 0
    sessions: list[Session] = []
    deaths: list[Event] = []
    advancements: list[Event] = []
    chat: list[Event] = []
    disconnects: list[Event] = []
    mob_deaths: dict[str, int] = {}
    new_problems: list[Problem] = []
    recurring: list[Problem] = []
    baseline_size: int = 0
    learned: bool = False  # this run established the baseline rather than using it

    @property
    def distinct_problems(self) -> int:
        return len(self.new_problems) + len(self.recurring)

    def playtime(self, now: datetime | None = None) -> dict[str, timedelta]:
        totals: dict[str, timedelta] = {}
        for session in self.sessions:
            totals[session.player] = totals.get(session.player, timedelta()) + session.duration(now)
        return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


class Baseline:
    """Fingerprints already reported, in SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.executescript(SCHEMA)
        return connection

    def known(self) -> set[str]:
        with closing(self._connect()) as connection:
            return {row[0] for row in connection.execute("SELECT fingerprint FROM seen")}

    def size(self) -> int:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def remember(self, problems: Iterable[Problem]) -> None:
        """Record these as seen, accumulating counts for ones already known."""
        rows = [
            (
                problem.fingerprint,
                problem.level.value,
                problem.sample,
                problem.count,
                problem.first_seen.isoformat(),
                problem.last_seen.isoformat(),
            )
            for problem in problems
        ]
        if not rows:
            return
        with closing(self._connect()) as connection:
            connection.executemany(
                """
                INSERT INTO seen (fingerprint, level, sample, total, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    total = total + excluded.total,
                    last_seen = MAX(last_seen, excluded.last_seen)
                """,
                rows,
            )
            connection.commit()

    def reset(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM seen")
            connection.commit()


def collect_problems(records: Iterable[Record]) -> dict[str, Problem]:
    """Group WARN/ERROR/FATAL records by fingerprint."""
    grouped: dict[str, Problem] = {}
    for record in records:
        if not record.level.is_problem:
            continue
        key = record.fingerprint
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = Problem(
                fingerprint=key,
                level=record.level,
                count=1,
                sample=record.summary,
                thread=record.thread,
                first_seen=record.at,
                last_seen=record.at,
            )
        else:
            grouped[key] = existing.model_copy(
                update={
                    "count": existing.count + 1,
                    "last_seen": max(existing.last_seen, record.at),
                    "first_seen": min(existing.first_seen, record.at),
                }
            )
    return grouped


def build_sessions(events: Iterable[Event]) -> list[Session]:
    """Pair joins with leaves.

    A shutdown never emits "left the game" for whoever was online, so a stop --
    or a start, which implies the previous run ended however messily -- closes
    every open session. Without that, a session orphaned by a restart runs to
    the present and playtime exceeds the window it was measured in.
    """
    open_join: dict[str, datetime] = {}
    sessions: list[Session] = []
    last_at: datetime | None = None

    def close_all(when: datetime) -> None:
        for player, joined in open_join.items():
            sessions.append(Session(player=player, joined=joined, left=when))
        open_join.clear()

    for event in sorted(events, key=lambda e: e.at):
        if event.kind is EventKind.SERVER_STOP:
            close_all(event.at)
        elif event.kind is EventKind.SERVER_START:
            # A crash leaves no stop marker; close at the last thing logged
            # before the restart rather than at the restart itself.
            close_all(last_at or event.at)
        elif event.kind is EventKind.JOIN:
            if event.player in open_join:  # a join with no matching leave
                sessions.append(Session(player=event.player, joined=open_join[event.player]))
            open_join[event.player] = event.at
        elif event.kind is EventKind.LEAVE and event.player in open_join:
            sessions.append(
                Session(player=event.player, joined=open_join.pop(event.player), left=event.at)
            )
        last_at = event.at

    for player, joined in open_join.items():
        sessions.append(Session(player=player, joined=joined))
    return sorted(sessions, key=lambda s: s.joined)


def build(
    records: list[Record],
    baseline: Baseline,
    recurring_limit: int = 5,
) -> Digest:
    """Digest these records against what the baseline has already seen."""
    if not records:
        return Digest(baseline_size=baseline.size())

    known = baseline.known()
    learned = not known
    problems = collect_problems(records)
    new = [problem for key, problem in problems.items() if key not in known]
    seen_before = [problem for key, problem in problems.items() if key in known]

    events = extract_events(records)
    by_kind: dict[EventKind, list[Event]] = {}
    for event in events:
        by_kind.setdefault(event.kind, []).append(event)

    mob_deaths = Counter(
        event.detail for event in by_kind.get(EventKind.MOB_DEATH, [])
    )

    return Digest(
        start=records[0].at,
        end=records[-1].at,
        records=len(records),
        problem_records=sum(problem.count for problem in problems.values()),
        sessions=build_sessions(events),
        deaths=by_kind.get(EventKind.DEATH, []),
        advancements=by_kind.get(EventKind.ADVANCEMENT, []),
        chat=by_kind.get(EventKind.CHAT, []),
        disconnects=by_kind.get(EventKind.DISCONNECT, []),
        mob_deaths=dict(mob_deaths.most_common()),
        new_problems=sorted(new, key=lambda p: (-p.count, p.sample)),
        recurring=sorted(seen_before, key=lambda p: -p.count)[:recurring_limit],
        baseline_size=len(known),
        learned=learned,
    )
