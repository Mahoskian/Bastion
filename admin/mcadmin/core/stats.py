"""Reading the per-player statistics the server already keeps.

Every player has a `stats/<uuid>.json` holding counters vanilla has always
recorded: play time, blocks mined, mobs killed, distance by travel type,
deaths. Nothing here asks the server for anything -- it is all on disk, keyed
by uuid, which is the only reason `usercache.json` matters.

One thing worth knowing before reading any number out of this: the server only
writes these files when it saves a player off, so a counter is as fresh as the
last autosave, not as fresh as now. `Roster.updated` carries that timestamp so
a report can say which it is showing.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Paths

# `minecraft:play_time` and friends count *ticks*, not seconds. A tick is 20ms
# only while the server keeps up -- on a lagging server these counters run slow,
# which is why playtime here can disagree with the log digest's wall-clock stay.
TICKS_PER_SECOND = 20
CUSTOM = "minecraft:custom"
DISTANCE_SUFFIX = "_one_cm"
# Mods grant their own advancements by the hundred -- 1316 "done" for a player
# with 30 real ones -- and recipe unlocks are advancements too. Only vanilla
# non-recipe entries mean what a player thinks "advancement" means.
VANILLA_ADVANCEMENT = "minecraft:"
RECIPE_ADVANCEMENT = "minecraft:recipes/"
# Damage counters are stored as round(damage * 10), and one point of damage is
# half a heart -- so a full heart is 20 of these. Reading them as raw hearts
# reports a player taking several hundred a death, which is what gave it away.
DAMAGE_PER_HEART = 20


class Unit(StrEnum):
    """What a number means, so the UI can format it without deciding anything."""

    COUNT = "count"
    DURATION = "duration"  # seconds
    DISTANCE = "distance"  # centimetres
    HEALTH = "health"  # damage counters, in DAMAGE_PER_HEART units


def short(key: str) -> str:
    """`minecraft:deepslate_diamond_ore` -> `deepslate diamond ore`.

    Namespaced ids are how the game writes things down and not how anyone reads
    them, but a modded id keeps its namespace -- `betternether:soul_sand` and
    `minecraft:soul_sand` are different blocks and collapsing them would lie.
    """
    namespace, _, name = key.partition(":")
    readable = name.replace("_", " ")
    return readable if namespace == "minecraft" else f"{readable} ({namespace})"


class PlayerStats(BaseModel):
    """One player's counters, exactly as the server wrote them."""

    model_config = ConfigDict(frozen=True)

    uuid: str
    name: str
    updated: datetime
    stats: dict[str, dict[str, int]] = {}
    advancements: int = 0

    # -- raw access

    def category(self, name: str) -> dict[str, int]:
        return self.stats.get(name, {})

    def custom(self, key: str) -> int:
        return self.category(CUSTOM).get(key, 0)

    def total(self, name: str) -> int:
        return sum(self.category(name).values())

    def best(self, name: str) -> tuple[str, int] | None:
        """The largest entry in a category, or None when it is empty."""
        entries = self.category(name)
        if not entries:
            return None
        key = max(entries, key=lambda item: entries[item])
        return key, entries[key]

    def top(self, name: str, limit: int = 5) -> list[tuple[str, int]]:
        entries = self.category(name)
        return sorted(entries.items(), key=lambda item: -item[1])[:limit]

    # -- the counters worth naming

    @property
    def play_seconds(self) -> float:
        return self.custom("minecraft:play_time") / TICKS_PER_SECOND

    @property
    def since_death_seconds(self) -> float:
        return self.custom("minecraft:time_since_death") / TICKS_PER_SECOND

    @property
    def deaths(self) -> int:
        return self.custom("minecraft:deaths")

    @property
    def mob_kills(self) -> int:
        return self.custom("minecraft:mob_kills")

    @property
    def player_kills(self) -> int:
        return self.custom("minecraft:player_kills")

    @property
    def blocks_mined(self) -> int:
        return self.total("minecraft:mined")

    @property
    def items_crafted(self) -> int:
        return self.total("minecraft:crafted")

    @property
    def damage_taken(self) -> int:
        return self.custom("minecraft:damage_taken")

    @property
    def damage_dealt(self) -> int:
        return self.custom("minecraft:damage_dealt")

    @property
    def jumps(self) -> int:
        return self.custom("minecraft:jump")

    def travel(self) -> dict[str, int]:
        """Centimetres by travel type, largest first.

        Mods add their own -- `happy_ghast_one_cm` is in this world's files --
        so the keys are discovered rather than listed.
        """
        moved = {
            key[: -len(DISTANCE_SUFFIX)].partition(":")[2]: value
            for key, value in self.category(CUSTOM).items()
            if key.endswith(DISTANCE_SUFFIX) and value
        }
        return dict(sorted(moved.items(), key=lambda item: -item[1]))

    @property
    def distance_cm(self) -> int:
        return sum(self.travel().values())

    @property
    def deaths_per_hour(self) -> float:
        hours = self.play_seconds / 3600
        return self.deaths / hours if hours else 0.0

    @property
    def nemesis(self) -> tuple[str, int] | None:
        """Whatever has killed this player most."""
        return self.best("minecraft:killed_by")


class Roster(BaseModel):
    """Every player with a stats file, and how stale the newest one is."""

    model_config = ConfigDict(frozen=True)

    players: list[PlayerStats] = []
    directory: Path | None = None

    @property
    def updated(self) -> datetime | None:
        """When the server last saved any of these files."""
        return max((p.updated for p in self.players), default=None)

    def find(self, name: str) -> PlayerStats | None:
        lowered = name.lower()
        for player in self.players:
            if player.name.lower() == lowered or player.uuid.startswith(lowered):
                return player
        return None


def names(paths: Paths | None = None) -> dict[str, str]:
    """uuid -> name, from the two files the server keeps it in.

    `usercache.json` expires entries, so the whitelist fills in anyone who has
    not logged in lately. A uuid neither file knows stays a uuid rather than
    becoming a guess.
    """
    paths = paths or Paths.from_env()
    found: dict[str, str] = {}
    for path in (paths.whitelist, paths.usercache):
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("uuid") and entry.get("name"):
                found[str(entry["uuid"])] = str(entry["name"])
    return found


def _advancement_count(path: Path) -> int:
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    return sum(
        1
        for key, value in entries.items()
        if key.startswith(VANILLA_ADVANCEMENT)
        and not key.startswith(RECIPE_ADVANCEMENT)
        and isinstance(value, dict)
        and value.get("done")
    )


def load(paths: Paths | None = None) -> Roster:
    """Read every stats file, newest playtime first.

    A file that will not parse is skipped rather than fatal: the server writes
    these live, and catching one mid-write should not take a report down.
    """
    paths = paths or Paths.from_env()
    directory = paths.player_dir("stats")
    advancements = paths.player_dir("advancements")
    lookup = names(paths)

    players: list[PlayerStats] = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        uuid = path.stem
        raw = document.get("stats", {}) if isinstance(document, dict) else {}
        players.append(
            PlayerStats(
                uuid=uuid,
                name=lookup.get(uuid, uuid[:8]),
                updated=datetime.fromtimestamp(path.stat().st_mtime),
                stats={
                    key: {k: int(v) for k, v in value.items()}
                    for key, value in raw.items()
                    if isinstance(value, dict)
                },
                advancements=_advancement_count(advancements / f"{uuid}.json"),
            )
        )
    players.sort(key=lambda player: -player.play_seconds)
    return Roster(players=players, directory=directory)


# ------------------------------------------------------------- leaderboards


class Standing(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    player: str
    value: float
    detail: str = ""


class Board(BaseModel):
    """One leaderboard: a title, a unit, and everyone who scored above zero."""

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    unit: Unit = Unit.COUNT
    note: str = ""
    standings: list[Standing] = []

    @property
    def winner(self) -> Standing | None:
        return self.standings[0] if self.standings else None


def _detail(player: PlayerStats, key: str) -> str:
    """The one extra fact that makes a standing worth reading."""
    if key == "mined":
        best = player.best("minecraft:mined")
        return f"mostly {short(best[0])}" if best else ""
    if key == "killed":
        best = player.best("minecraft:killed")
        return f"mostly {short(best[0])}" if best else ""
    if key == "deaths":
        worst = player.nemesis
        return f"{short(worst[0])} x{worst[1]}" if worst else ""
    if key == "distance":
        travel = player.travel()
        return f"mostly {next(iter(travel)).replace('_', ' ')}" if travel else ""
    return ""


# (key, title, how to score it, unit). Scoring stays here rather than in the
# UI so a board is data -- `--json` and the table read the same numbers.
DEFINITIONS: tuple[tuple[str, str, str, Unit], ...] = (
    ("playtime", "Playtime", "play_seconds", Unit.DURATION),
    ("mined", "Blocks mined", "blocks_mined", Unit.COUNT),
    ("killed", "Mobs killed", "mob_kills", Unit.COUNT),
    ("deaths", "Deaths", "deaths", Unit.COUNT),
    ("distance", "Distance travelled", "distance_cm", Unit.DISTANCE),
    ("crafted", "Items crafted", "items_crafted", Unit.COUNT),
    ("damage_taken", "Damage taken", "damage_taken", Unit.HEALTH),
    ("advancements", "Advancements", "advancements", Unit.COUNT),
)

NOTES = {
    "playtime": "counted in ticks, so a lagging server undercounts it",
    "advancements": "vanilla advancements only -- mods grant hundreds",
}


def boards(roster: Roster, limit: int = 10) -> list[Board]:
    """Every leaderboard, each ordered best first with zeroes left out."""
    built: list[Board] = []
    for key, title, attribute, unit in DEFINITIONS:
        scored = [
            (player, float(getattr(player, attribute))) for player in roster.players
        ]
        ranked = sorted(
            ((player, value) for player, value in scored if value > 0),
            key=lambda item: -item[1],
        )[:limit]
        built.append(
            Board(
                key=key,
                title=title,
                unit=unit,
                note=NOTES.get(key, ""),
                standings=[
                    Standing(
                        rank=index,
                        player=player.name,
                        value=value,
                        detail=_detail(player, key),
                    )
                    for index, (player, value) in enumerate(ranked, start=1)
                ],
            )
        )
    return built


def titles(roster: Roster, player: PlayerStats) -> list[str]:
    """The boards this player tops. Empty for everyone but the winner."""
    return [
        board.title
        for board in boards(roster)
        if board.winner is not None and board.winner.player == player.name
    ]


def as_dict(roster: Roster) -> dict:
    """Machine-readable form for --json."""
    return {
        "updated": roster.updated.isoformat() if roster.updated else None,
        "players": [
            {
                "uuid": player.uuid,
                "name": player.name,
                "updated": player.updated.isoformat(),
                "play_seconds": round(player.play_seconds, 1),
                "deaths": player.deaths,
                "mob_kills": player.mob_kills,
                "player_kills": player.player_kills,
                "blocks_mined": player.blocks_mined,
                "items_crafted": player.items_crafted,
                "damage_taken": player.damage_taken,
                "damage_dealt": player.damage_dealt,
                "advancements": player.advancements,
                "distance_cm": player.distance_cm,
                "travel_cm": player.travel(),
                "top_mined": dict(player.top("minecraft:mined")),
                "top_killed": dict(player.top("minecraft:killed")),
                "killed_by": dict(player.top("minecraft:killed_by")),
            }
            for player in roster.players
        ],
        "boards": [
            {
                "key": board.key,
                "title": board.title,
                "unit": board.unit.value,
                "standings": [
                    {"rank": s.rank, "player": s.player, "value": s.value}
                    for s in board.standings
                ],
            }
            for board in boards(roster)
        ],
    }
