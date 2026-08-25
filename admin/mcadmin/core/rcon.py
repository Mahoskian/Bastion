"""Minecraft RCON client (Source RCON protocol)."""

from __future__ import annotations

import itertools
import socket
import struct
from collections.abc import Iterator
from contextlib import contextmanager

from .properties import properties

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2


class RconError(RuntimeError):
    pass


class RconAuthError(RconError):
    pass


class Rcon:
    """A single RCON session. Use as a context manager."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 25575, password: str = "", timeout: float = 10.0
    ) -> None:
        self._ids = itertools.count(1)
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except ConnectionRefusedError as exc:
            raise RconError(
                f"Connection refused on {host}:{port} -- is the server running "
                "and enable-rcon=true?"
            ) from exc
        if self._send(SERVERDATA_AUTH, password) is None:
            self.close()
            raise RconAuthError("RCON authentication failed (wrong password?)")

    def _recv_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("Connection closed by server")
            buf += chunk
        return buf

    def _send(self, packet_type: int, body: str) -> str | None:
        request_id = next(self._ids)
        payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)

        (length,) = struct.unpack("<i", self._recv_exactly(4))
        data = self._recv_exactly(length)
        resp_id, _ = struct.unpack("<ii", data[:8])
        if resp_id == -1:  # protocol's auth-failure sentinel
            return None
        return data[8:-2].decode("utf8", errors="replace")

    def command(self, cmd: str) -> str:
        result = self._send(SERVERDATA_EXECCOMMAND, cmd)
        return (result or "").strip()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> Rcon:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def connect(timeout: float = 10.0) -> Iterator[Rcon]:
    """Open an RCON session using credentials from server.properties."""
    props = properties()
    if not props.rcon_enabled:
        raise RconError("enable-rcon is not true in server.properties")
    if not props.rcon_password:
        raise RconError("rcon.password is empty in server.properties")
    client = Rcon(
        host="127.0.0.1",
        port=props.rcon_port,
        password=props.rcon_password,
        timeout=timeout,
    )
    try:
        yield client
    finally:
        client.close()
