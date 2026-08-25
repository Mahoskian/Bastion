"""Tests for the restic repository wrapper, against a real repository."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from mcadmin.core.models import Paths
from mcadmin.core.repository import (
    RepoStats,
    ResticError,
    ResticMissing,
    ResticRepository,
    RetentionPolicy,
    Snapshot,
    SnapshotSummary,
)

needs_restic = pytest.mark.skipif(
    not ResticRepository.available(), reason="restic not installed"
)


@pytest.fixture
def server(tmp_path):
    """A miniature server tree plus an empty backup directory."""
    root = tmp_path / "server"
    for relative, content in {
        "world/level.dat": b"level",
        "world/session.lock": b"lock",
        "world/region/r.0.0.mca": b"region" * 1000,
        "world/data/DistantHorizons.sqlite": b"cache" * 1000,
        "config/DistantHorizons.toml": b"config",
        "server.properties": b"enable-rcon=false",
        "ops.json": b"[]",
        "admin/.metrics.db": b"metrics",
        "admin/mcadmin/core/repository.py": b"source",
        "admin/.venv/bin/python": b"binary",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    backups = tmp_path / "backups"
    backups.mkdir()
    return Paths(server_dir=root, backup_dir=backups)


@pytest.fixture
def repo(server):
    made = ResticRepository(server)
    made.init()
    return made


# ------------------------------------------------------------------ policy


def test_retention_maps_onto_restic_flags():
    args = RetentionPolicy(hourly=12, daily=7, weekly=4).forget_args()
    assert args == ["--keep-hourly", "12", "--keep-daily", "7", "--keep-weekly", "4"]


def test_retention_never_produces_an_empty_policy():
    """An empty policy would let forget delete every snapshot."""
    assert RetentionPolicy(hourly=0, daily=0, weekly=0, monthly=0).forget_args() == [
        "--keep-last",
        "1",
    ]


def test_snapshot_time_survives_nanosecond_precision():
    snapshot = Snapshot.model_validate(
        {
            "id": "abc123",
            "short_id": "abc123",
            "time": "2026-08-24T19:30:00.123456789-04:00",
        }
    )
    assert isinstance(snapshot.time, datetime)
    assert snapshot.time.microsecond == 123456


def test_dedup_ratio_reports_bytes_read_per_byte_stored():
    assert SnapshotSummary(total_bytes_processed=1000, data_added=10).dedup_ratio == 100.0


def test_summary_is_taken_from_the_last_json_line():
    stream = "\n".join(
        [
            json.dumps({"message_type": "status", "percent_done": 0.5}),
            json.dumps({"message_type": "summary", "data_added": 42, "files_new": 3}),
        ]
    )
    summary = ResticRepository._summary(stream)
    assert summary.data_added == 42
    assert summary.files_new == 3


def test_a_backup_with_no_summary_is_an_error():
    with pytest.raises(ResticError, match="no summary"):
        ResticRepository._summary('{"message_type": "status"}')


def test_leading_json_ignores_the_prune_text_that_follows_it():
    """`forget --prune` emits its JSON, then human-readable prune output."""
    stream = '[{"remove": [{"short_id": "abc"}]}]\nloading indexes...\ndone\n'
    assert ResticRepository._leading_json(stream) == [{"remove": [{"short_id": "abc"}]}]
    assert ResticRepository._leading_json("") == []


def test_a_missing_binary_is_reported_clearly(server):
    with pytest.raises(ResticMissing, match="not installed"):
        ResticRepository(server, binary="restic-does-not-exist")._run(["snapshots"])


# ------------------------------------------------------------------ repo


@needs_restic
def test_init_creates_a_repo_and_a_locked_down_password(server):
    repo = ResticRepository(server)
    assert not repo.exists()
    repo.init()
    assert repo.exists()
    assert repo.password_file.read_text().strip()
    assert repo.password_file.stat().st_mode & 0o777 == 0o600


@needs_restic
def test_init_refuses_to_clobber_an_existing_repo(repo):
    with pytest.raises(ResticError, match="already exists"):
        repo.init()


@needs_restic
def test_backup_honours_the_exclude_set(repo):
    repo.backup()
    listed = repo._run(["ls", "latest"]).stdout
    assert "level.dat" in listed
    assert "r.0.0.mca" in listed
    assert "DistantHorizons.toml" in listed, "the config is not the cache"
    assert "session.lock" not in listed
    assert "DistantHorizons.sqlite" not in listed


@needs_restic
def test_backup_takes_the_admin_databases_but_not_its_source(repo):
    """The tooling is git's job; the two databases it writes are nobody else's."""
    repo.backup()
    listed = repo._run(["ls", "latest"]).stdout
    assert ".metrics.db" in listed
    assert "repository.py" not in listed
    assert "/.venv/" not in listed


@needs_restic
def test_a_second_snapshot_of_unchanged_data_stores_almost_nothing(repo):
    """The entire reason for this module."""
    first = repo.backup()
    second = repo.backup()
    assert first.data_added > 0
    assert second.data_added < first.data_added / 10
    assert len(repo.snapshots()) == 2


@needs_restic
def test_snapshots_come_back_newest_first(repo):
    repo.backup()
    repo.backup()
    found = repo.snapshots()
    assert found[0].time >= found[1].time


@needs_restic
def test_resolve_accepts_latest_index_and_id(repo):
    repo.backup()
    repo.backup()
    found = repo.snapshots()
    assert repo.resolve("latest").id == found[0].id
    assert repo.resolve("2").id == found[1].id
    assert repo.resolve(found[1].short_id).id == found[1].id


def test_resolve_prefers_an_id_over_an_index(monkeypatch):
    """Roughly one short_id in forty is all digits; it must stay addressable."""
    numeric = Snapshot(id="12345678" + "0" * 56, short_id="12345678", time=datetime.now())
    other = Snapshot(id="deadbeef" + "0" * 56, short_id="deadbeef", time=datetime.now())
    repo = ResticRepository()
    monkeypatch.setattr(ResticRepository, "snapshots", lambda self, tag=None: [other, numeric])

    assert repo.resolve("12345678").short_id == "12345678"
    assert repo.resolve("1").short_id == "deadbeef"  # small numbers are still indices
    assert repo.resolve("2").short_id == "12345678"
    assert repo.resolve("latest").short_id == "deadbeef"
    assert repo.resolve("deadbeef0000").short_id == "deadbeef"  # long prefix still works


@needs_restic
def test_resolve_rejects_nonsense(repo):
    repo.backup()
    with pytest.raises(LookupError):
        repo.resolve("99")
    with pytest.raises(LookupError):
        repo.resolve("deadbeef")


@needs_restic
def test_resolve_on_an_empty_repo_says_so(repo):
    with pytest.raises(LookupError, match="no snapshots"):
        repo.resolve("latest")


@needs_restic
def test_check_passes_on_a_fresh_repo(repo):
    repo.backup()
    repo.check()


@needs_restic
def test_stats_report_physical_and_logical_size(repo):
    repo.backup()
    repo.backup()
    stats = repo.stats()
    assert stats.total_size > 0
    assert stats.total_file_count > 0
    assert stats.snapshots == 2
    # Two snapshots of the same data restore to twice what they occupy.
    assert stats.restore_size > stats.total_size
    assert stats.dedup_ratio > 1


def test_dedup_ratio_is_zero_on_an_empty_repo():
    assert RepoStats().dedup_ratio == 0.0


@needs_restic
def test_forget_collapses_same_hour_snapshots_and_spares_the_newest(repo):
    """restic keeps the newest snapshot plus the hourly keeper, so several
    snapshots taken inside one hour collapse -- but never to zero, and never
    at the expense of the most recent one."""
    for _ in range(4):
        repo.backup()
    newest = repo.snapshots()[0]

    removed = repo.forget()
    remaining = repo.snapshots()

    assert removed, "retention removed nothing"
    assert 0 < len(remaining) < 4
    assert newest.id in {s.id for s in remaining}, "the newest snapshot must survive"


@needs_restic
def test_forget_dry_run_removes_nothing(repo):
    for _ in range(3):
        repo.backup()
    before = len(repo.snapshots())
    repo.forget(dry_run=True)
    assert len(repo.snapshots()) == before


@needs_restic
def test_restore_strips_the_common_parent(repo, tmp_path):
    """Contents land directly under the target, not under a rebuilt absolute
    path. Getting this backwards sends an in-place restore to /."""
    repo.backup()
    target = tmp_path / "restored"
    repo.restore(repo.resolve("latest"), target)

    assert (target / "world" / "level.dat").read_bytes() == b"level"
    assert (target / "server.properties").exists()
    assert not (target / "world" / "session.lock").exists()
    assert not (target / "home").exists(), "the absolute path must not be rebuilt"


@needs_restic
def test_common_parent_is_the_server_directory(repo, server):
    repo.backup()
    assert repo.resolve("latest").common_parent == server.server_dir


def test_common_parent_of_a_single_path_is_its_directory():
    snapshot = Snapshot(id="a", short_id="a", time=datetime.now(), paths=[Path("/srv/mc/world")])
    assert snapshot.common_parent == Path("/srv/mc")


def test_common_parent_of_a_pathless_snapshot_is_an_error():
    snapshot = Snapshot(id="a", short_id="a", time=datetime.now(), paths=[])
    with pytest.raises(ResticError, match="records no paths"):
        _ = snapshot.common_parent


@needs_restic
def test_restore_in_place_puts_files_back_where_they_came_from(repo, server):
    repo.backup()
    (server.server_dir / "world" / "level.dat").write_bytes(b"corrupted")
    repo.restore_in_place(repo.resolve("latest"))
    assert (server.server_dir / "world" / "level.dat").read_bytes() == b"level"


@needs_restic
def test_restore_in_place_refuses_a_snapshot_rooted_elsewhere(repo, server, tmp_path):
    """The guard against restoring to a target the caller did not intend."""
    outside = tmp_path / "elsewhere" / "sub"
    outside.mkdir(parents=True)
    (outside / "file.txt").write_text("x")
    repo._run(["backup", str(outside)])
    with pytest.raises(ResticError, match="rooted at"):
        repo.restore_in_place(repo.resolve("latest"))
