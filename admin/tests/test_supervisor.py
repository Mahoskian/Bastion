"""Tests for the supervision loop.

The real JVM is replaced by `sleep`, so these exercise the loop itself:
restart-on-crash, the crash-loop guard, and the difference a stop request makes.
"""

import signal
import subprocess
import threading

import pytest

from mcadmin.core import supervisor as sup
from mcadmin.core.board import Board, Phase
from mcadmin.core.models import JvmOptions, Paths, RuntimeState
from mcadmin.core.notify import EventKind, Notice
from mcadmin.core.rcon import RconError


class FakeJvm(sup.Supervisor):
    """A supervisor whose 'JVM' is a sleep of a chosen duration.

    `exit_code` makes that sleep exit dirty instead of clean, which is the only
    thing separating a crash from an intentional restart further up.
    """

    def __init__(self, *args, lifetime: str = "0.05", exit_code: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lifetime = lifetime
        self.exit_code = exit_code
        self.launches = 0

    def _launch(self) -> subprocess.Popen:
        self.launches += 1
        if self.exit_code:
            return subprocess.Popen(["sh", "-c", f"sleep {self.lifetime}; exit {self.exit_code}"])
        return subprocess.Popen(["sleep", self.lifetime])


class FakeRcon:
    """A server that answers `list` and `tick query`, the way a booted one does.

    The roster it gives out is a class attribute so a test can have somebody
    join between one refresh and the next.
    """

    roster = "There are 0 of a max of 20 players online:"

    def command(self, text: str) -> str:
        if text == "tick query":
            return (
                "The game is running normallyTarget tick rate: 20.0 per second."
                "Average time per tick: 4.7ms (Target: 50.0ms)"
            )
        return self.roster

    def __enter__(self) -> "FakeRcon":
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never let a test reach the real RCON, and restore signal handlers."""
    (tmp_path / "admin").mkdir()
    (tmp_path / "logs").mkdir()

    def no_rcon(*args, **kwargs):
        raise RconError("no rcon in tests")

    monkeypatch.setattr(sup, "connect", no_rcon)
    monkeypatch.setattr(sup, "POLL_INTERVAL", 0.01)
    # The supervisor builds a notifier from the environment when given none;
    # a developer with a token exported must not make these tests post to it.
    monkeypatch.delenv("MC_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("MC_DISCORD_CHANNEL", raising=False)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield tmp_path
    for number, handler in handlers.items():
        signal.signal(number, handler)


@pytest.fixture
def paths(isolated):
    return Paths(server_dir=isolated)


@pytest.fixture
def answering_rcon(monkeypatch):
    """A server that answers `list`, and drops the act quickly when stopped.

    Without the shortened timeout a test would wait out the patience the
    supervisor extends to a real multi-gigabyte world save.
    """
    monkeypatch.setattr(sup, "READY_POLL", 0.01)
    monkeypatch.setattr(sup, "GRACEFUL_STOP_TIMEOUT", 0.2)
    monkeypatch.setattr(sup, "connect", lambda **_: FakeRcon())


def test_crash_loop_guard_stops_relaunching(paths):
    """A JVM that dies instantly must not be restarted forever."""
    supervisor = FakeJvm(paths, JvmOptions(), restart_delay=0.01, max_rapid_restarts=3)
    supervisor.run()
    assert supervisor.launches == 3


def test_a_healthy_run_resets_the_crash_counter(paths, monkeypatch):
    monkeypatch.setattr(sup, "MIN_HEALTHY_SECONDS", 0.01)
    supervisor = FakeJvm(paths, JvmOptions(), restart_delay=0.01, max_rapid_restarts=2)
    # Every run counts as healthy, so the guard never trips; stop it by hand.
    threading.Timer(0.5, supervisor.request_shutdown).start()
    supervisor.run()
    assert supervisor.launches > 2


def test_a_stop_request_prevents_the_restart(paths):
    supervisor = FakeJvm(paths, JvmOptions(), lifetime="30", restart_delay=0.01)
    threading.Timer(0.2, supervisor.request_shutdown).start()
    supervisor.run()
    assert supervisor.launches == 1


def test_a_stop_request_during_the_restart_delay_is_honoured(paths):
    supervisor = FakeJvm(paths, JvmOptions(), restart_delay=5.0, max_rapid_restarts=99)
    threading.Timer(0.3, supervisor.request_shutdown).start()
    supervisor.run()
    assert supervisor.launches == 1


def test_runtime_state_is_published_then_cleared(paths):
    supervisor = FakeJvm(paths, JvmOptions(heap="8G"), lifetime="30", restart_delay=0.01)
    seen: list[RuntimeState] = []

    def capture() -> None:
        state = RuntimeState.load(paths.runtime_file)
        if state is not None:
            seen.append(state)
        supervisor.request_shutdown()

    threading.Timer(0.3, capture).start()
    supervisor.run()

    assert seen, "the supervisor never published its runtime state"
    assert seen[0].heap == "8G"
    assert seen[0].jvm_pid is not None
    assert not paths.runtime_file.exists(), "runtime state outlived the supervisor"


# ------------------------------------------------------------------ the board


class Pinned:
    """A pinboard that keeps every board it was shown."""

    def __init__(self) -> None:
        self.shown: list[Board] = []

    def show(self, board: Board) -> None:
        self.shown.append(board)


def test_the_running_board_is_rewritten_while_nothing_transitions(
    paths, monkeypatch, answering_rcon
):
    """The bug this exists for: a roster taken when the server finished
    booting said "nobody" for the rest of the run, however many people joined.
    """
    monkeypatch.setattr(sup, "BOARD_REFRESH", 0.05)
    monkeypatch.setattr(FakeRcon, "roster", "There are 0 of a max of 20 players online:")
    board = Pinned()
    supervisor = FakeJvm(paths, JvmOptions(), lifetime="30", restart_delay=0.01, pinboard=board)

    def somebody_joins() -> None:
        FakeRcon.roster = "There are 1 of a max of 20 players online: Steve"

    threading.Timer(0.2, somebody_joins).start()
    threading.Timer(0.6, supervisor.request_shutdown).start()
    try:
        supervisor.run()
    finally:
        FakeRcon.roster = "There are 0 of a max of 20 players online:"

    running = [shown for shown in board.shown if shown.phase is Phase.RUNNING]
    assert len(running) > 2, "the board was only written on transitions"
    assert running[-1].players.names == ("Steve",), "the roster never caught up"
    assert running[-1].tick is not None, "the tick rate is read on the same visit"
    assert supervisor.launches == 1, "refreshing must not disturb the loop"


def test_a_refresh_never_overwrites_the_transition_that_overtook_it(paths, monkeypatch):
    """The probe takes a round trip, and a crash during it writes the board
    first. A refresh landing afterwards would put Running back -- and, since
    the copy it writes is Running too, it would keep refreshing that lie for
    the rest of the run."""
    monkeypatch.setattr(sup, "BOARD_REFRESH", 0.01)
    board = Pinned()
    supervisor = FakeJvm(paths, JvmOptions(), pinboard=board)
    supervisor._board = Board(phase=Phase.RUNNING)

    def overtaken_probe() -> sup.Live:
        # What a probe that lost the race looks like from the refresher: by
        # the time it returns, the loop has already written the JVM's exit.
        supervisor._show(Phase.CRASHED)
        supervisor.request_shutdown()  # one pass is enough
        return sup.Live("There are 0 of a max of 20 players online:", None, None)

    monkeypatch.setattr(supervisor, "_probe", overtaken_probe)
    supervisor._refresh_board()

    assert board.shown[-1].phase is Phase.CRASHED, "a stale refresh won the race"


def test_only_a_running_board_is_refreshed(paths, monkeypatch):
    """Booting has nothing to ask the server for, and a restart's countdown is
    rendered by the client -- so neither is worth an RCON round trip."""
    monkeypatch.setattr(sup, "BOARD_REFRESH", 0.01)
    supervisor = FakeJvm(paths, JvmOptions(), pinboard=Pinned())
    supervisor._board = Board(phase=Phase.RESTARTING)
    probes = 0

    def counting_probe() -> None:
        nonlocal probes
        probes += 1
        return None

    monkeypatch.setattr(supervisor, "_probe", counting_probe)
    threading.Timer(0.1, supervisor.request_shutdown).start()
    supervisor._refresh_board()

    assert probes == 0


def test_stop_falls_back_to_sigterm_when_rcon_is_unavailable(paths):
    supervisor = FakeJvm(paths, JvmOptions(), lifetime="30")
    process = subprocess.Popen(["sleep", "30"])
    supervisor._stop_jvm(process)
    assert process.poll() is not None, "the process should have been terminated"


def test_request_shutdown_is_idempotent(paths):
    supervisor = FakeJvm(paths, JvmOptions())
    supervisor.request_shutdown()
    supervisor.request_shutdown()
    assert supervisor._shutdown.is_set()


# ------------------------------------------------------ announcing

# Two channels now, and which one a transition goes down is the whole point:
# the board changes silently on every transition, and the notifier fires only
# for the ones that should reach a phone.


class Recorder:
    """A notifier that remembers, so a test can assert on the events raised."""

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def send(self, notice: Notice) -> None:
        self.notices.append(notice)

    @property
    def kinds(self) -> list[EventKind]:
        return [notice.kind for notice in self.notices]


class Pinboard:
    """A board that remembers every phase it was moved through."""

    def __init__(self) -> None:
        self.boards: list[Board] = []

    def show(self, board: Board) -> None:
        self.boards.append(board)

    @property
    def phases(self) -> list[Phase]:
        return [board.phase for board in self.boards]


def watched(paths, **kwargs) -> tuple[FakeJvm, Recorder, Pinboard]:
    recorder, pinboard = Recorder(), Pinboard()
    supervisor = FakeJvm(paths, JvmOptions(), notifier=recorder, pinboard=pinboard, **kwargs)
    return supervisor, recorder, pinboard


def test_a_requested_stop_only_moves_the_board(paths):
    """The noisy case that started all this: nobody needs a notification about
    a stop they typed themselves."""
    supervisor, recorder, pinboard = watched(paths, lifetime="30")
    threading.Timer(0.2, supervisor.request_shutdown).start()
    supervisor.run()
    assert pinboard.phases == [Phase.BOOTING, Phase.STOPPED]
    assert recorder.kinds == []


def test_a_crash_moves_the_board_and_notifies(paths):
    """The one transition worth interrupting somebody for."""
    supervisor, recorder, pinboard = watched(paths, exit_code=1, restart_delay=0.01)
    supervisor.run()
    assert Phase.CRASHED in pinboard.phases
    assert recorder.kinds.count(EventKind.CRASHED) >= 1


def test_a_clean_exit_the_supervisor_did_not_ask_for_reads_as_a_restart(paths):
    """This is `mc restart`: it stops the JVM and leaves the supervisor alone,
    so the loop sees a clean exit it never requested and brings the server
    back. Announcing that as a crash would cry wolf on every restart."""
    supervisor, recorder, pinboard = watched(paths, restart_delay=0.01)
    supervisor.run()
    assert Phase.RESTARTING in pinboard.phases
    assert Phase.CRASHED not in pinboard.phases
    # A restart-loop of clean exits still trips the guard at the end -- what
    # must not happen is any of those exits being reported as a crash.
    assert EventKind.CRASHED not in recorder.kinds


def test_a_restart_carries_the_time_it_will_be_back(paths):
    """The countdown is a Discord timestamp, so it ticks down in the client
    without this ever editing the message again."""
    supervisor, _, pinboard = watched(paths, restart_delay=0.01)
    supervisor.run()
    restarting = next(b for b in pinboard.boards if b.phase is Phase.RESTARTING)
    assert restarting.resumes_at is not None
    assert f":{int(restarting.resumes_at.timestamp())}:R>" in str(restarting.embed())


def test_the_crash_loop_guard_announces_giving_up(paths):
    supervisor, recorder, pinboard = watched(paths, restart_delay=0.01, max_rapid_restarts=2)
    supervisor.run()
    assert pinboard.phases[-1] is Phase.ABANDONED
    assert recorder.kinds == [EventKind.ABANDONED]


def test_the_board_turns_green_once_the_server_answers_rcon(paths, answering_rcon):
    """The JVM being alive is not the same as the server being playable."""
    supervisor, recorder, pinboard = watched(paths, lifetime="30")
    threading.Timer(0.4, supervisor.request_shutdown).start()
    supervisor.run()
    assert Phase.RUNNING in pinboard.phases
    assert recorder.kinds == []


def test_the_running_board_counts_the_players_rcon_named(paths, answering_rcon):
    """The readiness check already asked `list`; the count is free from it."""
    supervisor, _, pinboard = watched(paths, lifetime="30")
    threading.Timer(0.4, supervisor.request_shutdown).start()
    supervisor.run()
    running = next(b for b in pinboard.boards if b.phase is Phase.RUNNING)
    assert running.players is not None
    assert running.players.maximum == 20


def test_the_running_board_dates_from_the_launch_not_the_boot_finishing(paths, answering_rcon):
    """`Up since` should answer how long the server has been there."""
    supervisor, _, pinboard = watched(paths, lifetime="30")
    threading.Timer(0.4, supervisor.request_shutdown).start()
    supervisor.run()
    booting = next(b for b in pinboard.boards if b.phase is Phase.BOOTING)
    running = next(b for b in pinboard.boards if b.phase is Phase.RUNNING)
    assert running.since == booting.since


def test_a_notifier_that_raises_never_breaks_the_loop(paths):
    """Discord being down must not be able to stop the server from running."""

    class Broken:
        def send(self, notice: Notice) -> None:
            raise RuntimeError("discord is on fire")

    supervisor = FakeJvm(paths, JvmOptions(), lifetime="30", notifier=Broken())
    threading.Timer(0.2, supervisor.request_shutdown).start()
    supervisor.run()  # the exit code is the JVM's; what matters is reaching here
    assert supervisor.launches == 1


def test_a_board_that_raises_never_breaks_the_loop(paths):
    """The same promise, for the channel that now carries most transitions."""

    class Broken:
        def show(self, board: Board) -> None:
            raise RuntimeError("discord is still on fire")

    supervisor = FakeJvm(paths, JvmOptions(), lifetime="30", pinboard=Broken())
    threading.Timer(0.2, supervisor.request_shutdown).start()
    supervisor.run()
    assert supervisor.launches == 1


def test_an_unconfigured_server_still_supervises(paths):
    """No config file in `paths`, so both channels default to the null one."""
    supervisor = FakeJvm(paths, JvmOptions(), lifetime="30")
    threading.Timer(0.2, supervisor.request_shutdown).start()
    supervisor.run()
    assert supervisor.launches == 1
