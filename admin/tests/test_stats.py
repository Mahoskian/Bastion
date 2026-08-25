"""Tests for reading world stats into leaderboards and wrapped cards."""

import json

import pytest

from mcadmin.core import stats as st
from mcadmin.core.models import Paths
from mcadmin.core.stats import DAMAGE_PER_HEART, TICKS_PER_SECOND, PlayerStats, Roster, Unit

STEVE = "11111111-1111-1111-1111-111111111111"
ALEX = "22222222-2222-2222-2222-222222222222"


def write_stats(root, uuid: str, custom=None, **categories):
    directory = root / "world" / "players" / "stats"
    directory.mkdir(parents=True, exist_ok=True)
    stats = {f"minecraft:{name}": values for name, values in categories.items()}
    if custom:
        stats["minecraft:custom"] = {f"minecraft:{k}": v for k, v in custom.items()}
    (directory / f"{uuid}.json").write_text(json.dumps({"stats": stats, "DataVersion": 4903}))


def write_names(root, mapping: dict[str, str]):
    (root / "usercache.json").write_text(
        json.dumps([{"uuid": uuid, "name": name} for uuid, name in mapping.items()])
    )


@pytest.fixture
def world(tmp_path):
    write_stats(
        tmp_path,
        STEVE,
        custom={"play_time": 36000 * TICKS_PER_SECOND, "deaths": 3, "mob_kills": 40},
        mined={"minecraft:stone": 100, "minecraft:dirt": 20},
        killed={"minecraft:zombie": 30},
        killed_by={"minecraft:creeper": 2, "minecraft:zombie": 1},
    )
    write_stats(
        tmp_path,
        ALEX,
        custom={"play_time": 3600 * TICKS_PER_SECOND, "deaths": 9},
        mined={"minecraft:deepslate": 500},
    )
    write_names(tmp_path, {STEVE: "Steve", ALEX: "Alex"})
    return Paths(server_dir=tmp_path, backup_dir=tmp_path / "backups")


# ------------------------------------------------------------------ loading


def test_stats_load_with_names_resolved(world):
    roster = st.load(world)
    assert [player.name for player in roster.players] == ["Steve", "Alex"]


def test_players_are_ordered_by_playtime(world):
    roster = st.load(world)
    assert roster.players[0].play_seconds > roster.players[1].play_seconds


def test_play_time_is_converted_from_ticks(world):
    assert st.load(world).find("Steve").play_seconds == 36000


def test_an_unknown_uuid_stays_a_uuid_rather_than_a_guess(tmp_path):
    write_stats(tmp_path, STEVE, custom={"play_time": 20})
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    assert st.load(paths).players[0].name == STEVE[:8]


def test_the_whitelist_fills_in_for_an_expired_usercache(tmp_path):
    write_stats(tmp_path, STEVE, custom={"play_time": 20})
    (tmp_path / "whitelist.json").write_text(json.dumps([{"uuid": STEVE, "name": "Steve"}]))
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    assert st.load(paths).players[0].name == "Steve"


def test_a_corrupt_stats_file_is_skipped_not_fatal(world, tmp_path):
    bad = tmp_path / "world" / "players" / "stats" / "33333333-3333-3333-3333-333333333333.json"
    bad.write_text("{ half written")
    assert len(st.load(world).players) == 2


def test_a_missing_world_is_an_empty_roster(tmp_path):
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    roster = st.load(paths)
    assert roster.players == []
    assert roster.updated is None


def test_legacy_worlds_keep_stats_directly_under_world(tmp_path):
    directory = tmp_path / "world" / "stats"
    directory.mkdir(parents=True)
    (directory / f"{STEVE}.json").write_text(json.dumps({"stats": {}}))
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    assert paths.player_dir("stats") == directory
    assert len(st.load(paths).players) == 1


def test_only_vanilla_non_recipe_advancements_are_counted(tmp_path):
    write_stats(tmp_path, STEVE, custom={"play_time": 20})
    advancements = tmp_path / "world" / "players" / "advancements"
    advancements.mkdir(parents=True)
    (advancements / f"{STEVE}.json").write_text(
        json.dumps(
            {
                "minecraft:story/root": {"done": True},
                "minecraft:story/mine_stone": {"done": True},
                "minecraft:recipes/misc/stick": {"done": True},
                "betternether:root": {"done": True},
                "minecraft:end/elytra": {"done": False},
                "DataVersion": 4903,
            }
        )
    )
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    assert st.load(paths).players[0].advancements == 2


# ------------------------------------------------------------------ counters


def test_travel_discovers_modded_movement_types():
    player = PlayerStats(
        uuid=STEVE,
        name="Steve",
        updated="2026-08-25T12:00:00",
        stats={
            "minecraft:custom": {
                "minecraft:walk_one_cm": 100,
                "minecraft:happy_ghast_one_cm": 900,
                "minecraft:jump": 5,
            }
        },
    )
    assert list(player.travel()) == ["happy_ghast", "walk"]
    assert player.distance_cm == 1000, "jump is not a distance"


def test_a_zero_distance_is_left_out_of_travel():
    player = PlayerStats(
        uuid=STEVE,
        name="Steve",
        updated="2026-08-25T12:00:00",
        stats={"minecraft:custom": {"minecraft:fly_one_cm": 0}},
    )
    assert player.travel() == {}


def test_nemesis_is_whatever_killed_the_player_most(world):
    assert st.load(world).find("Steve").nemesis == ("minecraft:creeper", 2)


def test_a_player_who_never_died_has_no_nemesis(world):
    assert st.load(world).find("Alex").nemesis is None


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("minecraft:deepslate_diamond_ore", "deepslate diamond ore"),
        ("betternether:soul_sand", "soul sand (betternether)"),
    ],
)
def test_ids_are_readable_but_a_mod_keeps_its_namespace(key, expected):
    assert st.short(key) == expected


# ------------------------------------------------------------------ boards


def test_boards_rank_best_first(world):
    boards = {board.key: board for board in st.boards(st.load(world))}
    assert boards["playtime"].winner.player == "Steve"
    assert boards["deaths"].winner.player == "Alex"


def test_a_zero_score_is_left_off_a_board(world):
    boards = {board.key: board for board in st.boards(st.load(world))}
    # Alex has no kills at all, so the board holds one entry, not two at zero.
    assert [s.player for s in boards["killed"].standings] == ["Steve"]


def test_every_board_declares_a_unit(world):
    assert all(isinstance(board.unit, Unit) for board in st.boards(st.load(world)))


def test_damage_is_counted_in_tenths_of_a_half_heart():
    player = PlayerStats(
        uuid=STEVE,
        name="Steve",
        updated="2026-08-25T12:00:00",
        stats={"minecraft:custom": {"minecraft:damage_taken": 10 * DAMAGE_PER_HEART}},
    )
    assert player.damage_taken / DAMAGE_PER_HEART == 10


def test_titles_name_only_the_boards_a_player_tops(world):
    roster = st.load(world)
    assert "Deaths" in st.titles(roster, roster.find("Alex"))
    assert "Playtime" not in st.titles(roster, roster.find("Alex"))


def test_find_matches_case_insensitively_and_by_uuid_prefix(world):
    roster = st.load(world)
    assert roster.find("steve").uuid == STEVE
    assert roster.find(STEVE[:8]).name == "Steve"
    assert roster.find("nobody") is None


def test_boards_on_an_empty_roster_are_empty_not_an_error():
    for board in st.boards(Roster()):
        assert board.standings == []


def test_as_dict_is_json_serialisable(world):
    payload = st.as_dict(st.load(world))
    assert json.loads(json.dumps(payload))["players"][0]["name"] == "Steve"
