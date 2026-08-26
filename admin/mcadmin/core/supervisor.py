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

from .board import Board, Phase, Pinboard, pinboard_for, restart_at
from .controller import Online
from .models import JvmOptions, Paths, RuntimeState
from .notify import Notice, Notifier, notifier_for
from .rcon import RconError, connect

POLL_INTERVAL = 0.5
# Saving a multi-gigabyte world is not quick; only escalate after real patience.
GRACEFUL_STOP_TIMEOUT = 180.0
SIGTERM_GRACE = 60.0
RESTART_DELAY = 10.0
# A JVM that dies faster than this never really came up.
MIN_HEALTHY_SECONDS = 60.0
MAX_RAPID_RESTARTS = 3
# How long to keep asking a booting server whether it is up yet. Modded boots
# run to ~90s here; the ceiling is only there so the watcher cannot outlive a
# server that came up broken and never opened its RCON port.
READY_TIMEOUT = 600.0
READY_POLL = 5.0


class Supervisor:
    """Runs one server, restarting it until told to stop."""

    def __init__(
        self,
        paths: Paths,
        jvm: JvmOptions,
        restart_delay: float = RESTART_DELAY,
        max_rapid_restarts: int = MAX_RAPID_RESTARTS,
        notifier: Notifier | None = None,
        pinboard: Pinboard | None = None,
    ) -> None:
        self.paths = paths
        self.jvm = jvm
        self.restart_delay = restart_delay
        self.max_rapid_restarts = max_rapid_restarts
        self.notifier = notifier or notifier_for(paths, self._say)
        self.pinboard = pinboard or pinboard_for(paths, self._say)
        self._shutdown = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._restarts = 0
        self._started_at = datetime.now()

    # ------------------------------------------------------------- output

    def _say(self, message: str) -> None:
        print(f"[mc] {datetime.now():%H:%M:%S} {message}", flush=True)

    # ------------------------------------------------------------- announcing

    def _notify(self, notice: Notice) -> None:
        """Announce something, and never let announcing it matter.

        A Discord outage, an expired token or a slow network must not delay a
        restart or take the supervisor down with it, so every failure here is
        logged and dropped -- including ones a notifier is not supposed to
        raise in the first place.

        Reached only for a crash or a give-up. Everything else is `_show`,
        which changes the pinned board without notifying anyone.
        """
        try:
            self.notifier.send(notice)
        except Exception as exc:  # noqa: BLE001 -- deliberate; see the docstring
            self._say(f"could not send the {notice.kind} notification: {exc}")

    def _show(self, phase: Phase, **fields: object) -> None:
        """Move the pinned status board to a phase, swallowing any failure.

        Heap and restart count are filled in here rather than at each call
        site: they are true of the supervisor, not of the transition, and a
        board that dropped them halfway through a run would look like the
        server had changed shape.
        """
        # The transition is happening now unless a caller knows better -- the
        # running board dates from the launch, not from the boot finishing.
        fields.setdefault("since", datetime.now())
        board = Board(
            phase=phase,
            heap=self.jvm.heap,
            restarts=self._restarts,
            **fields,  # type: ignore[arg-type]
        )
        try:
            self.pinboard.show(board)
        except Exception as exc:  # noqa: BLE001 -- deliberate; as above
            self._say(f"could not update the status board to {phase}: {exc}")

    def _list(self) -> str | None:
        """RCON's answer to `list`, or None while the server cannot give one.

        Answering this at all is what "ready" means here, and the answer also
        happens to name everyone online -- so the board's player count costs
        nothing beyond the round trip the readiness check was making anyway.
        """
        try:
            with connect(timeout=3.0) as client:
                return client.command("list")
        except (RconError, OSError):
            return None

    def _watch_for_ready(self, process: subprocess.Popen[bytes]) -> None:
        """Move the board to Running once RCON answers, from a side thread.

        The JVM being alive is not the same as the server being playable --
        this mod set spends over a minute between the two -- and "playable" is
        what anyone waiting to join is reading the board for. It runs beside
        the loop rather than in it because the loop's job is watching the
        process, and blocking it here would delay noticing a crash during boot.
        """

        def watch() -> None:
            deadline = time.monotonic() + READY_TIMEOUT
            while time.monotonic() < deadline:
                if self._shutdown.is_set() or process.poll() is not None:
                    return
                reply = self._list()
                if reply is not None and "players" in reply.lower():
                    # `since` is the launch, not this moment: what a reader
                    # wants from "up since" is how long the server has been
                    # there, not how long ago it finished booting.
                    self._show(
                        Phase.RUNNING, since=self._started_at, players=Online.parse(reply)
                    )
                    return
                time.sleep(READY_POLL)

        threading.Thread(target=watch, name="ready-watcher", daemon=True).start()

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
        self._started_at = datetime.now()
        self._publish(process.pid)
        self._show(Phase.BOOTING, since=self._started_at)
        self._watch_for_ready(process)
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
                    self._show(Phase.STOPPED)
                    break

                rapid = rapid + 1 if lasted < MIN_HEALTHY_SECONDS else 0
                if rapid >= self.max_rapid_restarts:
                    self._say(
                        f"[ERROR] {rapid} crashes in under {MIN_HEALTHY_SECONDS:.0f}s each -- "
                        "giving up rather than restart-looping. Check logs/latest.log."
                    )
                    self._show(
                        Phase.ABANDONED,
                        detail=f"{rapid} crashes in a row. Not restarting again.",
                    )
                    self._notify(Notice.abandoned(rapid, MIN_HEALTHY_SECONDS))
                    break

                # Exit code is the only thing separating the two exits that
                # reach here. `mc restart` stops the JVM and leaves this loop
                # to bring it back, so an intentional restart looks exactly
                # like an unattended one -- except that it exited 0. That case
                # moves the board and stays silent; a crash also posts, because
                # a pinned message turning red is not something anyone sees at
                # 3am.
                back = restart_at(self.restart_delay)
                if code == 0:
                    self._show(Phase.RESTARTING, resumes_at=back)
                else:
                    self._show(
                        Phase.CRASHED,
                        resumes_at=back,
                        detail=f"Exit code {code}.",
                    )
                    self._notify(Notice.crashed(code, lasted, self.restart_delay))
                self._restarts += 1
                self._say(f"restarting in {self.restart_delay:.0f}s (Ctrl-C to stay stopped)")
                if self._shutdown.wait(self.restart_delay):
                    self._say("stop was requested -- not restarting.")
                    self._show(Phase.STOPPED)
                    break
        finally:
            self._clear_state()
        return code
