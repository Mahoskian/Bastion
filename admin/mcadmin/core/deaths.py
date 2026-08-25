"""Where players die, from what the log already writes down.

Vanilla death messages say who died and how, but never where. The gravestone
mod fills that gap: every death logs a placement line carrying the exact block
and dimension, on the same second as the death message. Joining the two gives
a death with both a cause and a position, which is enough to plot.

The two sources disagree in both directions, and both disagreements matter:

- a placement with no death message is still a death, so the location survives
  even when the cause does not;
- a death message with no placement happened somewhere a grave could not be
  placed, so it is counted but not plotted.

Neither is dropped. The count on a map is the count of deaths that had a
position, and the report says how many did not.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from math import hypot

from pydantic import BaseModel, ConfigDict

from .logs import EventKind, Record, extract_events

# Placed Moksha_'s gravestone at (195, 120, 680) in minecraft:overworld
PLACED_RE = re.compile(
    r"^Placed (?P<player>.+?)'s gravestone at "
    r"\((?P<x>-?\d+), (?P<y>-?\d+), (?P<z>-?\d+)\) in (?P<dimension>\S+)$"
)
# Moksha_ has found their grave at (195, 120, 680)
FOUND_RE = re.compile(
    r"^(?P<player>\S+) has found their grave at "
    r"\((?P<x>-?\d+), (?P<y>-?\d+), (?P<z>-?\d+)\)$"
)

# The death message and the placement share a second in every sample seen, but
# a busy tick can split them; a few seconds is generous and still cannot reach
# the next death, which needs a respawn first.
PAIR_WINDOW = timedelta(seconds=5)
# Deaths this close together are the same place -- a ravine, a lava lake, one
# piglin bastion. Chosen to be a few chunks: tight enough to name a spot,
# loose enough that dying twice while fleeing counts once.
CLUSTER_RADIUS = 48.0
# One dimension's coordinates mean nothing in another's -- the nether is 1:8 --
# so clustering and plotting are always per dimension.
HOTSPOT_MINIMUM = 2


def dimension_label(raw: str) -> str:
    """`minecraft:the_nether` -> `the_nether`; a modded dimension keeps its
    namespace, because two mods may well both ship a `void`."""
    namespace, _, name = raw.partition(":")
    return name if namespace == "minecraft" and name else raw


class Death(BaseModel):
    """One player death: when, who, how, and -- usually -- where."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    player: str
    cause: str = ""
    dimension: str = ""
    x: int | None = None
    y: int | None = None
    z: int | None = None
    recovered: bool = False  # the player came back and collected the grave

    @property
    def located(self) -> bool:
        return self.x is not None and self.z is not None

    @property
    def position(self) -> str:
        return f"{self.x}, {self.y}, {self.z}" if self.located else "unknown"

    def distance_to(self, x: float, z: float) -> float:
        """Horizontal distance only: a ravine is one place at every depth."""
        if not self.located:
            return float("inf")
        return hypot(self.x - x, self.z - z)


class Hotspot(BaseModel):
    """A place that keeps killing people."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    x: int
    y: int
    z: int
    deaths: list[Death] = []

    @property
    def count(self) -> int:
        return len(self.deaths)

    @property
    def players(self) -> list[str]:
        return sorted({death.player for death in self.deaths})

    @property
    def causes(self) -> dict[str, int]:
        counted = Counter(death.cause for death in self.deaths if death.cause)
        return dict(counted.most_common())

    @property
    def headline(self) -> str:
        """The single most common way this spot kills people."""
        causes = self.causes
        return next(iter(causes), "")

    @property
    def span(self) -> float:
        """How spread out the cluster is, so a wide one is not read as a point."""
        return max(
            (death.distance_to(self.x, self.z) for death in self.deaths if death.located),
            default=0.0,
        )


class DeathMap(BaseModel):
    """Every death in a window, and the places they pile up."""

    model_config = ConfigDict(frozen=True)

    deaths: list[Death] = []
    start: datetime | None = None
    end: datetime | None = None

    @property
    def located(self) -> list[Death]:
        return [death for death in self.deaths if death.located]

    @property
    def unlocated(self) -> list[Death]:
        return [death for death in self.deaths if not death.located]

    @property
    def dimensions(self) -> list[str]:
        """Dimensions with plottable deaths, deadliest first."""
        counted = Counter(death.dimension for death in self.located)
        return [name for name, _ in counted.most_common()]

    def in_dimension(self, name: str) -> list[Death]:
        return [death for death in self.located if death.dimension == name]

    @property
    def by_player(self) -> dict[str, int]:
        counted = Counter(death.player for death in self.deaths)
        return dict(counted.most_common())

    @property
    def by_cause(self) -> dict[str, int]:
        counted = Counter(normalise_cause(death.cause) for death in self.deaths if death.cause)
        return dict(counted.most_common())

    @property
    def unrecovered(self) -> list[Death]:
        """Graves nobody ever came back for -- someone's gear is still there."""
        return [death for death in self.located if not death.recovered]


def normalise_cause(cause: str) -> str:
    """Group death messages by kind rather than by wording.

    "was slain by Piglin" and "was slain by Zombified Piglin" are different
    causes worth keeping apart, but "fell from a high place" and "hit the
    ground too hard" are the same story told twice, so falls collapse.
    """
    lowered = cause.lower()
    if "fell" in lowered or "hit the ground" in lowered:
        return "fell"
    if "lava" in lowered:
        return "lava"
    if "burn" in lowered or "flames" in lowered or "fire" in lowered:
        return "fire"
    if "drown" in lowered:
        return "drowned"
    if "blew up" in lowered or "blown up" in lowered:
        return "explosion"
    for verb in (" by ", " using "):
        if verb in lowered:
            return "killed by " + cause.split(verb, 1)[1].strip()
    return cause


def collect(records: list[Record]) -> DeathMap:
    """Join gravestone placements to death messages, oldest first."""
    events = extract_events(records)
    # Deaths waiting for a placement, per player. A player has at most one open
    # death at a time -- they have to respawn before they can die again.
    pending: dict[str, list] = {}
    for event in events:
        if event.kind is EventKind.DEATH:
            pending.setdefault(event.player, []).append([event.at, event.detail, False])

    placements: list[Death] = []
    recovered: set[tuple[str, int, int, int]] = set()
    for record in records:
        found = FOUND_RE.match(record.message)
        if found is not None:
            recovered.add(
                (found["player"], int(found["x"]), int(found["y"]), int(found["z"]))
            )
            continue
        placed = PLACED_RE.match(record.message)
        if placed is None:
            continue
        player = placed["player"]
        cause = ""
        for entry in reversed(pending.get(player, [])):
            when, detail, claimed = entry
            if claimed or abs(record.at - when) > PAIR_WINDOW:
                continue
            cause, entry[2] = detail, True
            break
        placements.append(
            Death(
                at=record.at,
                player=player,
                cause=cause,
                dimension=dimension_label(placed["dimension"]),
                x=int(placed["x"]),
                y=int(placed["y"]),
                z=int(placed["z"]),
            )
        )

    deaths = [
        death.model_copy(
            update={
                "recovered": (death.player, death.x, death.y, death.z) in recovered
            }
        )
        for death in placements
    ]
    # Whatever the placements never claimed died somewhere unplottable, but it
    # still died.
    deaths.extend(
        Death(at=when, player=player, cause=detail)
        for player, entries in pending.items()
        for when, detail, claimed in entries
        if not claimed
    )
    deaths.sort(key=lambda death: death.at)
    return DeathMap(
        deaths=deaths,
        start=records[0].at if records else None,
        end=records[-1].at if records else None,
    )


def hotspots(
    deaths: list[Death], radius: float = CLUSTER_RADIUS, minimum: int = HOTSPOT_MINIMUM
) -> list[Hotspot]:
    """Cluster located deaths by proximity, busiest first.

    Greedy single-pass clustering seeded from the deaths themselves: for a few
    dozen points it finds the same groups a real clusterer would, and it never
    invents a centre where nobody actually died.
    """
    clusters: list[Hotspot] = []
    for dimension in sorted({death.dimension for death in deaths if death.located}):
        remaining = sorted(
            (death for death in deaths if death.located and death.dimension == dimension),
            key=lambda death: death.at,
        )
        while remaining:
            def neighbours(candidate: Death, pool: list[Death] = remaining) -> int:
                return sum(
                    1
                    for other in pool
                    if other.distance_to(candidate.x, candidate.z) <= radius
                )

            seed = max(remaining, key=neighbours)
            near = [
                death for death in remaining if death.distance_to(seed.x, seed.z) <= radius
            ]
            remaining = [death for death in remaining if death not in near]
            clusters.append(
                Hotspot(
                    dimension=dimension,
                    x=round(sum(death.x for death in near) / len(near)),
                    y=round(sum(death.y for death in near) / len(near)),
                    z=round(sum(death.z for death in near) / len(near)),
                    deaths=near,
                )
            )
    return sorted(
        (cluster for cluster in clusters if cluster.count >= minimum),
        key=lambda cluster: -cluster.count,
    )


def as_dict(death_map: DeathMap) -> dict:
    """Machine-readable form for --json."""
    return {
        "start": death_map.start.isoformat() if death_map.start else None,
        "end": death_map.end.isoformat() if death_map.end else None,
        "deaths": [
            {
                "at": death.at.isoformat(),
                "player": death.player,
                "cause": death.cause,
                "dimension": death.dimension,
                "x": death.x,
                "y": death.y,
                "z": death.z,
                "recovered": death.recovered,
            }
            for death in death_map.deaths
        ],
        "by_player": death_map.by_player,
        "by_cause": death_map.by_cause,
        "unlocated": len(death_map.unlocated),
        "hotspots": [
            {
                "dimension": spot.dimension,
                "x": spot.x,
                "y": spot.y,
                "z": spot.z,
                "count": spot.count,
                "players": spot.players,
                "causes": spot.causes,
            }
            for spot in hotspots(death_map.located)
        ],
    }
