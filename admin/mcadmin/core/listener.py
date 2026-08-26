"""The Discord listener as a managed process, not a terminal you must keep open.

This mirrors `controller.py` deliberately: a detached tmux session, a state file
the running process publishes, and start/stop that read the real world rather
than trusting the file. The server already solved this problem once, and a
second mechanism for the same job would be one more thing to reason about.

What it does *not* mirror is the supervisor's restart loop. discord.py
reconnects and resumes on its own, so the failure that loop would catch -- the
gateway dropping -- is already handled a layer down. What neither survives is
the box rebooting, which is a job for cron's `@reboot` or a systemd unit.

The listener is kept independent of the server's lifecycle on purpose. Tying it
to `mc start`/`mc stop` would mean the bot is offline exactly when the server
is, and a server that is down is when you most want to ask a question about it.
"""

from __future__ import annotations

import os
import signal
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from . import process
from .controller import LifecycleError
from .models import LISTENER_SESSION, Paths
from .tmux import TmuxError, TmuxSession

STOP_TIMEOUT = 30.0


class ListenerState(BaseModel):
    """What the running listener publishes about itself, for the CLI to read.

    `bot` and `guilds` are empty until the gateway connects, which is what
    makes "started but not connected" a state `mc listen status` can show
    rather than one it has to guess at.
    """

    pid: int
    started_at: datetime = Field(default_factory=datetime.now)
    bot: str | None = None
    guilds: list[str] = []

    @classmethod
    def load(cls, path: Path) -> ListenerState | None:
        """None when absent or unreadable -- a stale file must never be fatal."""
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return None

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    @property
    def connected(self) -> bool:
        return self.bot is not None


class ListenerController:
    """Start, stop and inspect the Discord listener."""

    def __init__(self, paths: Paths | None = None, session: TmuxSession | None = None) -> None:
        self.paths = paths or Paths.from_env()
        self.session = session or TmuxSession(name=LISTENER_SESSION)

    # ------------------------------------------------------------- state

    def state(self) -> ListenerState | None:
        """What the listener published, ignoring a stale file it left behind."""
        state = ListenerState.load(self.paths.listener_file)
        if state is None:
            return None
        return state if process.alive(state.pid) else None

    def running(self) -> bool:
        return self.state() is not None

    def command(self) -> list[str]:
        """The command tmux runs: this CLI, in the foreground listening mode."""
        import sys

        entry = Path(sys.executable).with_name("mc")
        binary = str(entry) if entry.exists() else sys.argv[0]
        return [binary, "listen", "run"]

    # ------------------------------------------------------------- start

    def start(self) -> None:
        """Launch the listener detached. Refuses if one is already up."""
        if not TmuxSession.available():
            raise LifecycleError("tmux is not installed -- `mc listen run` runs it in-terminal.")
        if self.running():
            raise LifecycleError("the listener is already running. Use 'mc listen stop' first.")
        if self.session.exists():
            raise LifecycleError(
                f"a tmux session named {self.session.name!r} exists but holds no listener. "
                f"Inspect it with 'mc listen console', or clear it with 'mc listen stop --force'."
            )
        # A listener that died hard leaves this behind; start would misread it.
        self.paths.listener_file.unlink(missing_ok=True)
        try:
            self.session.create(self.command())
        except TmuxError as exc:
            raise LifecycleError(str(exc)) from exc

    # ------------------------------------------------------------- stop

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Ask the listener to exit, then make sure its session went with it."""
        state = self.state()
        if state is not None:
            os.kill(state.pid, signal.SIGTERM)
            if not process.wait_until(lambda: not process.alive(state.pid), timeout):
                raise LifecycleError(
                    f"the listener is still running after {timeout:.0f}s -- "
                    "inspect it with 'mc listen console'."
                )
        self.session.kill()
        self.paths.listener_file.unlink(missing_ok=True)

    def force_stop(self) -> bool:
        """Clear a leftover session when no listener is running."""
        self.paths.listener_file.unlink(missing_ok=True)
        return self.session.kill()
