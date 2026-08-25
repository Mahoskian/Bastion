"""A time series of what the server is doing, in SQLite.

GC logs roll away after ~50M and server logs are not backed up, so anything
worth comparing across weeks -- did the smaller heap help? was last Tuesday
actually worse? -- has to be copied out of them before they roll. This is that
copy: cheap samples taken on a schedule, plus one row per JVM run holding the
GC analysis that outlives the log it came from.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import process
from .gclog import Analysis, latest_heap
from .models import Paths, ServerState

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
PLAYER_COUNT_RE = re.compile(r"There are (?P<online>\d+) of a max of (?P<max>\d+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    state       TEXT NOT NULL,
    players     INTEGER,
    max_players INTEGER,
    rss_bytes   INTEGER,
    cpu_seconds REAL,
    heap_used   INTEGER,
    heap_max    INTEGER,
    world_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS samples_at ON samples (at);

CREATE TABLE IF NOT EXISTS gc_runs (
    run_started TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    seconds     REAL,
    heap_max    INTEGER,
    collections INTEGER,
    full_gcs    INTEGER,
    live_bytes  INTEGER,
    alloc_rate  REAL,
    promo_rate  REAL,
    p50_ms      REAL,
    p95_ms      REAL,
    p99_ms      REAL,
    max_ms      REAL,
    gc_overhead REAL,
    recommended INTEGER
);
"""


class Sample(BaseModel):
    """One point in time."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    state: ServerState
    players: int | None = None
    max_players: int | None = None
    rss_bytes: int | None = None
    cpu_seconds: float | None = None
    heap_used: int | None = None
    heap_max: int | None = None
    world_bytes: int | None = None


class GcRecord(BaseModel):
    """One JVM run's GC analysis, kept past the log's rotation."""

    model_config = ConfigDict(frozen=True)

    run_started: datetime
    recorded_at: datetime
    seconds: float = 0.0
    heap_max: int = 0
    collections: int = 0
    full_gcs: int = 0
    live_bytes: int = 0
    alloc_rate: float = 0.0
    promo_rate: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    gc_overhead: float = 0.0
    recommended: int = 0

    @classmethod
    def from_analysis(cls, analysis: Analysis) -> GcRecord:
        percentiles = analysis.pause_percentiles
        return cls(
            run_started=analysis.run.started.replace(tzinfo=None),
            recorded_at=datetime.now(),
            seconds=analysis.run.seconds,
            heap_max=analysis.run.heap_max,
            collections=len(analysis.run.pauses),
            full_gcs=sum(1 for pause in analysis.run.pauses if pause.kind == "Full"),
            live_bytes=analysis.live_bytes,
            alloc_rate=analysis.alloc_rate,
            promo_rate=analysis.promo_rate,
            p50_ms=percentiles["p50"],
            p95_ms=percentiles["p95"],
            p99_ms=percentiles["p99"],
            max_ms=percentiles["max"],
            gc_overhead=analysis.gc_overhead,
            recommended=analysis.recommended,
        )


class Series(BaseModel):
    """One metric over time, ready to plot."""

    model_config = ConfigDict(frozen=True)

    name: str
    unit: str = ""
    points: list[tuple[datetime, float]] = []

    @property
    def values(self) -> list[float]:
        return [value for _, value in self.points]

    @property
    def latest(self) -> float | None:
        return self.points[-1][1] if self.points else None

    @property
    def peak(self) -> float | None:
        return max(self.values) if self.points else None

    @property
    def mean(self) -> float | None:
        return sum(self.values) / len(self.values) if self.points else None


class MetricsStore:
    """The SQLite file behind `mc metrics`."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Paths.from_env().metrics_db
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        return connection

    # ------------------------------------------------------------- write

    def add(self, sample: Sample) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO samples
                    (at, state, players, max_players, rss_bytes, cpu_seconds,
                     heap_used, heap_max, world_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.at.isoformat(),
                    sample.state.value,
                    sample.players,
                    sample.max_players,
                    sample.rss_bytes,
                    sample.cpu_seconds,
                    sample.heap_used,
                    sample.heap_max,
                    sample.world_bytes,
                ),
            )
            connection.commit()

    def record_gc(self, record: GcRecord) -> None:
        """Upsert by run: re-recording a run in progress refreshes its row."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO gc_runs (
                    run_started, recorded_at, seconds, heap_max, collections, full_gcs,
                    live_bytes, alloc_rate, promo_rate, p50_ms, p95_ms, p99_ms, max_ms,
                    gc_overhead, recommended
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_started) DO UPDATE SET
                    recorded_at = excluded.recorded_at,
                    seconds = excluded.seconds,
                    collections = excluded.collections,
                    full_gcs = excluded.full_gcs,
                    live_bytes = excluded.live_bytes,
                    alloc_rate = excluded.alloc_rate,
                    promo_rate = excluded.promo_rate,
                    p50_ms = excluded.p50_ms,
                    p95_ms = excluded.p95_ms,
                    p99_ms = excluded.p99_ms,
                    max_ms = excluded.max_ms,
                    gc_overhead = excluded.gc_overhead,
                    recommended = excluded.recommended
                """,
                (
                    record.run_started.isoformat(),
                    record.recorded_at.isoformat(),
                    record.seconds,
                    record.heap_max,
                    record.collections,
                    record.full_gcs,
                    record.live_bytes,
                    record.alloc_rate,
                    record.promo_rate,
                    record.p50_ms,
                    record.p95_ms,
                    record.p99_ms,
                    record.max_ms,
                    record.gc_overhead,
                    record.recommended,
                ),
            )
            connection.commit()

    def prune(self, older_than: timedelta) -> int:
        cutoff = (datetime.now() - older_than).isoformat()
        with closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM samples WHERE at < ?", (cutoff,))
            connection.commit()
            return cursor.rowcount

    # ------------------------------------------------------------- read

    def samples(self, since: datetime | None = None) -> list[Sample]:
        query = "SELECT * FROM samples"
        params: tuple = ()
        if since is not None:
            query += " WHERE at >= ?"
            params = (since.isoformat(),)
        query += " ORDER BY at"
        with closing(self._connect()) as connection:
            return [
                Sample(
                    at=datetime.fromisoformat(row["at"]),
                    state=ServerState(row["state"]),
                    players=row["players"],
                    max_players=row["max_players"],
                    rss_bytes=row["rss_bytes"],
                    cpu_seconds=row["cpu_seconds"],
                    heap_used=row["heap_used"],
                    heap_max=row["heap_max"],
                    world_bytes=row["world_bytes"],
                )
                for row in connection.execute(query, params)
            ]

    def gc_runs(self, limit: int = 25) -> list[GcRecord]:
        """Newest first."""
        with closing(self._connect()) as connection:
            return [
                GcRecord(
                    run_started=datetime.fromisoformat(row["run_started"]),
                    recorded_at=datetime.fromisoformat(row["recorded_at"]),
                    **{
                        key: row[key]
                        for key in GcRecord.model_fields
                        if key not in ("run_started", "recorded_at")
                    },
                )
                for row in connection.execute(
                    "SELECT * FROM gc_runs ORDER BY run_started DESC LIMIT ?", (limit,)
                )
            ]

    def count(self) -> int:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]


def cpu_percent(samples: Iterable[Sample]) -> Series:
    """CPU is cumulative in /proc, so a rate needs two samples to exist."""
    points: list[tuple[datetime, float]] = []
    previous: Sample | None = None
    for sample in samples:
        if (
            previous is not None
            and sample.cpu_seconds is not None
            and previous.cpu_seconds is not None
        ):
            elapsed = (sample.at - previous.at).total_seconds()
            used = sample.cpu_seconds - previous.cpu_seconds
            # A restart resets the counter; a negative delta is not a rate.
            if elapsed > 0 and used >= 0:
                points.append((sample.at, 100.0 * used / elapsed))
        previous = sample
    return Series(name="cpu", unit="%", points=points)


def series(samples: Iterable[Sample], field: str, unit: str = "") -> Series:
    points = [
        (sample.at, float(getattr(sample, field)))
        for sample in samples
        if getattr(sample, field) is not None
    ]
    return Series(name=field, unit=unit, points=points)


class Sampler:
    """Gathers one Sample. Every source is cheap enough to run every minute."""

    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or Paths.from_env()

    def _proc(self, pid: int) -> tuple[int | None, float | None]:
        """(RSS bytes, cumulative CPU seconds) from /proc."""
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text().split()
            resident = int((Path("/proc") / str(pid) / "statm").read_text().split()[1])
        except (OSError, IndexError, ValueError):
            return None, None
        try:
            cpu = (int(fields[13]) + int(fields[14])) / CLOCK_TICKS
        except (IndexError, ValueError):
            cpu = None
        return resident * PAGE_SIZE, cpu

    def _players(self) -> tuple[int | None, int | None]:
        from .controller import ServerController

        listing = ServerController(self.paths).players()
        if not listing:
            return None, None
        match = PLAYER_COUNT_RE.search(listing)
        return (int(match["online"]), int(match["max"])) if match else (None, None)

    def _world_bytes(self) -> int | None:
        try:
            result = subprocess.run(
                ["du", "-sb", str(self.paths.server_dir / "world")],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        first = result.stdout.split("\t", 1)[0] if result.stdout else ""
        return int(first) if first.isdigit() else None

    def take(self, world_size: bool = True) -> Sample:
        from .controller import ServerController

        state = ServerController(self.paths).state()
        pid = process.server_pid()
        rss, cpu = self._proc(pid) if pid else (None, None)
        heap = latest_heap(self.paths.logs_dir / "gc.log") if state.is_live else None
        online, maximum = self._players() if state is ServerState.RUNNING else (None, None)
        return Sample(
            at=datetime.now().replace(microsecond=0),
            state=state,
            players=online,
            max_players=maximum,
            rss_bytes=rss,
            cpu_seconds=cpu,
            heap_used=heap[0] if heap else None,
            heap_max=heap[1] if heap else None,
            world_bytes=self._world_bytes() if world_size else None,
        )
