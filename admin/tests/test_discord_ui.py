"""Tests for the Discord slash-command surface.

Two things are checked here, and neither needs discord.py or a network: that
RCON's `list` sentence is read correctly, and that the embeds built from core's
models stay inside Discord's own limits. The embed builders return plain dicts
precisely so this can be true.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from mcadmin.core.controller import Online, Status
from mcadmin.core.deaths import Death, DeathMap, hotspots
from mcadmin.core.models import RuntimeState, ServerState
from mcadmin.core.properties import ServerProperties
from mcadmin.core.stats import Board, PlayerStats, Roster, Standing, Unit, boards
from mcadmin.ui import discord as view

STEVE = "11111111-1111-1111-1111-111111111111"

# Discord's documented ceilings for one embed.
FIELD_VALUE_MAX = 1024
FIELD_NAME_MAX = 256
DESCRIPTION_MAX = 4096
FIELDS_MAX = 25


def check_embed(embed: dict) -> dict:
    """Every embed this module builds has to be one Discord will accept."""
    assert len(str(embed.get("description", ""))) <= DESCRIPTION_MAX
    fields = embed.get("fields", [])
    assert len(fields) <= FIELDS_MAX
    for field in fields:
        assert 0 < len(field["name"]) <= FIELD_NAME_MAX
        assert 0 < len(field["value"]) <= FIELD_VALUE_MAX, "Discord rejects an empty field"
    assert isinstance(embed["color"], int)
    return embed


# ------------------------------------------------------------------ list parsing


def test_the_list_sentence_is_read_into_names():
    online = Online.parse("There are 2 of a max of 12 players online: Steve, Alex")
    assert online is not None
    assert (online.online, online.maximum) == (2, 12)
    assert online.names == ("Steve", "Alex")


def test_an_empty_server_parses_to_no_names():
    online = Online.parse("There are 0 of a max of 12 players online:")
    assert online is not None
    assert online.online == 0
    assert online.names == ()


def test_a_sentence_a_mod_rewrote_is_not_mistaken_for_an_empty_server():
    """Returning None keeps 'cannot read this' distinct from 'nobody on', which
    is what lets the caller fall back to showing the raw line."""
    assert Online.parse("Online (2): Steve, Alex") is None


def test_no_reply_at_all_is_not_an_empty_server():
    assert Online.parse(None) is None
    assert Online.parse("") is None


# ------------------------------------------------------------------ status


def status_of(state: ServerState, players: str | None = None) -> Status:
    return Status(
        state=state,
        pid=1234 if state.is_live else None,
        session_exists=state.is_live,
        runtime=RuntimeState(supervisor_pid=1, jvm_pid=2, heap="12G", restarts=3)
        if state.is_live
        else None,
        players=players,
    )


def props() -> ServerProperties:
    return ServerProperties(values={"difficulty": "hard", "white-list": "true"})


def test_a_running_server_reports_its_players_and_heap():
    embed = check_embed(
        view.server_status(
            status_of(ServerState.RUNNING, "There are 1 of a max of 12 players online: Steve"),
            props(),
            Online.parse("There are 1 of a max of 12 players online: Steve"),
        )
    )
    names = {field["name"]: field["value"] for field in embed["fields"]}
    assert names["Players (1/12)"] == "Steve"
    assert names["Heap"] == "12G"
    assert names["Restarts"] == "3"


def test_a_stopped_server_still_renders():
    embed = check_embed(view.server_status(status_of(ServerState.STOPPED), props(), None))
    assert embed["color"] == view.RED


@pytest.mark.parametrize("state", list(ServerState))
def test_every_server_state_has_a_colour(state):
    check_embed(view.server_status(status_of(state), props(), None))


# ------------------------------------------------------------------ players


def test_an_empty_server_says_so_rather_than_listing_nothing():
    online = Online.parse("There are 0 of a max of 12 players online:")
    embed = check_embed(view.players(online, None))
    assert "Nobody is online" in embed["description"]


def test_an_unparsable_reply_falls_back_to_the_raw_line():
    raw = "Online (2): Steve, Alex"
    embed = check_embed(view.players(None, raw))
    assert embed["description"] == raw


def test_no_rcon_at_all_says_that_instead_of_claiming_an_empty_server():
    embed = check_embed(view.players(None, None))
    assert "not answering RCON" in embed["description"]


# ------------------------------------------------------------------ stats


def player(name: str = "Steve", **stats: dict) -> PlayerStats:
    return PlayerStats(
        uuid=STEVE,
        name=name,
        updated=datetime(2026, 8, 25, 12, 0),
        advancements=30,
        stats=stats
        or {
            "minecraft:custom": {
                "minecraft:play_time": 72000,
                "minecraft:deaths": 4,
                "minecraft:mob_kills": 120,
                "minecraft:walk_one_cm": 250000,
            },
            "minecraft:mined": {"minecraft:stone": 5000},
            "minecraft:killed_by": {"minecraft:creeper": 3},
        },
    )


def test_an_empty_roster_explains_itself_rather_than_showing_nothing():
    embed = check_embed(view.leaderboards(Roster(), []))
    assert "No player stats yet" in embed["description"]


def test_leaderboards_render_the_top_places():
    roster = Roster(players=[player("Steve"), player("Alex")])
    check_embed(view.leaderboards(roster, boards(roster)))


def test_a_board_with_no_standings_contributes_no_field():
    board = Board(key="deaths", title="Deaths", unit=Unit.COUNT, standings=[])
    embed = check_embed(view.leaderboards(Roster(players=[player()]), [board]))
    assert embed["fields"] == []


def test_a_player_card_carries_the_headline_numbers():
    embed = check_embed(view.player_card(player(), ["Playtime"], datetime(2026, 8, 25, 12, 0)))
    assert embed["title"] == "Steve"
    assert "Playtime" in embed["description"]
    assert {field["name"] for field in embed["fields"]} >= {"Playtime", "Deaths", "Nemesis"}


def test_an_unknown_player_lists_who_is_known():
    embed = check_embed(view.unknown_player("Nobody", ["Steve", "Alex"]))
    assert "Alex, Steve" in embed["description"]


def test_a_very_long_standing_is_trimmed_to_discords_limit():
    """A modded death message or a long name must not make the embed illegal."""
    board = Board(
        key="deaths",
        title="Deaths",
        unit=Unit.COUNT,
        standings=[
            Standing(rank=i, player="A" * 200, value=i, detail="B" * 200)
            for i in range(1, 9)
        ],
    )
    check_embed(view.leaderboards(Roster(players=[player()]), [board], top=8))


# ------------------------------------------------------------------ deaths


def died(name: str, x: int, z: int, minute: int, located_: bool = True) -> Death:
    return Death(
        at=datetime(2026, 8, 24, 10, minute),
        player=name,
        dimension="the_nether" if located_ else "",
        x=x if located_ else None,
        y=64 if located_ else None,
        z=z if located_ else None,
        cause="was slain by Piglin",
    )


def test_no_deaths_is_its_own_answer():
    embed = check_embed(view.death_map(DeathMap(), []))
    assert "No deaths" in embed["description"]


def test_the_unlocated_deaths_are_counted_not_dropped():
    """A death with no gravestone is still a death; reporting only the plotted
    ones would give a smaller, tidier, wrong number."""
    found = DeathMap(deaths=[died("Steve", 0, 0, 1), died("Alex", 0, 0, 2, located_=False)])
    embed = check_embed(view.death_map(found, hotspots(found.located)))
    assert "**2** deaths" in embed["description"]
    assert "1 were never located" in embed["description"]


def test_hotspots_and_causes_are_reported():
    found = DeathMap(deaths=[died("Steve", 0, 0, i) for i in range(1, 6)])
    embed = check_embed(view.death_map(found, hotspots(found.located)))
    names = " ".join(field["name"] for field in embed["fields"])
    assert "Hotspot" in names
    assert "Top causes" in names
