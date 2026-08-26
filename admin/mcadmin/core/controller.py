"""The server lifecycle, as one object.

`stop` and `restart` differ in what they signal, and that is the whole design:

  stop     -> signal the supervisor. It shuts the JVM down and exits, so the
              tmux session ends with it. Server and session both gone.
  restart  -> ask the JVM to stop and leave the supervisor alone. Its restart
              loop brings the server straight back up in the same session.
"""

from __future__ import annotations

import os
import re
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import process
from .models import JvmOptions, Paths, RuntimeState, ServerState
from .rcon import RconError, connect
from .tmux import TmuxError, TmuxSession

# Modded startup is slow: ~90s for this mod set, with headroom for a cold cache.
START_TIMEOUT = 300.0
STOP_TIMEOUT = 300.0
# The supervisor's own exit is quick once the JVM is down.
SUPERVISOR_EXIT_TIMEOUT = 30.0
RESTART_TIMEOUT = 420.0


class LifecycleError(RuntimeError):
    """Something the user needs to resolve before the action can proceed."""


# "There are 2 of a max of 12 players online: Alice, Bob"
LIST_RE = re.compile(
    r"there are (?P<online>\d+) of a max of (?P<maximum>\d+) players online:?(?P<names>.*)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Online:
    """Who is on, parsed out of RCON's `list` sentence."""

    online: int
    maximum: int
    names: tuple[str, ...] = ()

    @classmethod
    def parse(cls, reply: str | None) -> Online | None:
        """None when the reply is not the sentence this knows how to read.

        Mods do rewrite this line, and a caller that cannot parse it still has
        the raw text to show -- so failing to parse must stay distinguishable
        from an empty server, which is what returning None keeps true.
        """
        if not reply:
            return None
        match = LIST_RE.search(reply.strip())
        if match is None:
            return None
        listed = match["names"].strip()
        names = tuple(name.strip() for name in listed.split(",") if name.strip())
        return cls(online=int(match["online"]), maximum=int(match["maximum"]), names=names)


@dataclass(frozen=True)
class Status:
    """A snapshot of everything `mc status` wants to show."""

    state: ServerState
    pid: int | None
    session_exists: bool
    runtime: RuntimeState | None
    players: str | None

    @property
    def supervised(self) -> bool:
        return self.runtime is not None and process.alive(self.runtime.supervisor_pid)


class ServerController:
    """Start, stop, restart and inspect the server."""

    def __init__(
        self,
        paths: Paths | None = None,
        session: TmuxSession | None = None,
    ) -> None:
        self.paths = paths or Paths.from_env()
        self.session = session or TmuxSession()

    # ------------------------------------------------------------- state

    def runtime(self) -> RuntimeState | None:
        """What the supervisor published, ignoring a stale file it left behind."""
        state = RuntimeState.load(self.paths.runtime_file)
        if state is None:
            return None
        return state if process.alive(state.supervisor_pid) else None

    def rcon_ready(self) -> bool:
        try:
            with connect(timeout=3.0) as client:
                return "players" in client.command("list").lower()
        except (RconError, OSError):
            return False

    def state(self) -> ServerState:
        if process.server_running():
            return ServerState.RUNNING if self.rcon_ready() else ServerState.BOOTING
        return ServerState.ORPHANED if self.session.exists() else ServerState.STOPPED

    def players(self) -> str | None:
        try:
            with connect(timeout=3.0) as client:
                return client.command("list") or None
        except (RconError, OSError):
            return None

    def status(self) -> Status:
        state = self.state()
        return Status(
            state=state,
            pid=process.server_pid(),
            session_exists=self.session.exists(),
            runtime=self.runtime(),
            players=self.players() if state is ServerState.RUNNING else None,
        )

    # ------------------------------------------------------------- quiesce

    @contextmanager
    def quiesced(self, log: Callable[[str], None] = lambda _message: None) -> Iterator[bool]:
        """Pause world saving for the duration, and always turn it back on.

        Yields True only when saving really was paused: with the server live,
        an archive taken without this catches half-written region files.
        """
        if not process.server_running():
            log("Server is stopped -- clean cold copy.")
            yield False
            return
        log("Server is RUNNING -- quiescing world via RCON.")
        try:
            with connect(timeout=10.0) as client:
                client.command("save-off")
                client.command("save-all flush")
        except (RconError, OSError) as exc:
            log(f"WARNING: RCON unreachable ({exc}) -- unquiesced hot copy.")
            yield False
            return
        log("save-off + save-all flush OK -- consistent hot copy.")
        try:
            yield True
        finally:
            try:
                with connect(timeout=10.0) as client:
                    client.command("save-on")
                log("save-on restored.")
            except (RconError, OSError):
                log("WARNING: could not re-enable saving -- run 'save-on' manually!")

    # ------------------------------------------------------------- start

    def supervisor_command(self, jvm: JvmOptions) -> list[str]:
        """The command tmux runs: this CLI, in supervise mode."""
        entry = Path(sys.executable).with_name("mc")
        binary = str(entry) if entry.exists() else sys.argv[0]
        return [binary, "supervise", "--heap", jvm.heap]

    def start(self, jvm: JvmOptions | None = None) -> None:
        """Launch the supervisor detached. Refuses if anything is already up."""
        jvm = jvm or JvmOptions()
        if not TmuxSession.available():
            raise LifecycleError("tmux is not installed -- `mc supervise` runs it in-terminal.")
        if process.server_running():
            raise LifecycleError("the server is already running. Use 'mc restart' to bounce it.")
        if self.runtime() is not None:
            raise LifecycleError("a supervisor is already running. Use 'mc restart'.")
        if self.session.exists():
            raise LifecycleError(
                f"a tmux session named {self.session.name!r} exists but holds no server. "
                f"Inspect it with 'mc console', or clear it with 'mc stop --force'."
            )
        # A supervisor that died hard leaves this behind; start would misread it.
        self.paths.runtime_file.unlink(missing_ok=True)
        try:
            self.session.create(self.supervisor_command(jvm))
        except TmuxError as exc:
            raise LifecycleError(str(exc)) from exc

    def wait_until_ready(self, timeout: float = START_TIMEOUT) -> bool:
        return process.wait_until(self.rcon_ready, timeout, interval=2.0)

    # ------------------------------------------------------------- stop

    def _ask_jvm_to_stop(self) -> None:
        """RCON first; typing into the console covers a server still booting."""
        try:
            with connect(timeout=5.0) as client:
                client.command("stop")
                return
        except (RconError, OSError):
            pass
        if not self.session.send_keys("stop"):
            raise LifecycleError(
                "RCON is unreachable and there is no console to type into. "
                "Attach with 'mc console' and stop it by hand."
            )

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Stop the server and take the tmux session down with it."""
        runtime = self.runtime()
        if runtime is not None:
            # The supervisor owns the JVM: signalling it is what distinguishes
            # a stop from a restart, so never bypass it when it is alive.
            os.kill(runtime.supervisor_pid, signal.SIGTERM)
        elif process.server_running():
            self._ask_jvm_to_stop()

        if not process.wait_until(lambda: not process.server_running(), timeout):
            raise LifecycleError(
                f"the server is still running after {timeout:.0f}s -- it may be saving a large "
                "world. Watch it with 'mc console'; do not kill a saving server."
            )
        if runtime is not None:
            process.wait_until(
                lambda: not process.alive(runtime.supervisor_pid), SUPERVISOR_EXIT_TIMEOUT
            )
        # The session normally ends with the supervisor; make sure of it.
        self.session.kill()
        self.paths.runtime_file.unlink(missing_ok=True)

    def force_stop(self) -> bool:
        """Clear a leftover session when no server is running."""
        self.paths.runtime_file.unlink(missing_ok=True)
        return self.session.kill()

    # ------------------------------------------------------------- restart

    def restart(self, timeout: float = RESTART_TIMEOUT) -> None:
        """Bounce the JVM inside the existing session.

        The supervisor is untouched, so its restart loop -- not this code --
        brings the server back, and the session survives.
        """
        if self.runtime() is None:
            raise LifecycleError(
                "no supervisor is running, so there is nothing to restart into. "
                "Use 'mc start'."
            )
        if not process.server_running():
            raise LifecycleError("the server is not running. Use 'mc start'.")

        old_pid = process.server_pid()
        self._ask_jvm_to_stop()
        if not process.wait_until(lambda: process.server_pid() != old_pid, timeout, interval=2.0):
            raise LifecycleError(
                f"the server did not come back within {timeout:.0f}s -- check 'mc console'."
            )
