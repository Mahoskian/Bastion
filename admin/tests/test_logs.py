"""Tests for log parsing and event extraction."""

import gzip
from datetime import date, datetime

import pytest

from mcadmin.core import logs as lg
from mcadmin.core.logs import EventKind, Level, Record, normalize


def line(time: str, message: str, thread: str = "Server thread", level: str = "INFO") -> str:
    return f"[{time}] [{thread}/{level}]: {message}\n"


def write(tmp_path, name: str, text: str):
    path = tmp_path / name
    if name.endswith(".gz"):
        path.write_bytes(gzip.compress(text.encode()))
    else:
        path.write_text(text)
    return path


# ------------------------------------------------------------------ parsing


def test_parses_time_thread_level_and_message(tmp_path):
    path = write(tmp_path, "2026-08-24-1.log", line("17:30:13", "Steve joined the game"))
    (record,) = list(lg.parse_file(path))
    assert record.at == datetime(2026, 8, 24, 17, 30, 13)
    assert record.thread == "Server thread"
    assert record.level is Level.INFO
    assert record.message == "Steve joined the game"


def test_continuation_lines_belong_to_the_entry_above(tmp_path):
    text = (
        line("17:00:00", "Something failed", level="ERROR")
        + "java.lang.IllegalStateException: broken\n"
        + "\tat net.minecraft.Thing.method(Thing.java:42)\n"
        + line("17:00:01", "Next entry")
    )
    path = write(tmp_path, "2026-08-24-1.log", text)
    first, second = list(lg.parse_file(path))
    assert len(first.continuation) == 2
    assert second.continuation == ()


def test_gzipped_and_plain_files_both_parse(tmp_path):
    plain = write(tmp_path, "2026-08-24-1.log", line("10:00:00", "a"))
    gzipped = write(tmp_path, "2026-08-24-2.log.gz", line("11:00:00", "b"))
    assert len(list(lg.parse_file(plain))) == 1
    assert len(list(lg.parse_file(gzipped))) == 1


def test_date_comes_from_the_rotated_filename(tmp_path):
    path = write(tmp_path, "2026-08-23-4.log.gz", line("10:00:00", "x"))
    assert lg.file_date(path) == date(2026, 8, 23)


def test_clock_going_backwards_rolls_over_midnight(tmp_path):
    text = line("23:59:58", "before") + line("00:00:02", "after")
    path = write(tmp_path, "2026-08-24-1.log", text)
    before, after = list(lg.parse_file(path))
    assert before.at.day == 24
    assert after.at.day == 25, "a wrapped clock must advance the date"


def test_log_files_are_ordered_with_latest_last(tmp_path):
    write(tmp_path, "2026-08-24-2.log.gz", "")
    write(tmp_path, "2026-08-23-1.log.gz", "")
    write(tmp_path, "latest.log", "")
    names = [p.name for p in lg.log_files(tmp_path)]
    assert names == ["2026-08-23-1.log.gz", "2026-08-24-2.log.gz", "latest.log"]


def test_unparseable_leading_lines_are_not_records(tmp_path):
    path = write(tmp_path, "2026-08-24-1.log", "garbage\n" + line("10:00:00", "real"))
    (record,) = list(lg.parse_file(path))
    assert record.message == "real"


# ------------------------------------------------------------------ fingerprints


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("chunk [224, 87]", "chunk [65, -86]"),
        ("BlockPos{x=-179, y=191, z=150}", "BlockPos{x=1, y=2, z=3}"),
        (
            "player 3fe07686-72e1-458c-b0b1-de151c87b441",
            "player 00000000-0000-0000-0000-000000000000",
        ),
        ("§4overloaded§r", "overloaded"),
    ],
)
def test_varying_details_collapse_to_one_fingerprint(left, right):
    assert normalize(left) == normalize(right)


def test_different_messages_keep_different_fingerprints():
    assert normalize("Empty pool") != normalize("Broken pool")


def test_the_exception_type_separates_otherwise_identical_entries():
    def record(exception: str) -> Record:
        return Record(
            at=datetime(2026, 8, 24, 10, 0),
            thread="Server thread",
            level=Level.ERROR,
            message="Error executing task",
            continuation=(f"{exception}: boom",),
        )

    a = record("java.util.ConcurrentModificationException")
    b = record("java.lang.NullPointerException")
    assert a.fingerprint != b.fingerprint


def test_summary_surfaces_the_exception_type():
    record = Record(
        at=datetime(2026, 8, 24, 10, 0),
        thread="t",
        level=Level.ERROR,
        message="Error executing task",
        continuation=("java.util.ConcurrentModificationException: boom",),
    )
    assert "ConcurrentModificationException" in record.summary


def test_only_warn_and_worse_count_as_problems():
    assert Level.ERROR.is_problem and Level.WARN.is_problem and Level.FATAL.is_problem
    assert not Level.INFO.is_problem and not Level.DEBUG.is_problem


# ------------------------------------------------------------------ events


def records_from(tmp_path, *messages: tuple[str, str]) -> list[Record]:
    text = "".join(line(time, message) for time, message in messages)
    return list(lg.parse_file(write(tmp_path, "2026-08-24-1.log", text)))


def test_extracts_the_player_event_vocabulary(tmp_path):
    events = lg.extract_events(
        records_from(
            tmp_path,
            ("10:00:00", "Steve joined the game"),
            ("10:00:05", "<Steve> hello"),
            ("10:00:10", "Steve has made the advancement [Diamonds!]"),
            ("10:00:20", "Steve drowned"),
            ("10:00:30", "Steve lost connection: Disconnected"),
            ("10:00:31", "Steve left the game"),
        )
    )
    kinds = [event.kind for event in events]
    assert kinds == [
        EventKind.JOIN,
        EventKind.CHAT,
        EventKind.ADVANCEMENT,
        EventKind.DEATH,
        EventKind.DISCONNECT,
        EventKind.LEAVE,
    ]


def test_console_say_is_attributed_to_the_server(tmp_path):
    (event,) = lg.extract_events(
        records_from(tmp_path, ("10:00:00", "[Not Secure] [Server] hello everyone"))
    )
    assert event.kind is EventKind.CHAT
    assert event.player == "Server"


def test_a_villager_death_is_not_a_player_death(tmp_path):
    events = lg.extract_events(
        records_from(
            tmp_path,
            ("10:00:00", "Steve joined the game"),
            (
                "10:00:10",
                "Villager Villager['Unemployed'/7605, l='ServerLevel[world]', "
                "x=1023.97, y=72.00, z=32.00] died, message: 'Unemployed drowned'",
            ),
        )
    )
    kinds = {event.kind for event in events}
    assert EventKind.MOB_DEATH in kinds
    assert EventKind.DEATH not in kinds


def test_mob_death_records_cause_and_place(tmp_path):
    (event,) = [
        e
        for e in lg.extract_events(
            records_from(
                tmp_path,
                (
                    "10:00:10",
                    "Villager Villager['Unemployed'/1, l='ServerLevel[world]', "
                    "x=542.53, y=61.83, z=615.25] died, message: 'Unemployed drowned'",
                ),
            )
        )
        if e.kind is EventKind.MOB_DEATH
    ]
    assert event.detail == "drowned @ 543,62,615"


def test_a_death_line_for_an_unknown_name_is_ignored(tmp_path):
    """Without a join, the name is not known to be a player."""
    events = lg.extract_events(records_from(tmp_path, ("10:00:00", "Steve drowned")))
    assert not [e for e in events if e.kind is EventKind.DEATH]


def test_known_players_can_be_supplied_directly(tmp_path):
    events = lg.extract_events(
        records_from(tmp_path, ("10:00:00", "Steve drowned")), players={"Steve"}
    )
    assert [e.kind for e in events] == [EventKind.DEATH]


def test_server_lifecycle_is_an_event(tmp_path):
    events = lg.extract_events(
        records_from(
            tmp_path,
            ("10:00:00", "Starting minecraft server version 26.2"),
            ("10:05:00", "Stopping server"),
        )
    )
    assert [e.kind for e in events] == [EventKind.SERVER_START, EventKind.SERVER_STOP]
