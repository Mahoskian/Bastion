"""Parsing G1 logs and sizing the heap from what they measured.

`JvmOptions` writes rotating `-Xlog:gc*` files precisely so the heap can be sized
from data instead of guesswork; this reads them back. The interesting numbers
are the *live set* (what survives a collection, i.e. what the heap must
actually hold), the allocation rate, and the pause distribution.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import process
from .models import Paths
from .units import human_bytes, human_seconds

# [2026-08-24T18:01:57.536-0400][1988.858s][info ][gc          ] GC(72) Pause ...
LINE_RE = re.compile(
    r"^\[(?P<time>[^\]]+)\]\[(?P<uptime>[\d.]+)s\]\[(?P<level>\w+)\s*\]\[(?P<tags>[\w,]+)\s*\]\s?"
    r"(?P<body>.*)$"
)

# GC(72) Pause Young (Mixed) (G1 Evacuation Pause) 5963M->1047M(12288M) 5.395ms
PAUSE_RE = re.compile(
    r"^GC\((?P<id>\d+)\) Pause (?P<kind>\w+) (?P<parens>.*?)\s*"
    r"(?P<before>\d+)(?P<bu>[BKMG])->(?P<after>\d+)(?P<au>[BKMG])"
    r"\((?P<cap>\d+)(?P<cu>[BKMG])\) (?P<ms>[\d.]+)ms$"
)

# GC(72) Eden regions: 613->0(613)
REGION_RE = re.compile(
    r"^GC\((?P<id>\d+)\) (?P<space>Eden|Survivor|Old|Humongous) regions: "
    r"(?P<before>\d+)->(?P<after>\d+)(?:\((?P<target>\d+)\))?$"
)

SAFEPOINT_RE = re.compile(
    r'^Safepoint "(?P<name>[^"]+)", Time since last: (?P<since>\d+) ns, '
    r"Reaching safepoint: (?P<reach>\d+) ns, At safepoint: \d+ ns, "
    r"Leaving safepoint: \d+ ns, Total: (?P<total>\d+) ns"
)

HEAP_SUMMARY_RE = re.compile(
    r"(?P<before>\d+)(?P<bu>[BKMG])->(?P<after>\d+)(?P<au>[BKMG])"
    r"\((?P<cap>\d+)(?P<cu>[BKMG])\)"
)

CONCURRENT_CYCLE_RE = re.compile(r"^GC\(\d+\) Concurrent Mark Cycle [\d.]+ms$")

INIT_RE = re.compile(r"^(?P<key>[A-Z][\w \-]*): (?P<value>.+)$")

UNITS = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}

# G1 does its own collections at a safepoint; anything else stopping the world
# is worth separating out, because it stalls ticks without showing up as GC.
GC_SAFEPOINTS = ("G1", "CollectFor")


def _size(value: str, unit: str) -> int:
    return int(value) * UNITS[unit]


def _paren_groups(text: str) -> list[str]:
    """Split "(Mixed) (System.gc())" into ['Mixed', 'System.gc()'].

    Causes can themselves contain parens, so this tracks depth rather than
    using a regex.
    """
    groups: list[str] = []
    depth = start = 0
    for i, char in enumerate(text):
        if char == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                groups.append(text[start:i])
    return groups


@dataclass
class Pause:
    """One stop-the-world collection."""

    gc_id: int
    at: datetime
    uptime: float
    kind: str  # Young | Remark | Cleanup | Full
    subkind: str  # Normal | Mixed | Concurrent Start | Prepare Mixed | ""
    cause: str
    before: int
    after: int
    capacity: int
    ms: float
    regions: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.kind} ({self.subkind})" if self.subkind else self.kind

    @property
    def reclaimed(self) -> int:
        return max(0, self.before - self.after)

    def region_bytes(self, space: str, region_size: int, when: int = 1) -> int:
        """Bytes in `space` before (when=0) or after (when=1) this pause."""
        pair = self.regions.get(space)
        return 0 if pair is None else pair[when] * region_size


@dataclass
class Safepoint:
    at: datetime
    uptime: float
    name: str
    total_ns: int
    reach_ns: int

    @property
    def is_gc(self) -> bool:
        return self.name.startswith(GC_SAFEPOINTS)


@dataclass
class Run:
    """A single JVM lifetime. Uptime restarting from zero starts a new one."""

    started: datetime
    settings: dict[str, str] = field(default_factory=dict)
    pauses: list[Pause] = field(default_factory=list)
    safepoints: list[Safepoint] = field(default_factory=list)
    concurrent_cycles: int = 0
    last_at: datetime | None = None
    last_uptime: float = 0.0

    @property
    def seconds(self) -> float:
        return self.last_uptime

    @property
    def region_size(self) -> int:
        raw = self.settings.get("Heap Region Size", "8M")
        return _size(raw[:-1], raw[-1])

    @property
    def heap_max(self) -> int:
        raw = self.settings.get("Heap Max Capacity")
        if raw:
            return _size(raw[:-1], raw[-1])
        return self.pauses[-1].capacity if self.pauses else 0


def log_files(directory: Path | None = None) -> list[Path]:
    """GC logs oldest-first.

    Rotation reuses `gc.log.N` cyclically, so the numeric suffix says nothing
    about age -- order by the first timestamp each file actually contains.
    """
    directory = directory or Paths.from_env().logs_dir
    files = sorted(directory.glob("gc.log*"))

    def first_stamp(path: Path) -> str:
        with path.open(errors="replace") as fh:
            for line in fh:
                match = LINE_RE.match(line)
                if match:
                    return match["time"]
        return ""

    return sorted(files, key=first_stamp)


def latest_heap(path: Path | None = None, window: int = 65536) -> tuple[int, int] | None:
    """(used after the last collection, capacity), read from the log's tail.

    Cheap enough to call every minute: it seeks to the end rather than parsing
    the whole file, which the full analysis does.
    """
    path = path or (Paths.from_env().logs_dir / "gc.log")
    if not path.exists():
        return None
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - window))
        tail = handle.read().decode("utf8", errors="replace")
    last = None
    for match in HEAP_SUMMARY_RE.finditer(tail):
        last = match
    if last is None:
        return None
    return (
        _size(last["after"], last["au"]),
        _size(last["cap"], last["cu"]),
    )


def parse(paths: list[Path]) -> list[Run]:
    """Read GC logs into runs, oldest first."""
    runs: list[Run] = []
    current: Run | None = None
    # Region lines for a GC are logged before its summary line, so they are
    # buffered here by GC id and attached when the Pause is created.
    regions: dict[int, dict[str, tuple[int, int]]] = {}

    for path in paths:
        with path.open(errors="replace") as fh:
            for line in fh:
                match = LINE_RE.match(line)
                if not match:
                    continue
                uptime = float(match["uptime"])
                # A restart rewinds uptime; rotation alone does not.
                if current is None or uptime < current.last_uptime - 1.0:
                    current = Run(started=_stamp(match["time"]))
                    runs.append(current)
                    regions = {}
                at = _stamp(match["time"])
                current.last_at, current.last_uptime = at, uptime
                _consume(current, regions, match["tags"], match["body"], at, uptime)

    return runs


def _stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z")


def _consume(
    run: Run,
    regions: dict[int, dict[str, tuple[int, int]]],
    tags: str,
    body: str,
    at: datetime,
    uptime: float,
) -> None:
    if tags == "gc,init":
        init = INIT_RE.match(body)
        if init:
            run.settings[init["key"]] = init["value"]
        return

    if tags == "safepoint":
        point = SAFEPOINT_RE.match(body)
        if point:
            run.safepoints.append(
                Safepoint(at, uptime, point["name"], int(point["total"]), int(point["reach"]))
            )
        return

    if tags.startswith("gc,heap"):
        region = REGION_RE.match(body)
        if region:
            spaces = regions.setdefault(int(region["id"]), {})
            spaces[region["space"]] = (int(region["before"]), int(region["after"]))
        return

    if tags != "gc":
        return

    if CONCURRENT_CYCLE_RE.match(body):
        run.concurrent_cycles += 1
        return

    event = PAUSE_RE.match(body)
    if not event:
        return
    groups = _paren_groups(event["parens"])
    # "Pause Young (Mixed) (G1 Evacuation Pause)" -- subkind then cause.
    # "Pause Full (System.gc())" -- cause only. "Pause Remark" -- neither.
    if event["kind"] == "Young" and len(groups) >= 2:
        subkind, cause = groups[0], groups[1]
    elif groups:
        subkind, cause = "", groups[-1]
    else:
        subkind = cause = ""
    pause = Pause(
        gc_id=int(event["id"]),
        at=at,
        uptime=uptime,
        kind=event["kind"],
        subkind=subkind,
        cause=cause,
        before=_size(event["before"], event["bu"]),
        after=_size(event["after"], event["au"]),
        capacity=_size(event["cap"], event["cu"]),
        ms=float(event["ms"]),
    )
    pause.regions = regions.pop(pause.gc_id, {})
    run.pauses.append(pause)


# ---------------------------------------------------------------- analysis

# A young collection every few seconds is cheap when pauses are short, but the
# heap should still hold more than a few seconds of allocation.
TARGET_YOUNG_INTERVAL = 10.0
# G1 wants room to work above the live set; below ~2x it collects constantly.
LIVE_SET_MULTIPLIER = 3.0
MIN_RECOMMENDED = 2 * 1024**3
# Ignore startup: mod init allocates hard and retains little of it.
WARMUP_SECONDS = 120.0
# Below this a run has not reached steady state; its numbers are not sizing data.
SETTLED_SECONDS = 900.0


class Level(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class Finding(BaseModel):
    """Something worth saying about a run, with no rendering baked in."""

    model_config = ConfigDict(frozen=True)

    level: Level = Level.INFO
    message: str


@dataclass
class Analysis:
    run: Run
    live_bytes: int
    live_source: str
    old_bytes: int
    peak_bytes: int
    alloc_rate: float  # bytes/sec
    promo_rate: float  # bytes/sec
    pause_total_ms: float
    pause_percentiles: dict[str, float]
    worst: Pause | None
    by_label: dict[str, tuple[int, float]]  # label -> (count, total ms)
    causes: dict[str, int]
    stw_total_ms: float
    non_gc_stw_ms: float
    non_gc_worst: dict[str, tuple[int, float]]
    recommended: int
    notes: list[Finding]

    @property
    def gc_overhead(self) -> float:
        return self.pause_total_ms / (self.run.seconds * 1000) if self.run.seconds else 0.0

    @property
    def stw_overhead(self) -> float:
        return self.stw_total_ms / (self.run.seconds * 1000) if self.run.seconds else 0.0

    @property
    def headroom(self) -> float:
        return self.run.heap_max / self.live_bytes if self.live_bytes else 0.0


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank; `values` need not be sorted."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = math.ceil(fraction * len(ordered)) - 1
    return ordered[max(0, min(len(ordered) - 1, index))]


def _live_set(run: Run) -> tuple[int, int, str]:
    """Bytes that survive collection, and where the figure came from.

    Only a mixed collection actually evacuates old regions, so the occupancy
    left after one is the honest answer. Without any, fall back to the low
    water mark and say so -- it will read high.
    """
    settled = [p for p in run.pauses if p.uptime >= WARMUP_SECONDS] or run.pauses
    for kinds, source in (
        (("Mixed",), "after mixed GC"),
        (("Full",), "after full GC"),
    ):
        candidates = [p for p in settled if p.subkind in kinds or p.kind in kinds]
        if candidates:
            best = min(candidates, key=lambda p: p.after)
            old = best.region_bytes("Old", run.region_size) + best.region_bytes(
                "Humongous", run.region_size
            )
            return best.after, old, source
    if not settled:
        return 0, 0, "no collections"
    return min(p.after for p in settled), 0, "heap low-water mark (no mixed GC seen)"


def _rates(run: Run) -> tuple[float, float]:
    """Allocation and promotion rate in bytes/sec."""
    pauses = run.pauses
    if len(pauses) < 2:
        return 0.0, 0.0
    span = pauses[-1].uptime - pauses[0].uptime
    if span <= 0:
        return 0.0, 0.0
    allocated = sum(
        max(0, curr.before - prev.after) for prev, curr in zip(pauses, pauses[1:], strict=False)
    )
    promoted = 0
    for pause in pauses:
        old = pause.regions.get("Old")
        if old and old[1] > old[0]:
            promoted += (old[1] - old[0]) * run.region_size
    return allocated / span, promoted / span


def _recommend(run: Run, live: int, alloc_rate: float) -> int:
    """Smallest heap that keeps the live set comfortable and GCs unhurried."""
    if not live:
        return run.heap_max
    want = max(live * LIVE_SET_MULTIPLIER, live + alloc_rate * TARGET_YOUNG_INTERVAL)
    gib = 1024**3
    rounded = int((want + gib - 1) // gib) * gib
    return max(rounded, MIN_RECOMMENDED)


def jvm_flags(marker: str = process.JAR_MARKER) -> dict[str, str]:
    """Flags of the running server, for spotting flag/heap interactions.

    Empty when the server is down -- every finding that uses these is optional.
    """
    args = next((a for a in process.java_processes().values() if marker in a), None)
    if args is None:
        return {}
    flags: dict[str, str] = {}
    for token in args.split():
        if token.startswith(("-XX:", "-Xm")):
            key, _, value = token.partition("=")
            flags[key] = value or "true"
    return flags


def _findings(
    run: Run,
    live: int,
    recommended: int,
    causes: dict[str, int],
    flags: dict[str, str],
) -> list[Finding]:
    """State what the numbers mean. Severity is data; colour is the UI's job."""
    found: list[Finding] = []
    heap = run.heap_max

    fulls = sum(1 for p in run.pauses if p.kind == "Full")
    if fulls:
        found.append(
            Finding(
                level=Level.ERROR,
                message=(
                    f"{fulls} full GC(s) -- G1 fell back to a stop-the-world compaction. "
                    "That is never routine; the heap was genuinely exhausted."
                ),
            )
        )

    if live and heap:
        ratio = heap / live
        if ratio >= 6:
            found.append(
                Finding(
                    message=(
                        f"Heap is {ratio:.0f}x the live set. It is doing no harm to pause "
                        "times, but with AlwaysPreTouch every committed byte is resident "
                        "from startup."
                    )
                )
            )
        elif ratio < 2:
            found.append(
                Finding(
                    level=Level.WARN,
                    message=(
                        f"Heap is only {ratio:.1f}x the live set -- too tight. G1 will "
                        "collect constantly and eventually fall back to full GCs."
                    ),
                )
            )

    # Shrinking the heap moves the IHOP trigger down with it; if it lands under
    # the live set, marking never stops.
    ihop = flags.get("-XX:InitiatingHeapOccupancyPercent")
    if ihop and live and recommended:
        trigger = recommended * int(ihop) / 100
        if trigger < live * 1.3:
            suggested = min(70, int(live * 2 * 100 / recommended))
            found.append(
                Finding(
                    level=Level.WARN,
                    message=(
                        f"IHOP={ihop}% starts concurrent marking at {human_bytes(int(trigger))} "
                        f"on a {human_bytes(recommended)} heap, below the {human_bytes(live)} "
                        f"live set -- marking would run continuously. Raise it to ~{suggested}% "
                        "if you shrink the heap."
                    ),
                )
            )

    if flags.get("-XX:+AlwaysPreTouch") and heap:
        found.append(
            Finding(
                message=(
                    f"AlwaysPreTouch commits the whole {human_bytes(heap)} at startup, so RSS "
                    "reflects -Xmx rather than actual use -- relevant when anything else "
                    "shares the box."
                )
            )
        )

    metadata = causes.get("Metadata GC Threshold", 0)
    if metadata:
        found.append(
            Finding(
                message=(
                    f"{metadata} collection(s) triggered by Metadata GC Threshold, not by the "
                    "heap -- metaspace growth from the mod set. Setting -XX:MetaspaceSize to "
                    "the steady-state size avoids these entirely."
                )
            )
        )

    # A short run is mostly mod init: the live set has not settled and the
    # allocation rate reflects startup, not play.
    if run.seconds < SETTLED_SECONDS:
        found.append(
            Finding(
                level=Level.WARN,
                message=(
                    f"This run only lasted {human_seconds(run.seconds)} -- the live set and "
                    "allocation rate are still dominated by startup. Size the heap from a "
                    "longer run."
                ),
            )
        )

    if run.concurrent_cycles and run.seconds:
        per_hour = run.concurrent_cycles / (run.seconds / 3600)
        if per_hour > 20:
            found.append(
                Finding(
                    message=(
                        f"{per_hour:.0f} concurrent mark cycles/hour -- high. Marking is cheap "
                        "here but it is constant background CPU."
                    )
                )
            )

    return found


def analyze(run: Run) -> Analysis:
    live, old, source = _live_set(run)
    alloc_rate, promo_rate = _rates(run)
    recommended = _recommend(run, live, alloc_rate)

    durations = [p.ms for p in run.pauses]
    by_label: dict[str, tuple[int, float]] = {}
    causes: dict[str, int] = {}
    for pause in run.pauses:
        count, total = by_label.get(pause.label, (0, 0.0))
        by_label[pause.label] = (count + 1, total + pause.ms)
        if pause.cause:
            causes[pause.cause] = causes.get(pause.cause, 0) + 1

    stw_total_ms = sum(s.total_ns for s in run.safepoints) / 1e6
    non_gc = [s for s in run.safepoints if not s.is_gc]
    non_gc_worst: dict[str, tuple[int, float]] = {}
    for point in non_gc:
        count, total = non_gc_worst.get(point.name, (0, 0.0))
        non_gc_worst[point.name] = (count + 1, total + point.total_ns / 1e6)

    return Analysis(
        run=run,
        live_bytes=live,
        live_source=source,
        old_bytes=old,
        peak_bytes=max((p.before for p in run.pauses), default=0),
        alloc_rate=alloc_rate,
        promo_rate=promo_rate,
        pause_total_ms=sum(durations),
        pause_percentiles={
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
            "max": max(durations, default=0.0),
        },
        worst=max(run.pauses, key=lambda p: p.ms) if run.pauses else None,
        by_label=by_label,
        causes=causes,
        stw_total_ms=stw_total_ms,
        non_gc_stw_ms=sum(s.total_ns for s in non_gc) / 1e6,
        non_gc_worst=non_gc_worst,
        recommended=recommended,
        notes=_findings(run, live, recommended, causes, jvm_flags()),
    )





def as_dict(analysis: Analysis) -> dict:
    """Machine-readable form for --json."""
    run = analysis.run
    return {
        "started": run.started.isoformat(),
        "seconds": round(run.seconds, 1),
        "heap_max": run.heap_max,
        "region_size": run.region_size,
        "collections": len(run.pauses),
        "concurrent_cycles": run.concurrent_cycles,
        "live_bytes": analysis.live_bytes,
        "live_source": analysis.live_source,
        "old_bytes": analysis.old_bytes,
        "peak_bytes": analysis.peak_bytes,
        "alloc_bytes_per_sec": round(analysis.alloc_rate),
        "promo_bytes_per_sec": round(analysis.promo_rate),
        "pause_ms": {k: round(v, 3) for k, v in analysis.pause_percentiles.items()},
        "pause_total_ms": round(analysis.pause_total_ms, 1),
        "gc_overhead": round(analysis.gc_overhead, 6),
        "stw_overhead": round(analysis.stw_overhead, 6),
        "non_gc_stw_ms": round(analysis.non_gc_stw_ms, 1),
        "by_label": {
            k: {"count": c, "total_ms": round(t, 1)} for k, (c, t) in analysis.by_label.items()
        },
        "causes": analysis.causes,
        "recommended_heap": analysis.recommended,
    }


def dumps(analysis: Analysis) -> str:
    return json.dumps(as_dict(analysis), indent=2)
