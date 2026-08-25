"""Chunk forensics: reading region headers to see what the world is made of.

An `.mca` file is 1024 chunks preceded by an 8KiB header -- 4KiB of location
entries (a 3-byte sector offset and a 1-byte sector count each) and 4KiB of
per-chunk modification timestamps. That header alone answers most of what you
want to know about a world: how many chunks exist, how big they are, which one
is enormous, and when each was last written.

Only the header is read. Two consequences worth stating plainly:

- Sizes are *allocated* sizes, rounded up to the 4KiB sector. The exact
  compressed length lives in each chunk's own header, which would mean a seek
  per chunk -- 580,000 of them for this world's overworld -- to sharpen a
  number that is already right to within a sector.
- Nothing here decompresses a chunk, so "what is in the big one" is a question
  this answers with coordinates, not with a block list. Coordinates are enough
  to go and look.

Nothing per-chunk is kept either: a world of this size holds more chunks than
it is sensible to build objects for, so the scan accumulates a histogram and a
running top-N and throws the rest away as it goes.
"""

from __future__ import annotations

import heapq
import struct
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Paths

SECTOR = 4096
CHUNKS_PER_REGION = 1024
REGION_SPAN = 32  # chunks along one edge
HEADER = SECTOR * 2
# r.-2.3.mca -- the region's coordinates, in units of 32x32 chunks.
NAME_FORMAT = "r.{x}.{z}.mca"
KINDS = ("region", "entities", "poi")
# A chunk needing this many sectors is worth a look. Vanilla terrain sits at
# one or two; anything past ~64KiB is block entities, and a lot of them.
BIG_SECTORS = 16
# The sector count is one byte, so a chunk that will not fit in 255 sectors is
# written to a side-car `.mcc` file instead. Those are the true outliers.
EXTERNAL_SUFFIX = ".mcc"


class BigChunk(BaseModel):
    """One chunk large enough to be worth going to look at."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    x: int  # chunk coordinates
    z: int
    sectors: int
    modified: datetime | None = None
    external: bool = False

    @property
    def bytes(self) -> int:
        return self.sectors * SECTOR

    @property
    def block_x(self) -> int:
        return self.x * 16

    @property
    def block_z(self) -> int:
        return self.z * 16

    @property
    def position(self) -> str:
        """Where to stand to see it -- the middle of the chunk, at sea level."""
        return f"{self.block_x + 8}, ~, {self.block_z + 8}"


class Scan(BaseModel):
    """What one dimension's region directory turned out to contain."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    kind: str = "region"
    directory: Path | None = None
    regions: int = 0
    empty_regions: int = 0  # created, but nothing in them has been generated
    chunks: int = 0
    allocated_bytes: int = 0
    file_bytes: int = 0
    sectors: dict[int, int] = {}  # sector count -> how many chunks have it
    biggest: list[BigChunk] = []
    written: dict[date, int] = {}  # day -> chunks last written that day
    bounds: tuple[int, int, int, int] | None = None  # min x, max x, min z, max z
    unreadable: list[str] = []

    @property
    def mean_bytes(self) -> float:
        return self.allocated_bytes / self.chunks if self.chunks else 0.0

    @property
    def slack_bytes(self) -> int:
        """File bytes not accounted for by any live chunk.

        Region files never shrink: a chunk that grows is rewritten at the end
        and its old sectors are left free for reuse. Persistent slack is how
        much of the world's size is that gap.
        """
        return max(0, self.file_bytes - self.allocated_bytes - self.regions * HEADER)

    @property
    def covered(self) -> int:
        """Chunks the bounding box would hold if every region were complete."""
        if self.bounds is None:
            return 0
        low_x, high_x, low_z, high_z = self.bounds
        return (high_x - low_x + 1) * (high_z - low_z + 1) * CHUNKS_PER_REGION

    @property
    def fill(self) -> float:
        """How much of that bounding box is actually generated.

        A pregen that ran to completion approaches 1.0; a world grown by
        players walking around stays low, because the box includes everywhere
        nobody went.
        """
        return self.chunks / self.covered if self.covered else 0.0

    def percentile(self, fraction: float) -> int:
        """Chunk size in bytes at this fraction, straight off the histogram."""
        if not self.chunks:
            return 0
        target = fraction * self.chunks
        seen = 0
        for count in sorted(self.sectors):
            seen += self.sectors[count]
            if seen >= target:
                return count * SECTOR
        return max(self.sectors) * SECTOR

    @property
    def big_chunks(self) -> int:
        return sum(count for size, count in self.sectors.items() if size >= BIG_SECTORS)


def region_coordinates(name: str) -> tuple[int, int] | None:
    """(x, z) from `r.-2.3.mca`, or None when the name is not one."""
    parts = name.split(".")
    if len(parts) != 4 or parts[0] != "r" or parts[3] != "mca":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _empty_totals(region_x: int, region_z: int) -> dict:
    return {
        "region": (region_x, region_z),
        "chunks": 0,
        "allocated_bytes": 0,
        "file_bytes": 0,
        "sectors": {},
        "written": {},
    }


def scan_file(path: Path, dimension: str, keep: int = 10) -> tuple[dict, list[BigChunk]]:
    """Read one region file's header.

    Returns the aggregates for that file plus its largest chunks, so the caller
    can merge without ever holding a per-chunk list.
    """
    coordinates = region_coordinates(path.name)
    if coordinates is None:
        raise ValueError(f"not a region file name: {path.name}")
    region_x, region_z = coordinates

    size = path.stat().st_size
    if size == 0:
        # 242 of this world's region files are zero bytes. That is not damage:
        # the server creates a region file when something touches the area and
        # only writes a header once a chunk in it is actually generated. They
        # are empty regions, and counting them as errors would hide the far
        # rarer file that really is corrupt.
        return _empty_totals(region_x, region_z), []

    with path.open("rb") as handle:
        header = handle.read(HEADER)
    if len(header) < HEADER:
        raise ValueError(f"{path.name} is truncated ({len(header)} bytes)")

    locations = struct.unpack(">1024I", header[:SECTOR])
    stamps = struct.unpack(">1024I", header[SECTOR:HEADER])

    sizes: Counter[int] = Counter()
    written: Counter[date] = Counter()
    top: list[tuple[int, int, int, int]] = []  # (sectors, index, chunk x, chunk z)
    chunks = allocated = 0
    external = path.parent / f"c.{region_x}.{region_z}{EXTERNAL_SUFFIX}"

    for index, entry in enumerate(locations):
        count = entry & 0xFF
        if entry == 0 or count == 0:
            continue
        chunks += 1
        allocated += count * SECTOR
        sizes[count] += 1
        stamp = stamps[index]
        if stamp:
            written[datetime.fromtimestamp(stamp).date()] += 1
        chunk_x = region_x * REGION_SPAN + (index % REGION_SPAN)
        chunk_z = region_z * REGION_SPAN + (index // REGION_SPAN)
        heapq.heappush(top, (count, index, chunk_x, chunk_z))
        if len(top) > keep:
            heapq.heappop(top)

    biggest = [
        BigChunk(
            dimension=dimension,
            x=chunk_x,
            z=chunk_z,
            sectors=count,
            modified=(
                datetime.fromtimestamp(stamps[index]) if stamps[index] else None
            ),
            external=external.exists(),
        )
        for count, index, chunk_x, chunk_z in sorted(top, reverse=True)
    ]
    return (
        {
            "region": (region_x, region_z),
            "chunks": chunks,
            "allocated_bytes": allocated,
            "file_bytes": size,
            "sectors": dict(sizes),
            "written": dict(written),
        },
        biggest,
    )


def scan(directory: Path, dimension: str, keep: int = 10) -> Scan:
    """Every region file in a directory, merged into one picture."""
    sizes: Counter[int] = Counter()
    written: Counter[date] = Counter()
    biggest: list[BigChunk] = []
    unreadable: list[str] = []
    regions = empty = chunks = allocated = file_bytes = 0
    xs: list[int] = []
    zs: list[int] = []

    for path in sorted(directory.glob("*.mca")):
        try:
            totals, top = scan_file(path, dimension, keep=keep)
        except (OSError, ValueError, struct.error):
            # One corrupt or half-written region must not take the scan down;
            # it is named in the report instead.
            unreadable.append(path.name)
            continue
        regions += 1
        empty += not totals["chunks"]
        chunks += totals["chunks"]
        allocated += totals["allocated_bytes"]
        file_bytes += totals["file_bytes"]
        sizes.update(totals["sectors"])
        written.update(totals["written"])
        xs.append(totals["region"][0])
        zs.append(totals["region"][1])
        biggest = heapq.nlargest(keep, [*biggest, *top], key=lambda chunk: chunk.sectors)

    return Scan(
        dimension=dimension,
        kind=directory.name,
        directory=directory,
        regions=regions,
        empty_regions=empty,
        chunks=chunks,
        allocated_bytes=allocated,
        file_bytes=file_bytes,
        sectors=dict(sizes),
        biggest=biggest,
        written=dict(sorted(written.items())),
        bounds=(min(xs), max(xs), min(zs), max(zs)) if xs else None,
        unreadable=unreadable,
    )


def scan_world(
    paths: Paths | None = None,
    kind: str = "region",
    dimension: str | None = None,
    keep: int = 10,
) -> list[Scan]:
    """Scan every dimension, biggest first."""
    paths = paths or Paths.from_env()
    directories = paths.region_dirs(kind)
    if dimension is not None:
        directories = {
            name: path for name, path in directories.items() if name == dimension
        }
    scans = [scan(path, name, keep=keep) for name, path in sorted(directories.items())]
    return sorted(scans, key=lambda result: -result.chunks)


def as_dict(scans: list[Scan]) -> dict:
    """Machine-readable form for --json."""
    return {
        "dimensions": [
            {
                "dimension": result.dimension,
                "kind": result.kind,
                "regions": result.regions,
                "empty_regions": result.empty_regions,
                "chunks": result.chunks,
                "allocated_bytes": result.allocated_bytes,
                "file_bytes": result.file_bytes,
                "slack_bytes": result.slack_bytes,
                "mean_bytes": round(result.mean_bytes),
                "p50_bytes": result.percentile(0.50),
                "p95_bytes": result.percentile(0.95),
                "p99_bytes": result.percentile(0.99),
                "bounds": result.bounds,
                "fill": round(result.fill, 4),
                "big_chunks": result.big_chunks,
                "unreadable": result.unreadable,
                "written": {day.isoformat(): count for day, count in result.written.items()},
                "biggest": [
                    {
                        "x": chunk.x,
                        "z": chunk.z,
                        "block_x": chunk.block_x,
                        "block_z": chunk.block_z,
                        "bytes": chunk.bytes,
                        "external": chunk.external,
                        "modified": (
                            chunk.modified.isoformat() if chunk.modified else None
                        ),
                    }
                    for chunk in result.biggest
                ],
            }
            for result in scans
        ],
        "totals": {
            "regions": sum(result.regions for result in scans),
            "chunks": sum(result.chunks for result in scans),
            "file_bytes": sum(result.file_bytes for result in scans),
        },
    }
