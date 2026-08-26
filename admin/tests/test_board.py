"""Tests for the pinned status board.

The board's whole reason to exist is that it edits one message instead of
posting a new one, so most of what is worth pinning here is *which* request
went out: a PATCH means the channel stayed quiet, a POST means it did not.
`DiscordBot` is replaced by a recorder rather than exercised over a fake
urlopen -- the transport already has its own tests in `test_notify`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from mcadmin.core.board import (
    Board,
    DiscordPinboard,
    NullPinboard,
    Phase,
    PinState,
    pinboard_for,
    stamp,
)
from mcadmin.core.controller import Online, Status
from mcadmin.core.models import Paths, RuntimeState, ServerState
from mcadmin.core.notify import DiscordConfig, NotifyError

TOKEN = "MTIzNDU2Nzg5.GaBcDe.f4kE-t0ken-for-tests"
CHANNEL = "123456789012345678"


class FakeBot:
    """A DiscordBot that records instead of sending.

    `edit_status` makes it fail an edit the way Discord would when the message
    is gone (404) or the bot lost permission to touch it (403), which are the
    two cases the board has to tell apart.
    """

    def __init__(self, channel_id: str = CHANNEL, edit_status: int | None = None) -> None:
        self.config = DiscordConfig(token=TOKEN, channel_id=channel_id)
        self.edit_status = edit_status
        self.posted: list[dict] = []
        self.edited: list[tuple[str, dict]] = []
        self.pinned: list[str] = []
        self.pin_fails = False

    def post(self, embed, attachments=()) -> str:
        self.posted.append(embed)
        return "777"

    def edit(self, message_id: str, embed) -> None:
        if self.edit_status is not None:
            raise NotifyError(f"Discord returned {self.edit_status}.", self.edit_status)
        self.edited.append((message_id, embed))

    def pin(self, message_id: str) -> None:
        if self.pin_fails:
            raise NotifyError("no Manage Messages here (403).", 403)
        self.pinned.append(message_id)


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    """The environment configures Discord too, so a developer with a token
    exported must not turn the factory tests into live ones."""
    monkeypatch.delenv("MC_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("MC_DISCORD_CHANNEL", raising=False)


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / ".board.json"


@pytest.fixture
def running() -> Board:
    return Board(phase=Phase.RUNNING, since=datetime.now(), heap="12G")


# ----------------------------------------------------------------- rendering


def test_the_title_says_the_state_in_the_words_that_were_asked_for(running):
    assert "Server Status: Running" in str(running.embed()["title"])


def test_every_phase_renders():
    """A phase with no presentation would raise inside the supervisor, which
    swallows it -- so the board would silently stop updating."""
    for phase in Phase:
        embed = Board(phase=phase).embed()
        assert embed["title"] and isinstance(embed["color"], int)


def test_times_go_out_as_discord_markup_not_as_text(running):
    """The client re-renders these against its own clock, which is what lets a
    message edited twice a day never read as stale."""
    assert stamp(running.since) in str(running.embed()["description"])


def test_a_scheduled_restart_carries_its_countdown():
    back = datetime.now() + timedelta(seconds=10)
    board = Board(phase=Phase.RESTARTING, resumes_at=back)
    assert f"Back {stamp(back)}" in str(board.embed()["description"])


def test_a_long_roster_is_trimmed_rather_than_refused():
    """Discord rejects a field value over 1024 outright."""
    crowd = Online(online=200, maximum=200, names=tuple(f"player{n:04}" for n in range(200)))
    field = Board(phase=Phase.RUNNING, players=crowd).embed()["fields"][0]
    assert len(field["value"]) <= 1024


def test_an_empty_server_says_so_rather_than_showing_a_blank_field():
    board = Board(phase=Phase.RUNNING, players=Online(online=0, maximum=20))
    assert board.embed()["fields"][0]["value"] == "nobody"


def test_a_board_with_nothing_to_add_has_no_fields():
    assert "fields" not in Board(phase=Phase.STOPPED).embed()


def test_a_time_nobody_knows_is_left_out_rather_than_guessed():
    """A stopped server left nothing behind saying when it stopped. Defaulting
    that to now would read as "stopped just now" on a box down for a week."""
    embed = Board(phase=Phase.STOPPED).embed()
    assert "description" not in embed
    assert "Stopped" in str(embed["title"])


# ----------------------------------------------------------------- observing


def observed(state: ServerState, runtime: RuntimeState | None = None) -> Board:
    status = Status(
        state=state, pid=None, session_exists=False, runtime=runtime, players=None
    )
    return Board.observed(status)


def test_an_observer_can_only_report_what_it_can_see():
    """`mc notify board` reads the live server, and nothing looking from
    outside can tell a crash from any other absent JVM."""
    assert observed(ServerState.RUNNING).phase is Phase.RUNNING
    assert observed(ServerState.BOOTING).phase is Phase.BOOTING
    assert observed(ServerState.STOPPED).phase is Phase.STOPPED


def test_an_orphaned_session_reads_as_stopped_and_says_why():
    board = observed(ServerState.ORPHANED)
    assert board.phase is Phase.STOPPED
    assert "tmux" in (board.detail or "")


def test_an_observed_board_without_a_supervisor_dates_from_nothing():
    """There is no runtime file to read a start time out of, and inventing one
    is worse than showing a state with no age against it."""
    assert observed(ServerState.STOPPED).since is None


def test_an_observed_board_takes_its_age_from_the_running_supervisor():
    started = datetime.now() - timedelta(hours=3)
    runtime = RuntimeState(
        supervisor_pid=1, jvm_pid=2, heap="8G", restarts=4, started_at=started
    )
    board = observed(ServerState.RUNNING, runtime)
    assert board.since == started
    assert board.heap == "8G"
    assert board.restarts == 4


# ----------------------------------------------------------------- publishing


def test_the_first_show_posts_and_pins_and_remembers_the_message(state_file, running):
    bot = FakeBot()
    DiscordPinboard(bot, state_file).show(running)

    assert len(bot.posted) == 1
    assert bot.pinned == ["777"]
    assert PinState.load(state_file).message_id == "777"


def test_every_show_after_the_first_is_an_edit(state_file, running):
    """The point of the whole exercise: one message, however many transitions."""
    bot = FakeBot()
    board = DiscordPinboard(bot, state_file)
    board.show(running)
    board.show(Board(phase=Phase.STOPPED))
    board.show(Board(phase=Phase.BOOTING))

    assert len(bot.posted) == 1, "a transition must never add a message"
    assert [message_id for message_id, _ in bot.edited] == ["777", "777"]


def test_a_deleted_message_is_replaced(state_file, running):
    """Deleting the message is a supported way to start the board over."""
    state_file.write_text(PinState(channel_id=CHANNEL, message_id="404").model_dump_json())
    bot = FakeBot(edit_status=404)
    warnings: list[str] = []

    DiscordPinboard(bot, state_file, warnings.append).show(running)

    assert len(bot.posted) == 1
    assert PinState.load(state_file).message_id == "777"
    assert warnings and "gone" in warnings[0]


def test_a_permissions_failure_raises_instead_of_posting_a_replacement(state_file, running):
    """A 403 is a standing condition, not a missing message. Replacing on every
    transition would rebuild exactly the feed the board was meant to end."""
    state_file.write_text(PinState(channel_id=CHANNEL, message_id="403").model_dump_json())
    bot = FakeBot(edit_status=403)

    with pytest.raises(NotifyError):
        DiscordPinboard(bot, state_file).show(running)
    assert bot.posted == []


def test_a_board_left_in_another_channel_is_not_edited_there(state_file, running):
    """Moving the channel in .notify.json must not leave this updating a
    live-looking board in the channel everyone left."""
    state_file.write_text(
        PinState(channel_id="999999999999999999", message_id="1").model_dump_json()
    )
    bot = FakeBot()

    DiscordPinboard(bot, state_file).show(running)

    assert bot.edited == []
    assert len(bot.posted) == 1
    assert PinState.load(state_file).channel_id == CHANNEL


def test_a_message_that_cannot_be_pinned_is_still_a_board(state_file, running):
    """Pinning needs Manage Messages; sending does not. Losing the pin costs
    the board its place at the top, not its existence."""
    bot = FakeBot()
    bot.pin_fails = True
    warnings: list[str] = []

    DiscordPinboard(bot, state_file, warnings.append).show(running)

    assert PinState.load(state_file).message_id == "777"
    assert warnings and "could not pin" in warnings[0]


def test_an_unreadable_state_file_costs_one_new_message(state_file, running):
    state_file.write_text("{not json")
    bot = FakeBot()
    DiscordPinboard(bot, state_file).show(running)
    assert len(bot.posted) == 1


# ----------------------------------------------------------------- factory


def test_an_unconfigured_server_gets_a_board_that_does_nothing(tmp_path):
    (tmp_path / "admin").mkdir()
    assert isinstance(pinboard_for(Paths(server_dir=tmp_path)), NullPinboard)


def test_a_broken_config_warns_but_never_blocks_the_boot(tmp_path):
    (tmp_path / "admin").mkdir()
    paths = Paths(server_dir=tmp_path)
    paths.notify_config.write_text("{not json")
    warnings: list[str] = []

    board = pinboard_for(paths, warnings.append)

    assert isinstance(board, NullPinboard), "a bad config must not stop the server"
    assert warnings and "status board is off" in warnings[0]


def test_enabled_false_turns_the_board_off_too(tmp_path):
    """One switch for Discord, not one per kind of message."""
    (tmp_path / "admin").mkdir()
    paths = Paths(server_dir=tmp_path)
    paths.notify_config.write_text(
        json.dumps({"token": TOKEN, "channel_id": CHANNEL, "enabled": False})
    )
    assert isinstance(pinboard_for(paths), NullPinboard)
