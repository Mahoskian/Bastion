"""Tests for the tmux session wrapper. Exercises real tmux where available."""

import os

import pytest

from mcadmin.core.tmux import TmuxError, TmuxSession

needs_tmux = pytest.mark.skipif(not TmuxSession.available(), reason="tmux not installed")


@pytest.fixture
def session():
    """A uniquely named session so tests never touch the real one."""
    made = TmuxSession(name=f"mctest-{os.getpid()}")
    yield made
    made.kill()


@needs_tmux
def test_create_then_kill(session):
    assert not session.exists()
    session.create(["sleep", "30"])
    assert session.exists()
    assert session.kill()
    assert not session.exists()


@needs_tmux
def test_create_refuses_to_clobber_an_existing_session(session):
    session.create(["sleep", "30"])
    with pytest.raises(TmuxError, match="already exists"):
        session.create(["sleep", "30"])


@needs_tmux
def test_killing_an_absent_session_reports_false(session):
    assert session.kill() is False


@needs_tmux
def test_send_keys_needs_a_session(session):
    assert session.send_keys("stop") is False
    session.create(["sleep", "30"])
    assert session.send_keys("hello") is True


def test_session_name_defaults_to_the_shared_constant():
    from mcadmin.core.models import SESSION_NAME

    assert TmuxSession().name == SESSION_NAME
