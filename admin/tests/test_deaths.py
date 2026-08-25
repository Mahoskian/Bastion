"""Tests for joining death messages to gravestone placements."""

from datetime import datetime

import pytest

from mcadmin.core import deaths as dt
from mcadmin.core.deaths import Death, normalise_cause
from mcadmin.core.logs import Level, Record
from mcadmin.ui.deaths import scatter


def record(time: str, message: str, level: Level = Level.INFO) -> Record:
    hour, minute, second = (int(part) for part in time.split(":"))
    return Record(
        at=datetime(2026, 8, 24, hour, minute, second),
        thread="Server thread",
        level=level,
        message=message,
    )


def joined(player: str, time: str = "10:00:00") -> Record:
    return record(time, f"{player} joined the game")


def grave(player: str, time: str, x: int, y: int, z: int, dimension="minecraft:the_nether"):
    return record(time, f"Placed {player}'s gravestone at ({x}, {y}, {z}) in {dimension}")


def death(player: str, time: str, cause: str = "was slain by Piglin") -> Record:
    return record(time, f"{player} {cause}")


# ------------------------------------------------------------------ joining


def test_a_placement_takes_its_cause_from_the_death_message():
    found = dt.collect(
        [joined("Steve"), death("Steve", "10:05:00"), grave("Steve", "10:05:00", 1, 2, 3)]
    )
    (one,) = found.deaths
    assert one.player == "Steve"
    assert one.cause == "was slain by Piglin"
    assert (one.x, one.y, one.z) == (1, 2, 3)
    assert one.dimension == "the_nether"


def test_a_placement_seconds_after_the_message_still_pairs():
    found = dt.collect(
        [joined("Steve"), death("Steve", "10:05:00"), grave("Steve", "10:05:04", 1, 2, 3)]
    )
    assert found.deaths[0].cause == "was slain by Piglin"


def test_a_placement_long_after_any_message_pairs_with_nothing():
    found = dt.collect(
        [joined("Steve"), death("Steve", "10:05:00"), grave("Steve", "10:30:00", 1, 2, 3)]
    )
    causes = sorted(one.cause for one in found.deaths)
    assert causes == ["", "was slain by Piglin"], "both are kept, neither is invented"


def test_a_placement_with_no_death_message_is_still_a_located_death():
    found = dt.collect([joined("Steve"), grave("Steve", "10:05:00", 4, 5, 6)])
    (one,) = found.deaths
    assert one.located and one.cause == ""


def test_a_death_message_with_no_placement_is_counted_but_not_plotted():
    found = dt.collect([joined("Steve"), death("Steve", "10:05:00")])
    (one,) = found.deaths
    assert not one.located
    assert found.unlocated == [one] and found.located == []


def test_two_deaths_do_not_share_one_message():
    found = dt.collect(
        [
            joined("Steve"),
            death("Steve", "10:05:00"),
            grave("Steve", "10:05:00", 1, 2, 3),
            grave("Steve", "10:06:00", 9, 9, 9),
        ]
    )
    assert sorted(one.cause for one in found.deaths) == ["", "was slain by Piglin"]


def test_deaths_come_back_in_time_order():
    found = dt.collect(
        [
            joined("Steve"),
            grave("Steve", "10:20:00", 1, 1, 1),
            grave("Steve", "10:05:00", 2, 2, 2),
        ]
    )
    assert [one.at.minute for one in found.deaths] == [5, 20]


def test_a_collected_grave_is_marked_recovered():
    found = dt.collect(
        [
            joined("Steve"),
            grave("Steve", "10:05:00", 1, 2, 3),
            record("10:09:00", "Steve has found their grave at (1, 2, 3)"),
        ]
    )
    assert found.deaths[0].recovered
    assert found.unrecovered == []


def test_finding_a_different_grave_does_not_mark_this_one_recovered():
    found = dt.collect(
        [
            joined("Steve"),
            grave("Steve", "10:05:00", 1, 2, 3),
            record("10:09:00", "Steve has found their grave at (7, 7, 7)"),
        ]
    )
    assert found.unrecovered == found.deaths


def test_a_players_name_with_an_apostrophe_still_parses():
    found = dt.collect([joined("o'brien"), grave("o'brien", "10:05:00", 1, 2, 3)])
    assert found.deaths[0].player == "o'brien"


def test_a_modded_dimension_keeps_its_namespace():
    found = dt.collect(
        [joined("Steve"), grave("Steve", "10:05:00", 1, 2, 3, "kattersstructures:deep_blue")]
    )
    assert found.deaths[0].dimension == "kattersstructures:deep_blue"


def test_no_records_is_an_empty_map():
    found = dt.collect([])
    assert found.deaths == [] and found.dimensions == []


# ------------------------------------------------------------------ causes


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ("fell from a high place", "fell"),
        ("hit the ground too hard", "fell"),
        ("tried to swim in lava", "lava"),
        ("burned to death", "fire"),
        ("drowned", "drowned"),
        ("was blown up by Creeper", "explosion"),
        ("was slain by Piglin", "killed by Piglin"),
        ("was shot by Skeleton", "killed by Skeleton"),
    ],
)
def test_causes_group_by_kind(cause, expected):
    assert normalise_cause(cause) == expected


def test_two_different_killers_stay_apart():
    assert normalise_cause("was slain by Piglin") != normalise_cause("was slain by Zombie")


# ------------------------------------------------------------------ hotspots


def located(player: str, x: int, z: int, minute: int, dimension="the_nether") -> Death:
    return Death(
        at=datetime(2026, 8, 24, 10, minute),
        player=player,
        dimension=dimension,
        x=x,
        y=64,
        z=z,
        cause="fell from a high place",
    )


def test_nearby_deaths_cluster_into_one_hotspot():
    spots = dt.hotspots([located("Steve", 0, 0, 1), located("Alex", 10, 10, 2)])
    assert len(spots) == 1 and spots[0].count == 2


def test_distant_deaths_do_not_cluster():
    spots = dt.hotspots([located("Steve", 0, 0, 1), located("Alex", 5000, 5000, 2)])
    assert spots == [], "two lone deaths are not hot spots"


def test_the_same_coordinates_in_two_dimensions_never_cluster():
    spots = dt.hotspots(
        [located("Steve", 0, 0, 1), located("Alex", 0, 0, 2, dimension="overworld")]
    )
    assert spots == []


def test_a_hotspot_reports_who_and_how():
    spots = dt.hotspots(
        [located("Steve", 0, 0, 1), located("Steve", 4, 4, 2), located("Alex", 8, 8, 3)]
    )
    (spot,) = spots
    assert spot.players == ["Alex", "Steve"]
    assert spot.headline == "fell from a high place"
    assert spot.count == 3


def test_clustering_ignores_depth():
    deep = located("Steve", 0, 0, 1).model_copy(update={"y": -50})
    shallow = located("Alex", 5, 5, 2).model_copy(update={"y": 200})
    assert dt.hotspots([deep, shallow])[0].count == 2, "a ravine is one place"


def test_the_busiest_cluster_comes_first():
    deaths = [located("Steve", 0, i, i) for i in range(4)]
    deaths += [located("Alex", 900, 900, 9), located("Alex", 905, 905, 10)]
    spots = dt.hotspots(deaths)
    assert [spot.count for spot in spots] == [4, 2]


# ------------------------------------------------------------------ plotting


def test_the_plot_places_the_northernmost_death_on_the_top_row():
    north = located("Steve", 0, -500, 1)
    south = located("Alex", 0, 500, 2)
    grid, _ = scatter([north, south], {north: "N", south: "S"})
    assert "N" in grid[0], "low z is north, and north is up"
    assert "S" in grid[-1]


def test_a_single_death_still_plots_without_dividing_by_zero():
    grid, bounds = scatter([located("Steve", 100, 100, 1)], {})
    assert grid and bounds[1] > bounds[0]


def test_hotspot_letters_reach_the_grid():
    deaths = [located("Steve", 0, 0, 1), located("Alex", 10, 10, 2)]
    letters = dict.fromkeys(deaths, "A")
    grid, _ = scatter(deaths, letters)
    assert any("A" in line for line in grid)


def test_a_tall_region_narrows_the_map_rather_than_squashing_it():
    deaths = [located("Steve", 0, 0, 1), located("Alex", 10, 4000, 2)]
    grid, _ = scatter(deaths, {})
    columns = grid[0].count("·") + grid[0].count("1") + grid[0].count("2")
    assert columns < 60, "the width shrinks to keep both axes on one scale"


def test_as_dict_is_json_serialisable():
    import json

    found = dt.collect([joined("Steve"), grave("Steve", "10:05:00", 1, 2, 3)])
    assert json.loads(json.dumps(dt.as_dict(found)))["deaths"][0]["player"] == "Steve"
