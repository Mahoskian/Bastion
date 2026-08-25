"""Tests for correlating tick lag, GC pauses, players and pregen."""

from datetime import datetime, timedelta, timezone

import pytest

from mcadmin.core import slow as sl
from mcadmin.core.gclog import Level, Pause, Run, Safepoint
from mcadmin.core.logs import Level as LogLevel
from mcadmin.core.logs import Record
from mcadmin.core.units import parse_moment
from mcadmin.ui.slow import strip

BUCKET = timedelta(minutes=10)
EAST = timezone(timedelta(hours=-4))


def record(minute: int, message: str, level: LogLevel = LogLevel.INFO, hour: int = 19) -> Record:
    return Record(
        at=datetime(2026, 8, 23, hour, minute),
        thread="Server thread",
        level=level,
        message=message,
    )


def behind(minute: int, ms: int) -> Record:
    return record(
        minute,
        f"Can't keep up! Is the server overloaded? Running {ms}ms or {ms // 50} ticks behind",
        LogLevel.WARN,
    )


def chunky(minute: int, processed: int, rate: float = 78.4) -> Record:
    return record(
        minute,
        f"[Chunky] Task running for minecraft:overworld. Processed: {processed} chunks "
        f"(51.00%), ETA: 0:40:19, Rate: {rate} cps, Current: -199, -247",
    )


def pause(minute: int, ms: float, kind: str = "Young") -> Pause:
    return Pause(
        gc_id=1,
        at=datetime(2026, 8, 23, 19, minute, tzinfo=EAST),
        uptime=100.0,
        kind=kind,
        subkind="Normal",
        cause="G1 Evacuation Pause",
        before=0,
        after=0,
        capacity=0,
        ms=ms,
    )


def run_with(*pauses: Pause, safepoints: list[Safepoint] | None = None) -> Run:
    return Run(
        started=datetime(2026, 8, 23, 19, tzinfo=EAST),
        pauses=list(pauses),
        safepoints=list(safepoints or []),
    )


# ------------------------------------------------------------------ bucketing


def test_lag_warnings_land_in_the_bucket_that_covers_them():
    timeline = sl.build([behind(2, 2000), behind(4, 5000), behind(15, 1000)], size=BUCKET)
    first = timeline.at(datetime(2026, 8, 23, 19, 0))
    second = timeline.at(datetime(2026, 8, 23, 19, 15))
    assert (first.lag_events, first.lag_ms, first.worst_lag_ms) == (2, 7000, 5000)
    assert second.lag_events == 1


def test_gc_pauses_are_matched_by_wall_clock_across_timezones():
    timeline = sl.build([behind(2, 2000)], runs=[run_with(pause(3, 40.0))], size=BUCKET)
    bucket = timeline.at(datetime(2026, 8, 23, 19, 0))
    assert bucket.gc_pauses == 1 and bucket.gc_ms == 40.0


def test_lost_time_is_lag_and_gc_together():
    timeline = sl.build([behind(2, 2000)], runs=[run_with(pause(3, 500.0))], size=BUCKET)
    assert timeline.at(datetime(2026, 8, 23, 19, 0)).lost_ms == 2500


def test_pregen_progress_is_the_difference_across_the_bucket():
    timeline = sl.build([chunky(1, 200_000), chunky(9, 240_000)], size=BUCKET)
    bucket = timeline.at(datetime(2026, 8, 23, 19, 0))
    assert bucket.pregen_chunks == 40_000
    assert bucket.pregen_dimension == "minecraft:overworld"


def test_the_pregen_rate_is_the_peak_the_bucket_saw():
    timeline = sl.build([chunky(1, 1, rate=20.0), chunky(2, 2, rate=90.0)], size=BUCKET)
    assert timeline.at(datetime(2026, 8, 23, 19, 0)).pregen_rate == 90.0


def test_errors_and_warnings_are_counted_apart():
    records = [
        record(1, "bad", LogLevel.ERROR),
        record(2, "worse", LogLevel.FATAL),
        record(3, "meh", LogLevel.WARN),
        record(4, "fine"),
    ]
    bucket = sl.build(records, size=BUCKET).at(datetime(2026, 8, 23, 19, 0))
    assert bucket.problems == 3 and bucket.errors == 2


def test_a_lag_warning_is_not_also_counted_as_a_problem_pattern():
    bucket = sl.build([behind(1, 2000)], size=BUCKET).at(datetime(2026, 8, 23, 19, 0))
    assert bucket.lag_events == 1 and bucket.problems == 1


def test_players_already_online_before_the_window_still_count():
    records = [
        record(0, "Steve joined the game", hour=18),
        behind(2, 3000),
    ]
    timeline = sl.build(
        records, since=datetime(2026, 8, 23, 19, 0), size=BUCKET
    )
    bucket = timeline.at(datetime(2026, 8, 23, 19, 0))
    assert bucket.players_peak == 1, "a session that started earlier is still a session"
    assert bucket.joins == 0


def test_joins_inside_the_window_are_counted():
    records = [record(1, "Steve joined the game"), record(2, "Alex joined the game")]
    bucket = sl.build(records, size=BUCKET).at(datetime(2026, 8, 23, 19, 0))
    assert bucket.joins == 2 and bucket.players_peak == 2


def test_a_leaver_does_not_inflate_the_peak():
    records = [
        record(1, "Steve joined the game"),
        record(2, "Steve left the game"),
        record(3, "Alex joined the game"),
    ]
    bucket = sl.build(records, size=BUCKET).at(datetime(2026, 8, 23, 19, 0))
    assert bucket.players_peak == 1


def test_no_records_is_an_empty_timeline():
    timeline = sl.build([], size=BUCKET)
    assert timeline.buckets == [] and timeline.worst is None


def test_records_before_the_window_are_left_out():
    timeline = sl.build(
        [behind(2, 9000)], since=datetime(2026, 8, 23, 20, 0), size=BUCKET
    )
    assert timeline.buckets == []


# ------------------------------------------------------------------ GC cover


def test_a_bucket_the_gc_log_reaches_is_marked_known():
    timeline = sl.build([behind(2, 2000)], runs=[run_with(pause(3, 40.0))], size=BUCKET)
    assert timeline.at(datetime(2026, 8, 23, 19, 0)).gc_known
    assert timeline.gc_covered


def test_a_bucket_the_gc_log_never_covered_is_marked_unknown():
    timeline = sl.build([behind(2, 2000)], runs=[], size=BUCKET)
    assert not timeline.at(datetime(2026, 8, 23, 19, 0)).gc_known
    assert not timeline.gc_covered


def test_the_heap_is_not_cleared_on_evidence_that_does_not_exist():
    timeline = sl.build([behind(2, 20_000)], runs=[], size=BUCKET)
    messages = [f.message for f in sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))]
    assert any("No GC log covers" in message for message in messages)
    assert not any("Not the heap" in message for message in messages)


def test_the_heap_is_cleared_when_the_gc_log_does_cover_the_window():
    timeline = sl.build([behind(2, 20_000)], runs=[run_with(pause(3, 40.0))], size=BUCKET)
    messages = [f.message for f in sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))]
    assert any("Not the heap" in message for message in messages)


# ------------------------------------------------------------------ explaining


def test_gc_is_blamed_when_it_accounts_for_most_of_the_loss():
    timeline = sl.build([behind(2, 2000)], runs=[run_with(pause(3, 9000.0))], size=BUCKET)
    findings = sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))
    assert any("This one is the heap" in f.message for f in findings)


def test_a_full_gc_is_always_an_error():
    timeline = sl.build(
        [behind(2, 2000)], runs=[run_with(pause(3, 900.0, kind="Full"))], size=BUCKET
    )
    findings = sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))
    assert any(f.level is Level.ERROR and "full GC" in f.message for f in findings)


def test_a_pregen_is_named_as_the_loudest_thing():
    records = [behind(2, 30_000), chunky(1, 100), chunky(9, 40_000, rate=90.0)]
    timeline = sl.build(records, runs=[run_with(pause(3, 10.0))], size=BUCKET)
    findings = sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))
    assert any("pregeneration" in f.message for f in findings)
    assert any(f.level is Level.WARN for f in findings)


def test_a_quiet_bucket_says_so_rather_than_inventing_a_cause():
    timeline = sl.build([record(1, "nothing happened")], size=BUCKET)
    findings = sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))
    assert len(findings) == 1 and "Nothing lost tick time" in findings[0].message


def test_the_lag_finding_admits_it_is_a_floor():
    timeline = sl.build([behind(2, 5000)], size=BUCKET)
    findings = sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))
    assert any("at least" in f.message for f in findings)


def test_non_gc_safepoints_are_called_out_when_they_dominate():
    point = Safepoint(
        at=datetime(2026, 8, 23, 19, 3, tzinfo=EAST),
        uptime=100.0,
        name="ThreadDump",
        total_ns=800_000_000,
        reach_ns=0,
    )
    timeline = sl.build(
        [behind(2, 5000)],
        runs=[run_with(pause(3, 10.0), safepoints=[point])],
        size=BUCKET,
    )
    findings = sl.explain(timeline.at(datetime(2026, 8, 23, 19, 0)))
    assert any("non-GC safepoints" in f.message for f in findings)


# ------------------------------------------------------------------ selection


def test_the_worst_bucket_is_the_one_that_lost_the_most():
    timeline = sl.build([behind(2, 2000), behind(15, 90_000)], size=BUCKET)
    assert timeline.worst.start == datetime(2026, 8, 23, 19, 10)


def test_only_buckets_past_the_threshold_are_worth_explaining():
    timeline = sl.build([behind(2, 500), behind(15, 90_000)], size=BUCKET)
    assert [bucket.start.minute for bucket in timeline.bad] == [10]


def test_neighbours_come_back_either_side():
    timeline = sl.build([behind(minute, 3000) for minute in (2, 12, 22, 32)], size=BUCKET)
    focus = timeline.at(datetime(2026, 8, 23, 19, 20))
    assert len(timeline.around(focus, span=1)) == 3


# ------------------------------------------------------------------ moments


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-08-24 18:00", datetime(2026, 8, 24, 18, 0)),
        ("yesterday 18:00", datetime(2026, 8, 24, 18, 0)),
        ("today 09:30", datetime(2026, 8, 25, 9, 30)),
        ("6pm", datetime(2026, 8, 24, 18, 0)),
        ("09:30", datetime(2026, 8, 25, 9, 30)),
    ],
)
def test_moments_parse_the_way_people_write_them(text, expected):
    assert parse_moment(text, now=datetime(2026, 8, 25, 15, 0)) == expected


def test_a_clock_time_still_ahead_of_now_means_yesterday():
    assert parse_moment("18:00", now=datetime(2026, 8, 25, 15, 0)).day == 24


def test_an_unreadable_moment_says_what_it_accepts():
    with pytest.raises(ValueError, match="6pm"):
        parse_moment("sometime tuesday")


# ------------------------------------------------------------------ rendering


def test_the_strip_folds_to_a_fixed_width():
    timeline = sl.build([behind(minute, 3000) for minute in range(0, 60)], size=BUCKET)
    marks, columns = strip(timeline.buckets, width=4)
    assert columns == 4 and marks


def test_folding_keeps_the_worst_bucket_rather_than_averaging_it_away():
    records = [behind(1, 90_000)] + [record(minute, "quiet") for minute in range(2, 59)]
    timeline = sl.build(records, size=timedelta(minutes=1))
    marks, _ = strip(timeline.buckets, width=4)
    assert "█" in marks, "an outage must survive being folded"


def test_a_quiet_window_renders_as_quiet():
    timeline = sl.build([record(minute, "quiet") for minute in range(0, 30)], size=BUCKET)
    marks, _ = strip(timeline.buckets)
    assert "█" not in marks
