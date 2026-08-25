"""Tests for the metrics store, derived series, and the sampler."""

from datetime import datetime, timedelta

import pytest

from mcadmin.core.metrics import (
    GcRecord,
    MetricsStore,
    Sample,
    Sampler,
    cpu_percent,
    series,
)
from mcadmin.core.models import Paths, ServerState
from mcadmin.core.units import parse_duration
from mcadmin.ui.metrics import sparkline


def at(minute: int, hour: int = 12) -> datetime:
    return datetime(2026, 8, 24, hour, minute)


def sample(minute: int, **fields) -> Sample:
    return Sample(at=at(minute), state=ServerState.RUNNING, **fields)


@pytest.fixture
def store(tmp_path):
    return MetricsStore(tmp_path / "metrics.db")


# ------------------------------------------------------------------ store


def test_a_fresh_store_is_empty(store):
    assert store.count() == 0
    assert store.samples() == []


def test_samples_round_trip(store):
    store.add(sample(0, players=3, rss_bytes=1024, heap_used=512, world_bytes=99))
    (loaded,) = store.samples()
    assert loaded.players == 3
    assert loaded.rss_bytes == 1024
    assert loaded.state is ServerState.RUNNING


def test_samples_come_back_in_time_order(store):
    for minute in (5, 1, 3):
        store.add(sample(minute))
    assert [s.at.minute for s in store.samples()] == [1, 3, 5]


def test_since_filters_the_window(store):
    for minute in range(5):
        store.add(sample(minute))
    assert len(store.samples(since=at(3))) == 2


def test_two_samples_in_the_same_second_both_survive(store):
    """The timestamp is not the key -- a fast loop must not lose rows."""
    store.add(sample(0, players=1))
    store.add(sample(0, players=2))
    assert store.count() == 2


def test_prune_drops_only_the_old(store):
    now = datetime.now()
    store.add(Sample(at=now - timedelta(days=100), state=ServerState.STOPPED))
    store.add(Sample(at=now, state=ServerState.RUNNING))
    assert store.prune(timedelta(days=90)) == 1
    assert store.count() == 1


def test_missing_values_stay_missing(store):
    """A stopped server has no player count; that is not zero."""
    store.add(Sample(at=at(0), state=ServerState.STOPPED))
    (loaded,) = store.samples()
    assert loaded.players is None
    assert loaded.rss_bytes is None


# ------------------------------------------------------------------ gc runs


def make_gc(started: datetime, **fields) -> GcRecord:
    return GcRecord(run_started=started, recorded_at=datetime.now(), **fields)


def test_gc_runs_round_trip(store):
    store.record_gc(make_gc(at(0), live_bytes=1000, p99_ms=12.5, collections=7))
    (loaded,) = store.gc_runs()
    assert loaded.live_bytes == 1000
    assert loaded.p99_ms == 12.5
    assert loaded.collections == 7


def test_recording_the_same_run_updates_it(store):
    """A run in progress grows; re-recording must refresh, not duplicate."""
    store.record_gc(make_gc(at(0), collections=10))
    store.record_gc(make_gc(at(0), collections=25))
    (loaded,) = store.gc_runs()
    assert loaded.collections == 25


def test_gc_runs_are_newest_first(store):
    store.record_gc(make_gc(at(0)))
    store.record_gc(make_gc(at(30)))
    assert [r.run_started.minute for r in store.gc_runs()] == [30, 0]


def test_gc_runs_respect_the_limit(store):
    for minute in range(5):
        store.record_gc(make_gc(at(minute)))
    assert len(store.gc_runs(limit=2)) == 2


# ------------------------------------------------------------------ series


def test_cpu_percent_is_a_rate_between_samples():
    samples = [sample(0, cpu_seconds=100.0), sample(1, cpu_seconds=130.0)]
    (point,) = cpu_percent(samples).points
    assert point[1] == pytest.approx(50.0)  # 30s of CPU over 60s of wall


def test_cpu_percent_needs_two_samples():
    assert cpu_percent([sample(0, cpu_seconds=1.0)]).points == []


def test_a_restart_does_not_produce_a_negative_rate():
    """The counter resets to zero when the JVM restarts."""
    samples = [sample(0, cpu_seconds=500.0), sample(1, cpu_seconds=2.0)]
    assert cpu_percent(samples).points == []


def test_series_skips_missing_values():
    result = series([sample(0, players=2), sample(1), sample(2, players=4)], "players")
    assert result.values == [2.0, 4.0]
    assert result.latest == 4.0
    assert result.peak == 4.0


def test_an_empty_series_has_no_statistics():
    result = series([], "players")
    assert result.latest is None and result.peak is None and result.mean is None


# ------------------------------------------------------------------ sparkline


def test_sparkline_tracks_the_shape():
    assert sparkline([0, 1, 2, 3], width=4)[0] == "▁"
    assert sparkline([0, 1, 2, 3], width=4)[-1] == "█"


def test_a_nearly_flat_series_renders_flat():
    """Min-max scaling would turn a 0.01% wobble in a 14G RSS into a cliff."""
    values = [14_000_000_000, 14_000_100_000, 14_000_000_000]
    assert set(sparkline(values)) == {"▁"}


def test_a_genuinely_varying_series_is_not_flattened():
    assert len(set(sparkline([0, 50, 100]))) > 1


def test_sparkline_buckets_down_to_the_width():
    assert len(sparkline(list(range(1000)), width=20)) == 20


def test_an_empty_sparkline_is_empty():
    assert sparkline([]) == ""


# ------------------------------------------------------------------ sampler


def test_sampler_reports_a_stopped_server_without_process_fields(tmp_path, monkeypatch):
    from mcadmin.core import process
    from mcadmin.core.controller import ServerController

    monkeypatch.setattr(process, "server_pid", lambda *a, **k: None)
    monkeypatch.setattr(process, "server_running", lambda *a, **k: False)
    monkeypatch.setattr(ServerController, "state", lambda self: ServerState.STOPPED)

    taken = Sampler(Paths(server_dir=tmp_path)).take(world_size=False)
    assert taken.state is ServerState.STOPPED
    assert taken.rss_bytes is None
    assert taken.players is None


def test_world_size_can_be_skipped(tmp_path, monkeypatch):
    from mcadmin.core.controller import ServerController

    monkeypatch.setattr(ServerController, "state", lambda self: ServerState.STOPPED)
    assert Sampler(Paths(server_dir=tmp_path)).take(world_size=False).world_bytes is None


def test_proc_reads_survive_a_process_that_just_exited(tmp_path):
    """The pid can vanish between finding it and reading /proc."""
    rss, cpu = Sampler(Paths(server_dir=tmp_path))._proc(999_999_999)
    assert rss is None and cpu is None


# ------------------------------------------------------------------ windows


@pytest.mark.parametrize(
    ("text", "expected"),
    [("90m", timedelta(minutes=90)), ("6h", timedelta(hours=6)), ("3d", timedelta(days=3))],
)
def test_duration_syntax(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "6", "6y", "-3d", "abc"])
def test_bad_durations_are_rejected(text):
    with pytest.raises(ValueError):
        parse_duration(text)
