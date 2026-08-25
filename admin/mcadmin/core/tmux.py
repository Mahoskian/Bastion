"""A typed wrapper around the one tmux session the server runs in."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from typing import NoReturn

from pydantic import BaseModel, ConfigDict

from .models import SESSION_NAME

WINDOW = "server"


class TmuxError(RuntimeError):
    pass


class TmuxSession(BaseModel):
    """One named tmux session. Methods are no-ops when it does not exist."""

    model_config = ConfigDict(frozen=True)

    name: str = SESSION_NAME

    @staticmethod
    def available() -> bool:
        return shutil.which("tmux") is not None

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", *args], capture_output=True, text=True)

    def exists(self) -> bool:
        return self.available() and self._run("has-session", "-t", self.name).returncode == 0

    def create(self, command: Sequence[str]) -> None:
        """Start `command` detached. Raises if the session already exists."""
        if not self.available():
            raise TmuxError("tmux is not installed")
        if self.exists():
            raise TmuxError(f"a tmux session named {self.name!r} already exists")
        result = self._run("new-session", "-d", "-s", self.name, "-n", WINDOW, *command)
        if result.returncode != 0:
            raise TmuxError(result.stderr.strip() or "tmux refused to create the session")

    def kill(self) -> bool:
        """True if a session was actually killed."""
        if not self.exists():
            return False
        return self._run("kill-session", "-t", self.name).returncode == 0

    def send_keys(self, text: str) -> bool:
        """Type into the console -- the fallback when RCON is unavailable."""
        if not self.exists():
            return False
        return self._run("send-keys", "-t", f"{self.name}:{WINDOW}", text, "Enter").returncode == 0

    def attach(self) -> NoReturn:
        """Hand the terminal to tmux; this call does not return."""
        os.execvp("tmux", ["tmux", "attach-session", "-t", self.name])
