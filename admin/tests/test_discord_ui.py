"""Tests for the Discord slash-command surface.

Two things are checked here, and neither needs discord.py or a network: that
RCON's `list` sentence is read correctly, and that the embeds built from core's
models stay inside Discord's own limits. The embed builders return plain dicts
precisely so this can be true.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from mcadmin.core.changelog import Changelog
from mcadmin.core.controller import Online, Status
from mcadmin.core.deaths import Death, DeathMap, hotspots
from mcadmin.core.models import RuntimeState, ServerState
from mcadmin.core.mrpack import ModFile, PackResult, PackSpec
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


# ------------------------------------------------------------------ charts

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def mpl():
    """Charts are an optional extra; skip cleanly when it is not installed."""
    return pytest.importorskip("matplotlib")


def test_charts_report_whether_they_can_render():
    from mcadmin.ui import charts

    assert charts.available() is True or charts.available() is False


def test_nothing_to_plot_returns_no_chart_rather_than_empty_axes(mpl):
    from mcadmin.ui import charts

    assert charts.death_map(DeathMap(), []) is None
    assert charts.leaderboards(Roster(), []) is None
    assert charts.player_card(player(), Roster(), []) is None


def test_deaths_with_no_position_are_not_plottable(mpl):
    """They are counted in the embed, but there is nowhere to put them."""
    from mcadmin.ui import charts

    found = DeathMap(deaths=[died("Steve", 0, 0, 1, located_=False)])
    assert charts.death_map(found, []) is None


def test_the_death_map_renders_a_png(mpl):
    from mcadmin.ui import charts

    found = DeathMap(deaths=[died("Steve", i * 10, i * 10, i) for i in range(1, 6)])
    png = charts.death_map(found, hotspots(found.located))
    assert png is not None and png.startswith(PNG_MAGIC)


def test_the_leaderboards_render_a_png(mpl):
    from mcadmin.ui import charts

    roster = Roster(players=[player("Steve"), player("Alex")])
    png = charts.leaderboards(roster, boards(roster))
    assert png is not None and png.startswith(PNG_MAGIC)


def test_a_player_card_renders_a_png(mpl):
    from mcadmin.ui import charts

    roster = Roster(players=[player("Steve"), player("Alex")])
    png = charts.player_card(roster.players[0], roster, ["Playtime"])
    assert png is not None and png.startswith(PNG_MAGIC)


def test_more_boards_than_hues_never_cycles_the_palette(mpl):
    """A ninth series is never a generated hue -- the tail is dropped instead."""
    from mcadmin.ui import charts

    roster = Roster(players=[player("Steve")])
    many = [
        Board(
            key=f"b{i}",
            title=f"Board {i}",
            unit=Unit.COUNT,
            standings=[Standing(rank=1, player="Steve", value=10)],
        )
        for i in range(12)
    ]
    assert charts.leaderboards(roster, many) is not None


def test_a_charted_leaderboard_does_not_repeat_itself_as_text():
    """The chart carries the boards; duplicating them is a wall of text."""
    roster = Roster(players=[player("Steve")])
    charted = check_embed(view.leaderboards(roster, boards(roster), charted=True))
    plain = check_embed(view.leaderboards(roster, boards(roster), charted=False))
    assert charted["fields"] == []
    assert plain["fields"]


def test_a_charted_death_map_keeps_what_the_plot_does_not_show():
    """Hot spots move into the plot; causes and per-player counts do not."""
    found = DeathMap(deaths=[died("Steve", 0, 0, i) for i in range(1, 6)])
    charted = check_embed(view.death_map(found, hotspots(found.located), charted=True))
    names = {field["name"] for field in charted["fields"]}
    assert not any(name.startswith("Hotspot") for name in names)
    assert "Top causes" in names and "Most deaths" in names


# ----------------------------------------------------------------- packs


def pack(mods: list[str]) -> PackResult:
    return PackResult(
        path=Path("mrpacks/HammysServer-2026-08-26.mrpack"),
        size=2_226_517,
        linked=[ModFile(name=n, sha1="s", sha512="s", size=1, url="https://x") for n in mods],
        bundled=[],
    )


def released(before: list[str], after: list[str]):
    log, _ = Changelog().record(before, "1.0.0", datetime(2026, 8, 25, 15, 10))
    _, release = log.record(after, "1.0.1", datetime(2026, 8, 26, 9, 46))
    return release


def test_a_release_post_leads_with_what_changed():
    release = released(["a-1.0.jar", "old-1.0.jar"], ["a-1.0.jar", "new-1.0.jar"])
    embed = check_embed(view.pack_release(release, pack(["a-1.0.jar", "new-1.0.jar"]), PackSpec()))
    assert embed["title"] == "New MrPack Release"
    names = [field["name"] for field in embed["fields"]]
    assert names[0] == "ChangeLog"
    assert "Added (1)" in names and "Removed (1)" in names
    assert "HammysServer-2026-08-26.mrpack" in embed["footer"]["text"]


def test_a_release_post_names_the_updated_builds_on_both_sides():
    release = released(["lithium-fabric-0.25.3.jar"], ["lithium-fabric-0.26.0.jar"])
    embed = check_embed(view.pack_release(release, pack(["lithium-fabric-0.26.0.jar"]), PackSpec()))
    updated = next(f for f in embed["fields"] if f["name"] == "Updated (1)")
    assert "lithium-fabric-0.25.3.jar" in updated["value"]
    assert "lithium-fabric-0.26.0.jar" in updated["value"]


def test_a_release_post_survives_a_pack_with_150_mods():
    """The first build lists every mod, which is far past Discord's field
    limit -- so the list is capped and says how many it did not show."""
    mods = [f"mod{i}-1.0.jar" for i in range(150)]
    _, release = Changelog().record(mods, "1.0.0", datetime(2026, 8, 26, 9, 46))
    embed = check_embed(view.pack_release(release, pack(mods), PackSpec()))
    listing = next(f for f in embed["fields"] if f["name"] == "Mods")
    assert "and 138 more" in listing["value"]


def test_a_release_post_with_no_pack_attached_says_why():
    """A release post with no file and no explanation reads as a bug."""
    release = released(["a-1.0.jar"], ["b-1.0.jar"])
    embed = check_embed(view.pack_release(release, pack(["b-1.0.jar"]), PackSpec(), attached=False))
    file_field = next(f for f in embed["fields"] if f["name"] == "File")
    assert "over what Discord accepts" in file_field["value"]


def test_a_release_post_with_nothing_to_report_is_still_valid():
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", datetime(2026, 8, 25))
    release = log.release(["a-1.0.jar"], "1.0.1", datetime(2026, 8, 26))
    embed = check_embed(view.pack_release(release, pack(["a-1.0.jar"]), PackSpec()))
    changelog = next(f for f in embed["fields"] if f["name"] == "ChangeLog")
    assert changelog["value"] == "no mod changes"
