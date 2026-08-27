"""Rendering core's models as Discord embeds.

The same rule as the rest of `ui`: a pure function of a model that decides
nothing. Discord is a second output medium beside the terminal, not a second
opinion about what the numbers mean -- so anything that had to be *computed*
(the hotspots of a death map, the boards of a roster) is computed by `core` and
passed in already finished.

Embeds are returned as plain dicts rather than `discord.Embed` objects, which
keeps this module importable -- and testable -- without discord.py installed,
and matches how `core.notify` already builds the payloads it sends.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ..core.changelog import Release
from ..core.controller import Online, Status, Tick
from ..core.deaths import DeathMap, Hotspot
from ..core.models import ServerState
from ..core.mrpack import PackResult, PackSpec
from ..core.properties import ServerProperties
from ..core.stats import DAMAGE_PER_HEART, Board, PlayerStats, Roster, Unit, short
from ..core.units import human_distance, human_seconds
from .format import human_age, human_size

BLURPLE = 0x5865F2
GREEN = 0x2ECC71
YELLOW = 0xE5A50A
RED = 0xE74C3C
GREY = 0x95A5A6

STATE_COLOUR = {
    ServerState.RUNNING: GREEN,
    ServerState.BOOTING: YELLOW,
    ServerState.ORPHANED: YELLOW,
    ServerState.STOPPED: RED,
}

# Discord's own ceilings. A field value over 1024 characters is rejected
# outright, so anything player-supplied or unbounded is trimmed to fit.
FIELD_LIMIT = 1024
# A pack has 150 mods in it, and the first release lists every one. Discord
# would silently swallow the overflow at FIELD_LIMIT mid-word; a stated count
# of what was left out reads better than an ellipsis.
LIST_LIMIT = 12
MEDALS = ("\N{FIRST PLACE MEDAL}", "\N{SECOND PLACE MEDAL}", "\N{THIRD PLACE MEDAL}")


def _field(name: str, value: str, inline: bool = True) -> dict[str, object]:
    text = value
    if len(text) > FIELD_LIMIT:
        text = text[: FIELD_LIMIT - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return {"name": name, "value": text or "\N{EM DASH}", "inline": inline}


def _listed(names: Sequence[str], limit: int = LIST_LIMIT) -> str:
    """A bullet list, capped, saying how many it did not show."""
    shown = [f"`{name}`" for name in names[:limit]]
    if len(names) > limit:
        shown.append(f"\N{HORIZONTAL ELLIPSIS} and {len(names) - limit} more")
    return "\n".join(shown)


def _embed(title: str, colour: int, description: str = "") -> dict[str, object]:
    embed: dict[str, object] = {"title": title, "color": colour, "fields": []}
    if description:
        embed["description"] = description
    return embed


def _rank(position: int) -> str:
    return MEDALS[position - 1] if position <= len(MEDALS) else f"`{position}.`"


def _value(value: float, unit: Unit) -> str:
    if unit is Unit.DURATION:
        return human_seconds(value)
    if unit is Unit.DISTANCE:
        return human_distance(value)
    if unit is Unit.HEALTH:
        return f"{value / DAMAGE_PER_HEART:,.0f} hearts"
    return f"{value:,.0f}"


def _freshness(updated: datetime | None) -> dict[str, str] | None:
    """Stats are only as current as the last time a player was saved off."""
    if updated is None:
        return None
    return {"text": f"as of {updated:%Y-%m-%d %H:%M} ({human_age(updated)})"}


# ----------------------------------------------------------------- status


def server_status(
    status: Status, props: ServerProperties, online: Online | None
) -> dict[str, object]:
    embed = _embed(
        "Server status",
        STATE_COLOUR[status.state],
        f"**{status.state.description.capitalize()}**",
    )
    fields: list[dict[str, object]] = embed["fields"]  # type: ignore[assignment]

    if online is not None:
        listed = ", ".join(online.names) if online.names else "nobody"
        fields.append(_field(f"Players ({online.online}/{online.maximum})", listed, False))
    elif status.players:
        fields.append(_field("Players", status.players, False))

    tick = Tick.parse(status.tick)
    if tick is not None:
        fields.append(_field("Tick", tick.summary))

    if status.runtime is not None:
        runtime = status.runtime
        fields.append(_field("Heap", runtime.heap))
        fields.append(_field("Up since", f"{runtime.started_at:%Y-%m-%d %H:%M}"))
        if runtime.restarts:
            fields.append(_field("Restarts", str(runtime.restarts)))
    elif status.session_exists:
        fields.append(_field("Session", "open, but no supervisor in it", False))

    fields.append(_field("Difficulty", props.get("difficulty", "?")))
    fields.append(_field("Whitelist", "on" if props.get("white-list") == "true" else "off"))
    return embed


def players(online: Online | None, raw: str | None) -> dict[str, object]:
    """Who is on. `raw` is the fallback for a `list` line this cannot parse."""
    if online is None:
        if raw:
            return _embed("Players online", GREY, raw)
        return _embed(
            "Players online",
            GREY,
            "The server is not answering RCON, so there is nobody to count.",
        )
    if not online.names:
        return _embed(
            "Players online", GREY, f"Nobody is online. (0/{online.maximum})"
        )
    listed = "\n".join(f"\N{BULLET} {name}" for name in online.names)
    return _embed(
        "Players online", GREEN, f"**{online.online}/{online.maximum}**\n{listed}"
    )


# ----------------------------------------------------------------- stats


def leaderboards(
    roster: Roster, boards: list[Board], top: int = 3, charted: bool = False
) -> dict[str, object]:
    """`charted` drops the per-board fields: when the chart is attached it
    already carries them, and repeating them is a wall of duplicate text."""
    if not roster.players:
        return _embed(
            "Leaderboards",
            GREY,
            "No player stats yet. The server writes these when it saves a player "
            "off, so they appear once someone has played.",
        )
    embed = _embed("Leaderboards", BLURPLE)
    fields: list[dict[str, object]] = embed["fields"]  # type: ignore[assignment]
    for board in [] if charted else boards:
        if not board.standings:
            continue
        lines = [
            f"{_rank(standing.rank)} **{standing.player}** \N{EM DASH} "
            f"{_value(standing.value, board.unit)}"
            + (f" *({standing.detail})*" if standing.detail else "")
            for standing in board.standings[:top]
        ]
        fields.append(_field(board.title, "\n".join(lines)))
    footer = _freshness(roster.updated)
    if footer:
        embed["footer"] = footer
    return embed


def unknown_player(asked: str, known: list[str]) -> dict[str, object]:
    listed = ", ".join(sorted(known)) if known else "nobody yet"
    return _embed(
        "No stats for that player",
        GREY,
        f"Nothing recorded for **{asked}**.\nPlayers with stats: {listed}.",
    )


def player_card(
    player: PlayerStats, won: list[str], updated: datetime | None, charted: bool = False
) -> dict[str, object]:
    description = ""
    if won:
        description = "Tops the board for " + ", ".join(f"**{title}**" for title in won) + "."
    embed = _embed(player.name, BLURPLE, description)
    fields: list[dict[str, object]] = embed["fields"]  # type: ignore[assignment]
    if charted:
        footer = _freshness(updated)
        if footer:
            embed["footer"] = footer
        return embed

    fields.append(_field("Playtime", human_seconds(player.play_seconds)))
    fields.append(_field("Blocks mined", f"{player.blocks_mined:,}"))
    fields.append(_field("Mobs killed", f"{player.mob_kills:,}"))
    fields.append(_field("Deaths", f"{player.deaths:,}"))
    fields.append(_field("Distance", human_distance(player.distance_cm)))
    fields.append(
        _field("Damage taken", f"{player.damage_taken / DAMAGE_PER_HEART:,.0f} hearts")
    )
    fields.append(_field("Advancements", f"{player.advancements:,}"))

    nemesis = player.nemesis
    if nemesis is not None:
        fields.append(_field("Nemesis", f"{short(nemesis[0])} \N{MULTIPLICATION SIGN}{nemesis[1]}"))
    footer = _freshness(updated)
    if footer:
        embed["footer"] = footer
    return embed


# ----------------------------------------------------------------- deaths


def death_map(
    found: DeathMap, spots: list[Hotspot], charted: bool = False
) -> dict[str, object]:
    """`charted` drops the hot-spot fields -- the plot shows those far better
    than a coordinate triple does. Causes and per-player counts stay either
    way, because the chart does not carry them."""
    total = len(found.deaths)
    if not total:
        return _embed("Deaths", GREY, "No deaths in this window. Suspicious.")

    located = len(found.located)
    description = f"**{total}** deaths, **{located}** with a known position."
    if located < total:
        # A death with no gravestone happened where no grave could be placed.
        # Counting it and saying so beats quietly plotting only the ones that
        # joined, which would report a smaller, tidier, wrong number.
        description += f" The other {total - located} were never located."
    embed = _embed("Deaths", RED, description)
    fields: list[dict[str, object]] = embed["fields"]  # type: ignore[assignment]

    for spot in [] if charted else spots[:3]:
        fields.append(
            _field(
                f"Hotspot \N{EM DASH} {spot.count} deaths",
                f"{spot.headline}\n`{spot.x}, {spot.y}, {spot.z}` in {spot.dimension}",
                False,
            )
        )

    causes = sorted(found.by_cause.items(), key=lambda pair: -pair[1])[:5]
    if causes:
        fields.append(
            _field("Top causes", "\n".join(f"{cause} \N{EM DASH} {n}" for cause, n in causes))
        )
    players_by_deaths = sorted(found.by_player.items(), key=lambda pair: -pair[1])[:5]
    if players_by_deaths:
        listed = "\n".join(f"{who} \N{EM DASH} {n}" for who, n in players_by_deaths)
        fields.append(_field("Most deaths", listed))
    return embed


# ----------------------------------------------------------------- packs


def pack_release(
    release: Release,
    result: PackResult,
    spec: PackSpec,
    attached: bool = True,
) -> dict[str, object]:
    """The announcement that goes out with a freshly built .mrpack.

    `attached` is false when the file was too big for Discord to carry. The
    embed then says so rather than going out looking like the pack is there --
    a release post with no pack and no explanation is worse than no post.
    """
    embed = _embed(
        "New MrPack Release",
        BLURPLE,
        f"**{spec.name}** `{spec.version}`\n"
        f"Minecraft {spec.mc_version} \N{MIDDLE DOT} Fabric Loader {spec.loader} "
        f"\N{MIDDLE DOT} {result.total} mods \N{MIDDLE DOT} {human_size(result.size)}",
    )
    fields: list[dict[str, object]] = embed["fields"]  # type: ignore[assignment]
    fields.append(_field("ChangeLog", release.summary, False))

    if release.initial:
        fields.append(_field("Mods", _listed(release.added), False))
    else:
        if release.added:
            fields.append(_field(f"Added ({len(release.added)})", _listed(release.added), False))
        if release.removed:
            fields.append(
                _field(f"Removed ({len(release.removed)})", _listed(release.removed), False)
            )
        if release.updated:
            changes = [
                f"`{change.mod}` \N{EM DASH} {change.before} \N{RIGHTWARDS ARROW} {change.after}"
                for change in release.updated[:LIST_LIMIT]
            ]
            if len(release.updated) > LIST_LIMIT:
                changes.append(
                    f"\N{HORIZONTAL ELLIPSIS} and {len(release.updated) - LIST_LIMIT} more"
                )
            fields.append(
                _field(f"Updated ({len(release.updated)})", "\n".join(changes), False)
            )

    if not attached:
        fields.append(
            _field(
                "File",
                f"`{result.path.name}` is {human_size(result.size)}, over what Discord "
                "accepts here \N{EM DASH} ask the admin for it directly.",
                False,
            )
        )
    embed["footer"] = {
        "text": f"{result.path.name} \N{MIDDLE DOT} built {release.built_at:%Y-%m-%d %H:%M}"
    }
    return embed
