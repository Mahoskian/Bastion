"""Why the server was slow at a particular time.

Four records of the same minutes exist and none of them answers the question
alone: the server log knows when ticks fell behind, the GC log knows when the
JVM stopped the world, the join/leave events know who was on, and Chunky's own
progress lines know when a pregeneration was eating the tick. This lines all
four up on one clock and says which of them was big enough to explain the rest.

The arithmetic that makes the answer worth anything is comparing *lost tick
time* against *GC pause time in the same window*. If the server skipped 17
seconds of ticks and G1 paused for 40ms, the heap did not do it -- and until
the two numbers sit side by side that is surprisingly hard to be sure of.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

# `Finding` and its severity are the shape every analysis in this package
# reports in; sharing them keeps `--json` and the terminal consistent.
from .gclog import Finding, Level, Run
from .logs import EventKind, Record, extract_events
from .metrics import Sample
from .units import human_seconds

# [Server thread/WARN]: Can't keep up! Is the server overloaded? Running 2007ms
# or 40 ticks behind
BEHIND_RE = re.compile(
    r"Can't keep up!.*?Running (?P<ms>\d+)ms or (?P<ticks>\d+) ticks behind"
)
# [Chunky] Task running for minecraft:overworld. Processed: 200373 chunks
# (50.97%), ETA: 0:40:58, Rate: 78.4 cps, Current: -194, -230
CHUNKY_RE = re.compile(
    r"^\[Chunky\] Task running for (?P<dimension>\S+?)\. Processed: (?P<chunks>\d+) chunks "
    r"\((?P<percent>[\d.]+)%\).*?Rate: (?P<rate>[\d.]+) cps"
)

DEFAULT_BUCKET = timedelta(minutes=10)
# The server only logs "Can't keep up!" once every 15 seconds however far
# behind it falls, so summed lag is a floor on time lost, never a total.
WARNING_COOLDOWN = timedelta(seconds=15)
# Below this a bucket is not worth explaining -- a single 2s hiccup in ten
# minutes is a hiccup.
INTERESTING_MS = 2000
# GC has to account for this much of the lost time before it is the answer
# rather than a bystander.
GC_BLAME = 0.5
# A pregen at this rate is saturating a core with chunk generation; Chunky's own
# default throttle is well under it.
PREGEN_BUSY_CPS = 20.0


class Bucket(BaseModel):
    """One slice of the clock, with every source's account of it."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    lag_events: int = 0
    lag_ms: int = 0  # summed; a floor, because of the warning cooldown
    worst_lag_ms: int = 0

    gc_pauses: int = 0
    gc_ms: float = 0.0
    worst_gc_ms: float = 0.0
    full_gcs: int = 0
    non_gc_stw_ms: float = 0.0

    joins: int = 0
    leaves: int = 0
    players_peak: int = 0

    pregen_chunks: int = 0
    pregen_rate: float = 0.0
    pregen_dimension: str = ""

    problems: int = 0
    errors: int = 0

    heap_used: int | None = None
    # False when no GC log covers these minutes. "G1 paused for 0ms" and "we
    # have no idea what G1 did" are opposite answers to "was it the heap", and
    # the GC logs rotate long before the server logs do.
    gc_known: bool = True

    @property
    def lost_ms(self) -> float:
        """Tick time the server did not get, from either source."""
        return self.lag_ms + self.gc_ms

    @property
    def interesting(self) -> bool:
        return self.lost_ms >= INTERESTING_MS

    @property
    def gc_share(self) -> float:
        """How much of the lost time G1 can account for."""
        return self.gc_ms / self.lost_ms if self.lost_ms else 0.0

    @property
    def label(self) -> str:
        return f"{self.start:%m-%d %H:%M}"


class Timeline(BaseModel):
    """Buckets over a window, and which of them was worst."""

    model_config = ConfigDict(frozen=True)

    buckets: list[Bucket] = []
    size: timedelta = DEFAULT_BUCKET
    start: datetime | None = None
    end: datetime | None = None
    gc_covered: bool = True  # the GC logs actually reach back this far

    @property
    def worst(self) -> Bucket | None:
        candidates = [bucket for bucket in self.buckets if bucket.lost_ms > 0]
        return max(candidates, key=lambda bucket: bucket.lost_ms) if candidates else None

    @property
    def bad(self) -> list[Bucket]:
        """Every bucket worth explaining, worst first."""
        return sorted(
            (bucket for bucket in self.buckets if bucket.interesting),
            key=lambda bucket: -bucket.lost_ms,
        )

    @property
    def lost_ms(self) -> float:
        return sum(bucket.lost_ms for bucket in self.buckets)

    def at(self, moment: datetime) -> Bucket | None:
        for bucket in self.buckets:
            if bucket.start <= moment < bucket.end:
                return bucket
        return None

    def around(self, bucket: Bucket, span: int = 2) -> list[Bucket]:
        """The bucket and its neighbours, for context either side."""
        index = self.buckets.index(bucket)
        return self.buckets[max(0, index - span) : index + span + 1]


def gc_spans(runs: list[Run]) -> list[tuple[datetime, datetime]]:
    """The wall-clock stretches the GC logs actually have something to say about."""
    spans: list[tuple[datetime, datetime]] = []
    for run in runs:
        stamps = [pause.at for pause in run.pauses]
        stamps += [point.at for point in run.safepoints]
        if stamps:
            spans.append(
                (min(stamps).replace(tzinfo=None), max(stamps).replace(tzinfo=None))
            )
    return spans


def _floor(moment: datetime, size: timedelta, origin: datetime) -> datetime:
    elapsed = (moment - origin) // size
    return origin + elapsed * size


def _peak_players(sessions: list, start: datetime, end: datetime) -> int:
    """The most players online at once inside a window.

    Counting joins would undercount an evening where everyone was already on,
    so this counts overlapping sessions instead.
    """
    edges = sorted(
        {session.joined for session in sessions if start <= session.joined < end}
        | {start}
    )
    peak = 0
    for edge in edges:
        here = sum(
            1
            for session in sessions
            if session.joined <= edge and (session.left is None or session.left > edge)
        )
        peak = max(peak, here)
    return peak


def build(
    records: list[Record],
    runs: list[Run] | None = None,
    samples: list[Sample] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    size: timedelta = DEFAULT_BUCKET,
) -> Timeline:
    """Line every source up on the same clock."""
    from .digest import build_sessions

    windowed = [
        record
        for record in records
        if (since is None or record.at >= since) and (until is None or record.at < until)
    ]
    if not windowed:
        return Timeline(size=size, start=since, end=until)

    start = since or windowed[0].at
    end = until or windowed[-1].at
    origin = _floor(start, size, datetime(start.year, start.month, start.day))
    edges: list[datetime] = []
    cursor = origin
    while cursor <= end:
        edges.append(cursor)
        cursor += size

    totals: dict[datetime, dict] = {
        edge: {
            "lag_events": 0,
            "lag_ms": 0,
            "worst_lag_ms": 0,
            "gc_pauses": 0,
            "gc_ms": 0.0,
            "worst_gc_ms": 0.0,
            "full_gcs": 0,
            "non_gc_stw_ms": 0.0,
            "joins": 0,
            "leaves": 0,
            "pregen_first": None,
            "pregen_last": None,
            "pregen_rate": 0.0,
            "pregen_dimension": "",
            "problems": 0,
            "errors": 0,
            "heap": [],
        }
        for edge in edges
    }

    def slot(moment: datetime) -> dict | None:
        edge = _floor(moment, size, origin)
        return totals.get(edge)

    for record in windowed:
        here = slot(record.at)
        if here is None:
            continue
        if record.level.is_problem:
            here["problems"] += 1
            if record.level.value in ("ERROR", "FATAL"):
                here["errors"] += 1
        behind = BEHIND_RE.search(record.message)
        if behind is not None:
            lost = int(behind["ms"])
            here["lag_events"] += 1
            here["lag_ms"] += lost
            here["worst_lag_ms"] = max(here["worst_lag_ms"], lost)
            continue
        pregen = CHUNKY_RE.match(record.message)
        if pregen is not None:
            processed = int(pregen["chunks"])
            if here["pregen_first"] is None:
                here["pregen_first"] = processed
            here["pregen_last"] = processed
            here["pregen_rate"] = max(here["pregen_rate"], float(pregen["rate"]))
            here["pregen_dimension"] = pregen["dimension"]

    # Sessions come from *every* record: a player who joined before the window
    # opened is still online inside it, and extracting from the window alone
    # would report an empty server.
    events = extract_events(records)
    for event in events:
        here = slot(event.at)
        if here is None:
            continue
        if event.kind is EventKind.JOIN:
            here["joins"] += 1
        elif event.kind is EventKind.LEAVE:
            here["leaves"] += 1

    spans = gc_spans(runs or [])
    for run in runs or []:
        for pause in run.pauses:
            moment = pause.at.replace(tzinfo=None)
            here = slot(moment)
            if here is None:
                continue
            here["gc_pauses"] += 1
            here["gc_ms"] += pause.ms
            here["worst_gc_ms"] = max(here["worst_gc_ms"], pause.ms)
            here["full_gcs"] += pause.kind == "Full"
        for point in run.safepoints:
            if point.is_gc:
                continue
            here = slot(point.at.replace(tzinfo=None))
            if here is not None:
                here["non_gc_stw_ms"] += point.total_ns / 1e6

    for sample in samples or []:
        here = slot(sample.at)
        if here is None:
            continue
        if sample.heap_used is not None:
            here["heap"].append(sample.heap_used)

    sessions = build_sessions(events)
    buckets = [
        Bucket(
            start=edge,
            end=edge + size,
            lag_events=data["lag_events"],
            lag_ms=data["lag_ms"],
            worst_lag_ms=data["worst_lag_ms"],
            gc_pauses=data["gc_pauses"],
            gc_ms=data["gc_ms"],
            worst_gc_ms=data["worst_gc_ms"],
            full_gcs=data["full_gcs"],
            non_gc_stw_ms=data["non_gc_stw_ms"],
            joins=data["joins"],
            leaves=data["leaves"],
            players_peak=_peak_players(sessions, edge, edge + size),
            pregen_chunks=(
                data["pregen_last"] - data["pregen_first"]
                if data["pregen_first"] is not None
                else 0
            ),
            pregen_rate=data["pregen_rate"],
            pregen_dimension=data["pregen_dimension"],
            problems=data["problems"],
            errors=data["errors"],
            gc_known=any(
                begin < edge + size and finish > edge for begin, finish in spans
            ),
            heap_used=(
                round(sum(data["heap"]) / len(data["heap"])) if data["heap"] else None
            ),
        )
        for edge, data in totals.items()
    ]
    return Timeline(
        buckets=buckets,
        size=size,
        start=start,
        end=end,
        gc_covered=any(bucket.gc_known for bucket in buckets),
    )


def explain(bucket: Bucket) -> list[Finding]:
    """Say what was happening, strongest evidence first.

    Every claim here is a comparison between two measured numbers rather than a
    rule of thumb: the point of the command is to rule causes *out*.
    """
    found: list[Finding] = []

    if not bucket.lost_ms:
        found.append(
            Finding(
                message=(
                    "Nothing lost tick time in this window -- no overload warnings "
                    "and no GC pauses. Whatever felt slow was not the server thread."
                )
            )
        )
        return found

    if bucket.lag_ms:
        found.append(
            Finding(
                level=Level.ERROR if bucket.lag_ms > 10_000 else Level.WARN,
                message=(
                    f"The server skipped at least {human_seconds(bucket.lag_ms / 1000)} "
                    f"of ticks across {bucket.lag_events} overload warning(s), worst "
                    f"{bucket.worst_lag_ms / 1000:.1f}s. It only warns once every "
                    f"{WARNING_COOLDOWN.seconds}s, so the real figure is higher."
                ),
            )
        )

    if bucket.pregen_chunks or bucket.pregen_rate:
        level = Level.WARN if bucket.pregen_rate >= PREGEN_BUSY_CPS else Level.INFO
        found.append(
            Finding(
                level=level,
                message=(
                    f"A pregeneration was running in {bucket.pregen_dimension}: "
                    f"{bucket.pregen_chunks:,} chunks in this window, peaking at "
                    f"{bucket.pregen_rate:.0f} chunks/s. Chunk generation runs on the "
                    "server thread's back, and this is the loudest thing in the window."
                ),
            )
        )

    if not bucket.gc_known:
        found.append(
            Finding(
                level=Level.WARN,
                message=(
                    "No GC log covers this window -- it has rotated away, so the heap "
                    "can be neither blamed nor cleared. `mc metrics record` keeps the "
                    "per-run summary before that happens."
                ),
            )
        )
    elif bucket.full_gcs:
        found.append(
            Finding(
                level=Level.ERROR,
                message=(
                    f"{bucket.full_gcs} full GC(s) -- a stop-the-world compaction, "
                    "which is never routine."
                ),
            )
        )
    elif bucket.gc_share >= GC_BLAME:
        found.append(
            Finding(
                level=Level.WARN,
                message=(
                    f"G1 paused for {bucket.gc_ms:.0f}ms here, {bucket.gc_share * 100:.0f}% "
                    f"of the {bucket.lost_ms / 1000:.1f}s lost. This one is the heap: "
                    "run `mc gc` against the same run."
                ),
            )
        )
    elif bucket.lag_ms:
        found.append(
            Finding(
                message=(
                    f"Not the heap: G1 paused for only {bucket.gc_ms:.0f}ms across "
                    f"{bucket.gc_pauses} collection(s), against "
                    f"{bucket.lag_ms / 1000:.1f}s of skipped ticks."
                ),
            )
        )

    if bucket.non_gc_stw_ms > max(50.0, bucket.gc_ms):
        found.append(
            Finding(
                level=Level.WARN,
                message=(
                    f"{bucket.non_gc_stw_ms:.0f}ms went on non-GC safepoints -- the JVM "
                    "stopping the world for something other than collecting."
                ),
            )
        )

    if bucket.joins:
        found.append(
            Finding(
                message=(
                    f"{bucket.joins} player(s) joined ({bucket.players_peak} on at once). "
                    "A join loads that player's chunks on the spot."
                )
            )
        )
    elif bucket.players_peak:
        found.append(
            Finding(message=f"{bucket.players_peak} player(s) were online throughout.")
        )

    if bucket.errors:
        found.append(
            Finding(
                level=Level.WARN if bucket.errors > 10 else Level.INFO,
                message=(
                    f"{bucket.errors} error(s) logged in the same window "
                    f"({bucket.problems} warnings and errors together) -- "
                    "`mc logs digest` says what they were."
                ),
            )
        )
    return found


def as_dict(timeline: Timeline, focus: Bucket | None = None) -> dict:
    """Machine-readable form for --json."""
    return {
        "start": timeline.start.isoformat() if timeline.start else None,
        "end": timeline.end.isoformat() if timeline.end else None,
        "bucket_seconds": int(timeline.size.total_seconds()),
        "gc_covered": timeline.gc_covered,
        "lost_ms": round(timeline.lost_ms),
        "focus": focus.start.isoformat() if focus else None,
        "findings": [
            {"level": finding.level.value, "message": finding.message}
            for finding in (explain(focus) if focus else [])
        ],
        "buckets": [
            {
                "start": bucket.start.isoformat(),
                "lag_events": bucket.lag_events,
                "lag_ms": bucket.lag_ms,
                "worst_lag_ms": bucket.worst_lag_ms,
                "gc_pauses": bucket.gc_pauses,
                "gc_ms": round(bucket.gc_ms, 1),
                "full_gcs": bucket.full_gcs,
                "non_gc_stw_ms": round(bucket.non_gc_stw_ms, 1),
                "joins": bucket.joins,
                "players_peak": bucket.players_peak,
                "pregen_chunks": bucket.pregen_chunks,
                "pregen_rate": bucket.pregen_rate,
                "problems": bucket.problems,
                "errors": bucket.errors,
                "heap_used": bucket.heap_used,
                "lost_ms": round(bucket.lost_ms),
            }
            for bucket in timeline.buckets
        ],
    }
