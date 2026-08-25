"""Supervising the JVM: launch it, restart it when it crashes, stop when asked.

This is the process that lives inside the tmux session, and it replaces the
`while true` loop a shell script used to run. Owning it in Python is what lets
`mc stop` and `mc restart` differ: a stop signals *this* process, which shuts
the JVM down and exits (taking the session with it), whereas a restart only
stops the JVM, which this loop then brings straight back up in place.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from types import FrameType

from .models import JvmOptions, Paths, RuntimeState
from .rcon import RconError, connect

POLL_INTERVAL = 0.5
# Saving a multi-gigabyte world is not quick; only escalate after real patience.
GRACEFUL_STOP_TIMEOUT = 180.0
SIGTERM_GRACE = 60.0
RESTART_DELAY = 10.0
# A JVM that dies faster than this never really came up.
MIN_HEALTHY_SECONDS = 60.0
MAX_RAPID_RESTARTS = 3


class Supervisor:
    """Runs one server, restarting it until told to stop."""

    def __init__(
        self,
        paths: Paths,
        jvm: JvmOptions,
        restart_delay: float = RESTART_DELAY,
        max_rapid_restarts: int = MAX_RAPID_RESTARTS,
    ) -> None:
        self.paths = paths
        self.jvm = jvm
        self.restart_delay = restart_delay
        self.max_rapid_restarts = max_rapid_restarts
        self._shutdown = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._restarts = 0

    # ------------------------------------------------------------- output

    def _say(self, message: str) -> None:
        print(f"[mc] {datetime.now():%H:%M:%S} {message}", flush=True)

    # ------------------------------------------------------------- signals

    def request_shutdown(self, *_: object) -> None:
        """Idempotent: repeated signals must not stack up shutdown attempts."""
        if not self._shutdown.is_set():
            self._shutdown.set()

    def _install_handlers(self) -> None:
        def handler(signum: int, _frame: FrameType | None) -> None:
            self._say(f"received {signal.Signals(signum).name} -- shutting down.")
            self.request_shutdown()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    # ------------------------------------------------------------- state

    def _publish(self, jvm_pid: int | None) -> None:
        RuntimeState(
            supervisor_pid=os.getpid(),
            jvm_pid=jvm_pid,
            heap=self.jvm.heap,
            restarts=self._restarts,
        ).save(self.paths.runtime_file)

    def _clear_state(self) -> None:
        self.paths.runtime_file.unlink(missing_ok=True)

    # ------------------------------------------------------------- the JVM

    def _launch(self) -> subprocess.Popen[bytes]:
        command = self.jvm.command(self.paths.jar(), self.paths.logs_dir)
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        self._say(f"launching server (heap {self.jvm.heap})")
        # stdio is inherited so the tmux pane *is* the server console.
        return subprocess.Popen(command, cwd=self.paths.server_dir)

    def _stop_jvm(self, process: subprocess.Popen[bytes]) -> None:
        """Ask nicely, then insist. Never SIGKILL a saving server early."""
        try:
            with connect(timeout=5.0) as client:
                client.command("stop")
            self._say("sent 'stop' over RCON; waiting for the world to save.")
        except (RconError, OSError):
            self._say("RCON unavailable -- sending SIGTERM instead.")
            process.terminate()

        if self._wait_for_exit(process, GRACEFUL_STOP_TIMEOUT):
            return
        self._say(f"still running after {GRACEFUL_STOP_TIMEOUT:.0f}s -- SIGTERM.")
        process.terminate()
        if self._wait_for_exit(process, SIGTERM_GRACE):
            return
        self._say("[WARNING] unresponsive to SIGTERM -- SIGKILL. The world may need repair.")
        process.kill()
        self._wait_for_exit(process, 30.0)

    def _wait_for_exit(self, process: subprocess.Popen[bytes], timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _run_once(self) -> int:
        """Launch the JVM and stay with it until it exits."""
        process = self._launch()
        self._process = process
        self._publish(process.pid)
        stop_sent = False
        while process.poll() is None:
            if self._shutdown.is_set() and not stop_sent:
                stop_sent = True
                self._stop_jvm(process)
            time.sleep(POLL_INTERVAL)
        return process.returncode or 0

    # ------------------------------------------------------------- loop

    def run(self) -> int:
        self._install_handlers()
        rapid = 0
        code = 0
        try:
            while True:
                started = time.monotonic()
                code = self._run_once()
                lasted = time.monotonic() - started
                self._say(f"server exited with code {code} after {lasted:.0f}s")

                if self._shutdown.is_set():
                    self._say("stop was requested -- not restarting.")
                    break

                rapid = rapid + 1 if lasted < MIN_HEALTHY_SECONDS else 0
                if rapid >= self.max_rapid_restarts:
                    self._say(
                        f"[ERROR] {rapid} crashes in under {MIN_HEALTHY_SECONDS:.0f}s each -- "
                        "giving up rather than restart-looping. Check logs/latest.log."
                    )
                    break

                self._restarts += 1
                self._say(f"restarting in {self.restart_delay:.0f}s (Ctrl-C to stay stopped)")
                if self._shutdown.wait(self.restart_delay):
                    self._say("stop was requested -- not restarting.")
                    break
        finally:
            self._clear_state()
        return code
