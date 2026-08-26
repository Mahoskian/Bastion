"""One pinned message that says what the server is doing, edited in place.

Every lifecycle transition used to be its own post: a restart alone was two
notifications -- "restarting", then "up" -- and a channel nobody had muted
became a feed of them. But the state of the server is not news. It is a fact
that changes, and a fact belongs in one message that is kept current rather
than in a message per change.

What stays a post is a crash. An exit nobody asked for is the one transition
worth waking a phone for, and a pinned embed quietly turning red at 3am is not
that. `core.notify` keeps that half; this module took the rest.

Discord has no notion of a message you own and keep current, so the id of the
one this created is kept in `.board.json` beside the runtime state. That
message going missing -- deleted, or left behind in a channel the config has
since moved off -- is ordinary rather than exceptional: a 404 posts a new one
and pins that, which makes "delete it" a supported way to start the board over.

Times are written as Discord's `<t:epoch:R>` markup rather than as text. The
client renders those against the reader's own clock and keeps re-rendering them
as it moves, so "up since 4 hours ago" and "back in 10 seconds" go on being
true without this module ever touching the message again. That is what lets the
board update only on real transitions and still never look stale.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .controller import Online, Status
from .models import Paths, ServerState
from .notify import DiscordBot, DiscordConfig, NotifyError

# Discord rejects a field value over this outright, so a roster of names on a
# busy server is trimmed rather than sent and swallowed.
FIELD_LIMIT = 1024


class Phase(StrEnum):
    """What the board can say.

    Deliberately not `ServerState`, which is what an *observer* can see. From
    outside the box a crash and a clean restart look identical -- both are a
    JVM that is no longer there -- and the supervisor is the only thing in the
    system that knows which one just happened. Collapsing the two enums would
    throw away the only distinction the board exists to draw.
    """

    BOOTING = "booting"
    RUNNING = "running"
    RESTARTING = "restarting"
    STOPPED = "stopped"
    CRASHED = "crashed"
    ABANDONED = "abandoned"


class Look(NamedTuple):
    """How one phase reads: its word, its colour, its dot, its verb.

    The dot is there because a pinned message is skimmed, not read -- and
    because Discord's embed colour is a four-pixel stripe down the left edge
    that a phone in a list view does not show at all.
    """

    label: str
    colour: int
    dot: str
    verb: str


# Colours are ints rather than the "#2ecc71" they look like: Discord takes them
# that way. Each verb is chosen to read correctly in front of a relative
# timestamp -- "Up since 4 hours ago", "Crashed 30 seconds ago".
PRESENTATION: dict[Phase, Look] = {
    Phase.BOOTING: Look("Booting", 0xE5A50A, "\N{LARGE YELLOW CIRCLE}", "Started"),
    Phase.RUNNING: Look("Running", 0x2ECC71, "\N{LARGE GREEN CIRCLE}", "Up since"),
    Phase.RESTARTING: Look("Restarting", 0x3498DB, "\N{LARGE BLUE CIRCLE}", "Went down"),
    Phase.STOPPED: Look("Stopped", 0x95A5A6, "\N{MEDIUM WHITE CIRCLE}", "Stopped"),
    Phase.CRASHED: Look("Crashed", 0xE74C3C, "\N{LARGE RED CIRCLE}", "Crashed"),
    Phase.ABANDONED: Look("Gave up", 0x992D22, "\N{LARGE RED CIRCLE}", "Gave up"),
}


def stamp(when: datetime, style: str = "R") -> str:
    """A Discord timestamp: rendered by the client, in the reader's timezone.

    `R` is the relative style ("4 hours ago"), which is the whole trick -- it
    is why a message edited twice a day never reads as stale.
    """
    return f"<t:{int(when.timestamp())}:{style}>"


class Board(BaseModel):
    """The state of the server, as one message wants to show it.

    Frozen like the rest of `core`, and rendered here rather than in `ui` for
    the same reason `Notice` is: the supervisor posts this without a terminal
    anywhere near it, and `core.notify` already owns the payloads it sends.
    """

    model_config = ConfigDict(frozen=True)

    phase: Phase
    # When this phase began -- not when the message was last written. The
    # difference is the point: `since` is what the relative timestamp counts
    # from, so it must survive edits that change nothing else.
    #
    # None when nobody knows. A server that is simply not running left nothing
    # behind saying when it stopped, and defaulting that to now would render as
    # "stopped just now" on a box that has been down for a week -- a confident
    # wrong answer where no answer was available.
    since: datetime | None = None
    updated: datetime = Field(default_factory=datetime.now)
    detail: str | None = None
    heap: str | None = None
    restarts: int = 0
    # A snapshot from the moment of the transition, not a live count. The
    # footer's timestamp is what keeps that honest.
    players: Online | None = None
    resumes_at: datetime | None = None

    @property
    def look(self) -> Look:
        return PRESENTATION[self.phase]

    @classmethod
    def observed(cls, status: Status, online: Online | None = None) -> Board:
        """The board as anything outside the supervisor can see it.

        Used by `mc notify board` to repair a message that went stale -- after
        the supervisor was killed outright, say, which leaves the last thing it
        wrote standing. Only the four states an observer can distinguish are
        reachable here; a crash is not one of them.
        """
        phase = {
            ServerState.RUNNING: Phase.RUNNING,
            ServerState.BOOTING: Phase.BOOTING,
            ServerState.ORPHANED: Phase.STOPPED,
            ServerState.STOPPED: Phase.STOPPED,
        }[status.state]
        runtime = status.runtime
        return cls(
            phase=phase,
            since=runtime.started_at if runtime else None,
            heap=runtime.heap if runtime else None,
            restarts=runtime.restarts if runtime else 0,
            players=online,
            detail=(
                "The tmux session is still open with no server in it."
                if status.state is ServerState.ORPHANED
                else None
            ),
        )

    def embed(self) -> dict[str, object]:
        look = self.look
        lines = []
        if self.since is not None:
            lines.append(f"{look.verb} {stamp(self.since)}")
        if self.resumes_at is not None:
            lines.append(f"Back {stamp(self.resumes_at)}")
        if self.detail:
            lines.append(self.detail)

        fields: list[dict[str, object]] = []
        if self.players is not None:
            listed = ", ".join(self.players.names) or "nobody"
            fields.append(
                {
                    "name": f"Players ({self.players.online}/{self.players.maximum})",
                    "value": listed[:FIELD_LIMIT],
                    "inline": False,
                }
            )
        if self.heap:
            fields.append({"name": "Heap", "value": self.heap, "inline": True})
        if self.restarts:
            fields.append({"name": "Restarts", "value": str(self.restarts), "inline": True})

        embed: dict[str, object] = {
            "title": f"{look.dot} Server Status: {look.label}",
            "color": look.colour,
            "footer": {"text": f"updated {self.updated:%Y-%m-%d %H:%M}"},
        }
        if lines:
            embed["description"] = "\n".join(lines)
        if fields:
            embed["fields"] = fields
        return embed


class PinState(BaseModel):
    """Which message the board is, and where it lives.

    The channel is stored beside the id because moving the channel in
    `.notify.json` must not have this editing a message in the old one forever
    -- an edit to a channel Discord can still find succeeds perfectly well and
    is invisible to everyone who moved.
    """

    model_config = ConfigDict(frozen=True)

    channel_id: str
    message_id: str

    @classmethod
    def load(cls, path: Path) -> PinState | None:
        """None when absent or unreadable -- a lost id costs one new message."""
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return None

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))


class Pinboard(Protocol):
    """Anywhere a Board can be shown."""

    def show(self, board: Board) -> None: ...


class NullPinboard:
    """The default. An unconfigured server keeps no board, and says nothing."""

    def show(self, board: Board) -> None:
        return None


class DiscordPinboard:
    """One message in one channel, edited for the life of the server."""

    def __init__(
        self,
        bot: DiscordBot,
        state_file: Path,
        warn: Callable[[str], None] = lambda _message: None,
    ) -> None:
        self.bot = bot
        self.state_file = state_file
        self.warn = warn

    def show(self, board: Board) -> None:
        embed = board.embed()
        state = PinState.load(self.state_file)
        if state is not None and state.channel_id == self.bot.config.channel_id:
            try:
                self.bot.edit(state.message_id, embed)
                return
            except NotifyError as exc:
                # 404 is the message being gone, which is recoverable and even
                # expected. Anything else -- a 403 above all -- is a standing
                # condition: posting a replacement on every transition would
                # rebuild exactly the feed of messages this replaced.
                if exc.status != 404:
                    raise
                self.warn("the pinned status message is gone -- posting a new one.")
        self._create(embed)

    def _create(self, embed: dict[str, object]) -> None:
        message_id = self.bot.post(embed)
        if message_id is None:
            raise NotifyError("Discord did not say which message it just created.")
        PinState(channel_id=self.bot.config.channel_id, message_id=message_id).save(
            self.state_file
        )
        try:
            self.bot.pin(message_id)
        except NotifyError as exc:
            # Pinning needs Manage Messages, which sending does not. Losing the
            # pin costs the board its place at the top of the channel; it does
            # not stop it being written, so it is a warning and not a failure.
            self.warn(f"posted the status message but could not pin it -- {exc}")


def pinboard_for(
    paths: Paths | None = None,
    warn: Callable[[str], None] = lambda _message: None,
) -> Pinboard:
    """The board this server is configured for, or one that does nothing.

    Same contract as `notifier_for`: a broken config warns and degrades, since
    nothing about Discord may keep the server from coming up.
    """
    paths = paths or Paths.from_env()
    try:
        config = DiscordConfig.load(paths.notify_config)
    except NotifyError as exc:
        warn(f"the Discord status board is off -- {exc}")
        return NullPinboard()
    if config is None or not config.enabled:
        return NullPinboard()
    return DiscordPinboard(DiscordBot(config), paths.board_file, warn)


def restart_at(delay: float) -> datetime:
    """When a restart already scheduled will land, for the countdown to read."""
    return datetime.now() + timedelta(seconds=delay)
