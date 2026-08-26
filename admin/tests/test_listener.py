"""Tests for managing the Discord listener as a process.

The tmux session is faked, so these exercise the decisions -- refusing to start
twice, noticing a state file whose process is gone, clearing a session that
outlived its listener -- rather than tmux itself.
"""

from __future__ import annotations

import os

import pytest

from mcadmin.core.controller import LifecycleError
from mcadmin.core.listener import ListenerController, ListenerState
from mcadmin.core.models import LISTENER_SESSION, Paths
from mcadmin.core.tmux import TmuxSession


class FakeSession(TmuxSession):
    """A tmux session that exists only in this object."""

    model_config = {"frozen": False}

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_live", False)
        object.__setattr__(self, "_commands", [])

    @staticmethod
    def available() -> bool:
        return True

    def exists(self) -> bool:
        return self._live

    def create(self, command) -> None:
        self._commands.append(list(command))
        object.__setattr__(self, "_live", True)

    def kill(self) -> bool:
        was = self._live
        object.__setattr__(self, "_live", False)
        return was


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "admin").mkdir()
    return Paths(server_dir=tmp_path)


@pytest.fixture
def control(paths):
    return ListenerController(paths=paths, session=FakeSession())


def test_the_listener_gets_its_own_session_not_the_servers():
    """Sharing the server's session would take the bot down with every stop."""
    assert LISTENER_SESSION != "minecraft"
    assert ListenerController(paths=Paths()).session.name == LISTENER_SESSION


def test_a_fresh_listener_is_not_running(control):
    assert not control.running()
    assert control.state() is None


def test_starting_creates_a_detached_session(control):
    control.start()
    assert control.session.exists()
    assert control.session._commands[0][1:] == ["listen", "run"]


def test_starting_twice_is_refused(control, paths):
    ListenerState(pid=os.getpid()).save(paths.listener_file)
    with pytest.raises(LifecycleError, match="already running"):
        control.start()


def test_a_session_with_no_listener_in_it_is_refused_not_overwritten(control):
    """Creating over it would orphan whatever is actually in there."""
    control.session.create(["something", "else"])
    with pytest.raises(LifecycleError, match="holds no listener"):
        control.start()


def test_a_state_file_whose_process_is_gone_is_ignored(control, paths):
    """A listener that died hard leaves this behind; start must not misread it."""
    ListenerState(pid=999_999_999).save(paths.listener_file)
    assert control.state() is None
    control.start()  # must not raise
    assert control.session.exists()


def test_a_stale_state_file_is_cleared_on_start(control, paths):
    ListenerState(pid=999_999_999).save(paths.listener_file)
    control.start()
    assert not paths.listener_file.exists() or ListenerState.load(paths.listener_file) is None


def test_an_unreadable_state_file_is_not_fatal(control, paths):
    paths.listener_file.write_text("{not json")
    assert control.state() is None


def test_stopping_when_nothing_runs_still_clears_the_session(control):
    control.session.create(["stale"])
    assert control.force_stop()
    assert not control.session.exists()


def test_a_running_listener_reports_itself(control, paths):
    ListenerState(pid=os.getpid(), bot="Bastion#0001", guilds=["Bastion SMP"]).save(
        paths.listener_file
    )
    state = control.state()
    assert state is not None
    assert state.connected
    assert state.guilds == ["Bastion SMP"]


def test_started_but_not_connected_is_its_own_state(control, paths):
    """The process can be up while the gateway handshake has not finished."""
    ListenerState(pid=os.getpid()).save(paths.listener_file)
    state = control.state()
    assert state is not None and not state.connected


def test_stopping_removes_the_state_file(control, paths):
    control.session.create(["listener"])
    ListenerState(pid=999_999_999).save(paths.listener_file)
    control.stop()
    assert not paths.listener_file.exists()
    assert not control.session.exists()
