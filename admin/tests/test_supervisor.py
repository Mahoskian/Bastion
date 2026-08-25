"""Tests for the supervision loop.

The real JVM is replaced by `sleep`, so these exercise the loop itself:
restart-on-crash, the crash-loop guard, and the difference a stop request makes.
"""

import signal
import subprocess
import threading

import pytest

from mcadmin.core import supervisor as sup
from mcadmin.core.models import JvmOptions, Paths, RuntimeState
from mcadmin.core.rcon import RconError


class FakeJvm(sup.Supervisor):
    """A supervisor whose 'JVM' is a sleep of a chosen duration."""

    def __init__(self, *args, lifetime: str = "0.05", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lifetime = lifetime
        self.launches = 0

    def _launch(self) -> subprocess.Popen:
        self.launches += 1
        return subprocess.Popen(["sleep", self.lifetime])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never let a test reach the real RCON, and restore signal handlers."""
    (tmp_path / "admin").mkdir()
    (tmp_path / "logs").mkdir()

    def no_rcon(*args, **kwargs):
        raise RconError("no rcon in tests")

    monkeypatch.setattr(sup, "connect", no_rcon)
    monkeypatch.setattr(sup, "POLL_INTERVAL", 0.01)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield tmp_path
    for number, handler in handlers.items():
        signal.signal(number, handler)


@pytest.fixture
def paths(isolated):
    return Paths(server_dir=isolated)


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
