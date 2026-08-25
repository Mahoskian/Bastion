"""Tests for reading region file headers."""

import json
import struct

import pytest

from mcadmin.core import regions as rg
from mcadmin.core.models import Paths
from mcadmin.core.regions import CHUNKS_PER_REGION, HEADER, SECTOR


def region(path, chunks: dict[int, int], stamps: dict[int, int] | None = None, body=0):
    """Write a region file whose header claims `index -> sector count`."""
    locations = [0] * CHUNKS_PER_REGION
    offset = 2  # the header occupies sectors 0 and 1
    for index, count in sorted(chunks.items()):
        locations[index] = (offset << 8) | count
        offset += count
    timestamps = [0] * CHUNKS_PER_REGION
    for index, stamp in (stamps or {}).items():
        timestamps[index] = stamp
    path.write_bytes(
        struct.pack(">1024I", *locations)
        + struct.pack(">1024I", *timestamps)
        + b"\0" * body
    )
    return path


@pytest.fixture
def overworld(tmp_path):
    directory = tmp_path / "world" / "dimensions" / "minecraft" / "overworld" / "region"
    directory.mkdir(parents=True)
    return directory


# ------------------------------------------------------------------ one file


def test_a_region_files_chunks_are_counted(tmp_path):
    path = region(tmp_path / "r.0.0.mca", {0: 1, 1: 2, 5: 3})
    totals, _ = rg.scan_file(path, "overworld")
    assert totals["chunks"] == 3
    assert totals["allocated_bytes"] == 6 * SECTOR


def test_an_empty_slot_is_not_a_chunk(tmp_path):
    path = region(tmp_path / "r.0.0.mca", {0: 1})
    totals, _ = rg.scan_file(path, "overworld")
    assert totals["chunks"] == 1


def test_chunk_coordinates_come_from_the_region_and_the_index(tmp_path):
    # index 33 is x=1, z=1 inside the region; region -2,3 starts at chunk -64,96
    path = region(tmp_path / "r.-2.3.mca", {33: 4})
    _, (biggest,) = rg.scan_file(path, "overworld")
    assert (biggest.x, biggest.z) == (-63, 97)
    assert (biggest.block_x, biggest.block_z) == (-1008, 1552)


def test_the_largest_chunks_come_back_biggest_first(tmp_path):
    path = region(tmp_path / "r.0.0.mca", {0: 1, 1: 9, 2: 4})
    _, biggest = rg.scan_file(path, "overworld")
    assert [chunk.sectors for chunk in biggest] == [9, 4, 1]


def test_only_the_top_n_chunks_are_kept(tmp_path):
    path = region(tmp_path / "r.0.0.mca", {index: index + 1 for index in range(20)})
    _, biggest = rg.scan_file(path, "overworld", keep=3)
    assert [chunk.sectors for chunk in biggest] == [20, 19, 18]


def test_a_zero_byte_region_is_empty_not_broken(tmp_path):
    path = tmp_path / "r.0.0.mca"
    path.write_bytes(b"")
    totals, biggest = rg.scan_file(path, "overworld")
    assert totals["chunks"] == 0 and biggest == []


def test_a_truncated_region_is_an_error(tmp_path):
    path = tmp_path / "r.0.0.mca"
    path.write_bytes(b"\0" * 100)
    with pytest.raises(ValueError, match="truncated"):
        rg.scan_file(path, "overworld")


def test_a_file_that_is_not_a_region_is_rejected(tmp_path):
    path = tmp_path / "notaregion.mca"
    path.write_bytes(b"\0" * HEADER)
    with pytest.raises(ValueError, match="not a region file"):
        rg.scan_file(path, "overworld")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("r.0.0.mca", (0, 0)), ("r.-2.3.mca", (-2, 3)), ("r.x.0.mca", None), ("nope", None)],
)
def test_region_names_parse_or_do_not(name, expected):
    assert rg.region_coordinates(name) == expected


# ------------------------------------------------------------------ a scan


def test_a_scan_merges_every_region(overworld):
    region(overworld / "r.0.0.mca", {0: 1, 1: 1})
    region(overworld / "r.1.0.mca", {0: 2})
    result = rg.scan(overworld, "overworld")
    assert result.regions == 2 and result.chunks == 3
    assert result.sectors == {1: 2, 2: 1}


def test_empty_regions_are_counted_separately(overworld):
    region(overworld / "r.0.0.mca", {0: 1})
    (overworld / "r.1.0.mca").write_bytes(b"")
    result = rg.scan(overworld, "overworld")
    assert result.regions == 2 and result.empty_regions == 1 and result.chunks == 1


def test_a_corrupt_region_is_named_not_fatal(overworld):
    region(overworld / "r.0.0.mca", {0: 1})
    (overworld / "r.1.0.mca").write_bytes(b"\0" * 10)
    result = rg.scan(overworld, "overworld")
    assert result.chunks == 1
    assert result.unreadable == ["r.1.0.mca"]


def test_bounds_cover_every_region_including_empty_ones(overworld):
    region(overworld / "r.-1.-1.mca", {0: 1})
    region(overworld / "r.2.3.mca", {0: 1})
    result = rg.scan(overworld, "overworld")
    assert result.bounds == (-1, 2, -1, 3)
    assert result.covered == 4 * 5 * CHUNKS_PER_REGION


def test_fill_is_generated_chunks_over_the_bounding_box(overworld):
    region(overworld / "r.0.0.mca", {index: 1 for index in range(CHUNKS_PER_REGION)})
    result = rg.scan(overworld, "overworld")
    assert result.fill == 1.0


def test_percentiles_read_off_the_histogram(overworld):
    region(overworld / "r.0.0.mca", {index: 1 for index in range(90)} | {90: 40})
    result = rg.scan(overworld, "overworld")
    assert result.percentile(0.5) == SECTOR
    assert result.percentile(1.0) == 40 * SECTOR


def test_slack_is_file_bytes_no_chunk_accounts_for(overworld):
    # Two sectors of chunk plus the header, and one spare sector of leftovers.
    region(overworld / "r.0.0.mca", {0: 2}, body=3 * SECTOR)
    result = rg.scan(overworld, "overworld")
    assert result.slack_bytes == SECTOR


def test_timestamps_bucket_chunks_by_the_day_they_were_written(overworld):
    from datetime import datetime

    noon = int(datetime(2026, 8, 23, 12).timestamp())
    region(overworld / "r.0.0.mca", {0: 1, 1: 1}, stamps={0: noon, 1: noon})
    result = rg.scan(overworld, "overworld")
    assert result.written[datetime(2026, 8, 23).date()] == 2


def test_an_empty_directory_scans_to_nothing(tmp_path):
    result = rg.scan(tmp_path, "overworld")
    assert result.chunks == 0 and result.bounds is None and result.fill == 0.0


# ------------------------------------------------------------------ the world


def test_dimensions_are_discovered_from_all_three_layouts(tmp_path):
    for path in (
        tmp_path / "world" / "region",
        tmp_path / "world" / "DIM-1" / "region",
        tmp_path / "world" / "dimensions" / "kattersstructures" / "deep_blue" / "region",
    ):
        path.mkdir(parents=True)
    found = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b").region_dirs()
    assert set(found) == {"overworld", "the_nether", "kattersstructures:deep_blue"}


def test_the_modern_layout_wins_over_the_legacy_one(tmp_path):
    (tmp_path / "world" / "dimensions" / "minecraft" / "overworld" / "region").mkdir(
        parents=True
    )
    (tmp_path / "world" / "region").mkdir(parents=True)
    found = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b").region_dirs()
    assert found["overworld"].parts[-2] == "overworld"


def test_scanning_a_world_orders_dimensions_by_chunk_count(tmp_path, overworld):
    nether = tmp_path / "world" / "dimensions" / "minecraft" / "the_nether" / "region"
    nether.mkdir(parents=True)
    region(overworld / "r.0.0.mca", {0: 1})
    region(nether / "r.0.0.mca", {0: 1, 1: 1, 2: 1})
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    assert [result.dimension for result in rg.scan_world(paths)] == [
        "the_nether",
        "overworld",
    ]


def test_as_dict_is_json_serialisable(tmp_path, overworld):
    region(overworld / "r.0.0.mca", {0: 1})
    paths = Paths(server_dir=tmp_path, backup_dir=tmp_path / "b")
    payload = json.loads(json.dumps(rg.as_dict(rg.scan_world(paths))))
    assert payload["totals"]["chunks"] == 1
