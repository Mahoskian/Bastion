"""Finding and waiting on OS processes."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

# The jar name changes with every loader bump, so match the stable prefix.
JAR_MARKER = "fabric-server"


def java_processes() -> dict[int, str]:
    """pid -> command line, for processes actually named `java`.

    Deliberately narrowed to `-C java`: matching a command-line pattern across
    all processes also matches the shell running the check.
    """
    try:
        result = subprocess.run(
            ["ps", "-C", "java", "-o", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    found: dict[int, str] = {}
    for line in result.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            found[int(pid)] = args.strip()
    return found


def server_pid(marker: str = JAR_MARKER) -> int | None:
    """The running server's pid, if there is one."""
    return next((pid for pid, args in java_processes().items() if marker in args), None)


def server_running(marker: str = JAR_MARKER) -> bool:
    return server_pid(marker) is not None


def alive(pid: int) -> bool:
    """True if the pid exists and we may signal it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until(
    predicate: Callable[[], bool], timeout: float, interval: float = 1.0
) -> bool:
    """Poll until true or the deadline passes; always checks at least twice."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
