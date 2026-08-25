"""Tests for the lifecycle controller.

The distinction these lock down: `stop` signals the supervisor (so the session
dies with it), `restart` does not (so the session survives).
"""

import signal

import pytest

from mcadmin.core import controller as ctl
from mcadmin.core import process
from mcadmin.core.models import JvmOptions, Paths, RuntimeState, ServerState


class FakeSession:
    """Stands in for tmux without needing a terminal."""

    def __init__(self, exists: bool = False, name: str = "mctest") -> None:
        self.name = name
        self._exists = exists
        self.created: list[list[str]] = []
        self.killed = 0
        self.typed: list[str] = []

    def exists(self) -> bool:
        return self._exists

    def create(self, command) -> None:
        self.created.append(list(command))
        self._exists = True

    def kill(self) -> bool:
        killed, self._exists = self._exists, False
        self.killed += 1
        return killed

    def send_keys(self, text: str) -> bool:
        self.typed.append(text)
        return self._exists


@pytest.fixture
def control(tmp_path, monkeypatch):
    (tmp_path / "admin").mkdir()
    monkeypatch.setattr(ctl.TmuxSession, "available", staticmethod(lambda: True))
    return ctl.ServerController(paths=Paths(server_dir=tmp_path), session=FakeSession())


def set_world(monkeypatch, control, *, running=False, rcon=False, alive=True):
    monkeypatch.setattr(process, "server_running", lambda *a, **k: running)
    monkeypatch.setattr(process, "server_pid", lambda *a, **k: 999 if running else None)
    monkeypatch.setattr(process, "alive", lambda pid: alive)
    monkeypatch.setattr(ctl.ServerController, "rcon_ready", lambda self: rcon)


# ------------------------------------------------------------------ state


@pytest.mark.parametrize(
    ("running", "rcon", "session", "expected"),
    [
        (True, True, True, ServerState.RUNNING),
        (True, False, True, ServerState.BOOTING),
        (False, False, True, ServerState.ORPHANED),
        (False, False, False, ServerState.STOPPED),
    ],
)
def test_state_reports_each_condition(control, monkeypatch, running, rcon, session, expected):
    set_world(monkeypatch, control, running=running, rcon=rcon)
    control.session._exists = session
    assert control.state() is expected


def test_a_stale_runtime_file_is_ignored(control, monkeypatch):
    RuntimeState(supervisor_pid=4242).save(control.paths.runtime_file)
    monkeypatch.setattr(process, "alive", lambda pid: False)
    assert control.runtime() is None


def test_a_live_runtime_file_is_read(control, monkeypatch):
    RuntimeState(supervisor_pid=4242, heap="24G").save(control.paths.runtime_file)
    monkeypatch.setattr(process, "alive", lambda pid: True)
    assert control.runtime().heap == "24G"


# ------------------------------------------------------------------ start


def test_start_launches_the_supervisor_in_a_session(control, monkeypatch):
    set_world(monkeypatch, control)
    control.start(JvmOptions(heap="8G"))
    assert control.session.created, "no tmux session was created"
    command = control.session.created[0]
    assert command[1:] == ["supervise", "--heap", "8G"]


def test_start_refuses_when_the_server_is_already_running(control, monkeypatch):
    set_world(monkeypatch, control, running=True, rcon=True)
    with pytest.raises(ctl.LifecycleError, match="already running"):
        control.start()


def test_start_refuses_when_a_supervisor_is_alive(control, monkeypatch):
    set_world(monkeypatch, control)
    RuntimeState(supervisor_pid=4242).save(control.paths.runtime_file)
    with pytest.raises(ctl.LifecycleError, match="supervisor is already running"):
        control.start()


def test_start_refuses_when_a_session_is_squatting(control, monkeypatch):
    set_world(monkeypatch, control)
    control.session._exists = True
    with pytest.raises(ctl.LifecycleError, match="holds no server"):
        control.start()


def test_start_clears_a_runtime_file_left_by_a_dead_supervisor(control, monkeypatch):
    set_world(monkeypatch, control, alive=False)
    control.paths.runtime_file.write_text("{}")
    control.start()
    assert control.session.created


# ------------------------------------------------------------------ stop


def test_stop_signals_the_supervisor_and_closes_the_session(control, monkeypatch):
    RuntimeState(supervisor_pid=4242).save(control.paths.runtime_file)
    control.session._exists = True
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(ctl.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    # The server goes away once the supervisor has been told to stop.
    monkeypatch.setattr(process, "server_running", lambda *a, **k: bool(not signalled))
    monkeypatch.setattr(process, "alive", lambda pid: bool(not signalled))

    control.stop(timeout=5.0)
    assert signalled == [(4242, signal.SIGTERM)]
    assert not control.session.exists(), "stop must take the tmux session with it"
    assert not control.paths.runtime_file.exists()


def test_stop_without_a_supervisor_asks_the_jvm_directly(control, monkeypatch):
    control.session._exists = True
    monkeypatch.setattr(process, "server_running", lambda *a, **k: False)
    monkeypatch.setattr(ctl.ServerController, "_ask_jvm_to_stop", lambda self: None)
    control.stop(timeout=1.0)
    assert control.session.killed == 1


def test_stop_reports_a_server_that_will_not_die(control, monkeypatch):
    set_world(monkeypatch, control, running=True)
    monkeypatch.setattr(ctl.ServerController, "_ask_jvm_to_stop", lambda self: None)
    with pytest.raises(ctl.LifecycleError, match="still running"):
        control.stop(timeout=0.1)


def test_force_stop_clears_a_leftover_session(control):
    control.session._exists = True
    control.paths.runtime_file.write_text("{}")
    assert control.force_stop() is True
    assert not control.paths.runtime_file.exists()


# ------------------------------------------------------------------ restart


def test_restart_leaves_the_supervisor_and_session_alone(control, monkeypatch):
    RuntimeState(supervisor_pid=4242).save(control.paths.runtime_file)
    control.session._exists = True
    monkeypatch.setattr(process, "alive", lambda pid: True)
    monkeypatch.setattr(process, "server_running", lambda *a, **k: True)
    monkeypatch.setattr(ctl.os, "kill", lambda pid, sig: pytest.fail("restart must not signal"))

    pids = iter([100, 100, 200, 200, 200])
    monkeypatch.setattr(process, "server_pid", lambda *a, **k: next(pids))
    asked: list[str] = []
    monkeypatch.setattr(ctl.ServerController, "_ask_jvm_to_stop", lambda self: asked.append("stop"))

    control.restart(timeout=5.0)
    assert asked == ["stop"]
    assert control.session.exists(), "restart must keep the session alive"
    assert control.session.killed == 0


def test_restart_needs_a_supervisor_to_restart_into(control, monkeypatch):
    set_world(monkeypatch, control, running=True, alive=False)
    with pytest.raises(ctl.LifecycleError, match="no supervisor"):
        control.restart()


def test_restart_refuses_when_the_server_is_down(control, monkeypatch):
    RuntimeState(supervisor_pid=4242).save(control.paths.runtime_file)
    monkeypatch.setattr(process, "alive", lambda pid: True)
    monkeypatch.setattr(process, "server_running", lambda *a, **k: False)
    with pytest.raises(ctl.LifecycleError, match="not running"):
        control.restart()


def test_restart_reports_a_server_that_never_comes_back(control, monkeypatch):
    RuntimeState(supervisor_pid=4242).save(control.paths.runtime_file)
    monkeypatch.setattr(process, "alive", lambda pid: True)
    monkeypatch.setattr(process, "server_running", lambda *a, **k: True)
    monkeypatch.setattr(process, "server_pid", lambda *a, **k: 100)
    monkeypatch.setattr(ctl.ServerController, "_ask_jvm_to_stop", lambda self: None)
    with pytest.raises(ctl.LifecycleError, match="did not come back"):
        control.restart(timeout=0.1)
