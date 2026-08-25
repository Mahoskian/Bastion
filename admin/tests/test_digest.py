"""Tests for the digest: sessions, the baseline, and what counts as new."""

from datetime import datetime, timedelta

import pytest

from mcadmin.core import digest as dg
from mcadmin.core.logs import Event, EventKind, Level, Record


def at(minute: int, hour: int = 10, day: int = 24) -> datetime:
    return datetime(2026, 8, day, hour, minute)


def event(kind: EventKind, minute: int, player: str = "", hour: int = 10) -> Event:
    return Event(kind=kind, at=at(minute, hour), player=player)


def problem_record(message: str, minute: int, level: Level = Level.ERROR) -> Record:
    return Record(at=at(minute), thread="Server thread", level=level, message=message)


# ------------------------------------------------------------------ sessions


def test_a_join_and_leave_make_one_session():
    (session,) = dg.build_sessions(
        [
            event(EventKind.JOIN, 0, "Steve"),
            event(EventKind.LEAVE, 30, "Steve"),
        ]
    )
    assert session.duration() == timedelta(minutes=30)
    assert not session.still_online


def test_an_unclosed_join_means_still_online():
    (session,) = dg.build_sessions([event(EventKind.JOIN, 0, "Steve")])
    assert session.still_online
    assert session.duration(now=at(15)) == timedelta(minutes=15)


def test_a_shutdown_closes_open_sessions():
    """The bug this guards: a restart emits no 'left the game', so playtime
    for whoever was online runs to the present."""
    sessions = dg.build_sessions(
        [
            event(EventKind.JOIN, 0, "Steve"),
            event(EventKind.SERVER_STOP, 20),
        ]
    )
    assert len(sessions) == 1
    assert not sessions[0].still_online
    assert sessions[0].duration() == timedelta(minutes=20)


def test_a_restart_closes_sessions_at_the_last_thing_logged():
    """A crash leaves no stop marker; the session must not span the downtime."""
    sessions = dg.build_sessions(
        [
            event(EventKind.JOIN, 0, "Steve"),
            event(EventKind.CHAT, 10, "Steve"),
            event(EventKind.SERVER_START, 0, hour=14),
        ]
    )
    assert sessions[0].duration() == timedelta(minutes=10)


def test_playtime_sums_across_sessions():
    sessions = dg.build_sessions(
        [
            event(EventKind.JOIN, 0, "Steve"),
            event(EventKind.LEAVE, 20, "Steve"),
            event(EventKind.JOIN, 30, "Steve"),
            event(EventKind.LEAVE, 40, "Steve"),
        ]
    )
    digest = dg.Digest(sessions=sessions)
    assert digest.playtime()["Steve"] == timedelta(minutes=30)


def test_playtime_never_exceeds_the_window(tmp_path):
    """The property that failed on real logs before restarts closed sessions."""
    events = [
        event(EventKind.JOIN, 0, "Steve"),
        event(EventKind.SERVER_STOP, 30),
        event(EventKind.SERVER_START, 0, hour=11),
        event(EventKind.JOIN, 10, "Steve", hour=11),
        event(EventKind.SERVER_STOP, 40, hour=11),
    ]
    digest = dg.Digest(sessions=dg.build_sessions(events))
    window = at(40, hour=11) - at(0)
    assert digest.playtime()["Steve"] <= window


def test_a_second_join_without_a_leave_still_records_the_first():
    sessions = dg.build_sessions(
        [event(EventKind.JOIN, 0, "Steve"), event(EventKind.JOIN, 20, "Steve")]
    )
    assert len(sessions) == 2


# ------------------------------------------------------------------ grouping


def test_identical_problems_group_into_one_with_a_count():
    grouped = dg.collect_problems(
        [problem_record("chunk [1, 2] failed", 0), problem_record("chunk [9, 9] failed", 5)]
    )
    (problem,) = grouped.values()
    assert problem.count == 2
    assert problem.first_seen == at(0)
    assert problem.last_seen == at(5)


def test_info_records_are_not_problems():
    assert dg.collect_problems([problem_record("hello", 0, level=Level.INFO)]) == {}


# ------------------------------------------------------------------ baseline


@pytest.fixture
def baseline(tmp_path):
    return dg.Baseline(tmp_path / "baseline.db")


def test_a_fresh_baseline_knows_nothing(baseline):
    assert baseline.known() == set()
    assert baseline.size() == 0


def test_remembering_makes_a_problem_known(baseline):
    problems = dg.collect_problems([problem_record("boom", 0)])
    baseline.remember(problems.values())
    assert baseline.known() == set(problems)
    assert baseline.size() == 1


def test_remembering_twice_accumulates_rather_than_duplicating(baseline):
    problems = list(dg.collect_problems([problem_record("boom", 0)]).values())
    baseline.remember(problems)
    baseline.remember(problems)
    assert baseline.size() == 1


def test_reset_forgets_everything(baseline):
    baseline.remember(dg.collect_problems([problem_record("boom", 0)]).values())
    baseline.reset()
    assert baseline.size() == 0


# ------------------------------------------------------------------ digest


def test_the_first_run_learns_instead_of_crying_wolf(baseline):
    digest = dg.build([problem_record("boom", 0)], baseline)
    assert digest.learned
    assert digest.new_problems, "the patterns are still collected, just not called new"


def test_the_second_run_reports_nothing_new(baseline):
    records = [problem_record("boom", 0)]
    first = dg.build(records, baseline)
    baseline.remember(first.new_problems)

    second = dg.build(records, baseline)
    assert not second.learned
    assert second.new_problems == []
    assert len(second.recurring) == 1


def test_a_genuinely_new_problem_surfaces(baseline):
    """The whole point: familiar noise stays quiet, novelty does not."""
    baseline.remember(dg.build([problem_record("known noise", 0)], baseline).new_problems)

    digest = dg.build(
        [problem_record("known noise", 5), problem_record("something else entirely", 6)],
        baseline,
    )
    assert [p.sample for p in digest.new_problems] == ["something else entirely"]
    assert [p.sample for p in digest.recurring] == ["known noise"]


def test_recurring_problems_are_ranked_by_volume_and_capped(baseline):
    # Distinct *words*, not distinct numbers -- numbers normalize to one
    # fingerprint, which is exactly what the grouping is for.
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    noisy = [problem_record(f"failure in {word}", i) for i, word in enumerate(words)]
    baseline.remember(dg.build(noisy, baseline).new_problems)
    digest = dg.build(noisy, baseline, recurring_limit=3)
    assert len(digest.recurring) == 3
    assert len(dg.collect_problems(noisy)) == len(words)


def test_an_empty_window_digests_to_nothing(baseline):
    digest = dg.build([], baseline)
    assert digest.records == 0
    assert digest.new_problems == []
