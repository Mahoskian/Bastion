"""Discord notification commands."""

from __future__ import annotations

import json
import os
import stat

import typer

from ..core.board import Board, DiscordPinboard, PinState, version_label
from ..core.controller import Online, ServerController, Tick
from ..core.models import Paths
from ..core.notify import ENV_CHANNEL, ENV_TOKEN, DiscordBot, DiscordConfig, Notice, NotifyError
from ..ui import notify as view
from ..ui.console import console, fail

app = typer.Typer(help="Broadcast server lifecycle events to a Discord channel.")


def _config(paths: Paths) -> DiscordConfig | None:
    try:
        return DiscordConfig.load(paths.notify_config)
    except NotifyError as exc:
        fail(str(exc))


def _source(paths: Paths) -> str:
    """Where the settings in play actually came from."""
    overrides = [name for name in (ENV_TOKEN, ENV_CHANNEL) if os.environ.get(name)]
    if overrides and paths.notify_config.exists():
        return f"{paths.notify_config} (overridden by {', '.join(overrides)})"
    if overrides:
        return ", ".join(overrides)
    return str(paths.notify_config)


@app.command()
def status() -> None:
    """Show whether Discord notifications are configured, and from where."""
    paths = Paths.from_env()
    view.show_config(_config(paths), _source(paths), PinState.load(paths.board_file))


@app.command()
def test() -> None:
    """Check the token and post a test message to the channel."""
    paths = Paths.from_env()
    config = _config(paths)
    if config is None:
        fail("Discord is not configured yet -- run 'mc notify setup'.")

    bot = DiscordBot(config)
    try:
        with console.status("Asking Discord who this bot is..."):
            name = bot.identify()
        console.print(f"Authenticated as [bold]{name}[/].")
        with console.status(f"Posting to channel {config.channel_id}..."):
            bot.send(Notice.test())
    except NotifyError as exc:
        fail(_diagnose(bot, exc))
    console.print("[bold green]Sent[/] -- check the channel.")
    if not config.enabled:
        console.print(
            "[yellow]Note:[/] enabled is false, so the supervisor will stay quiet."
        )


def _diagnose(bot: DiscordBot, exc: NotifyError) -> str:
    """Sharpen a 403 by asking which servers the bot is actually in.

    An uninvited bot cannot reach any channel, which produces the same 403 as
    a missing permission and sends you reading channel settings for an hour.
    The question is only asked once a 403 has already happened: Discord rate-
    limits it hard enough that putting it on the happy path breaks the test.
    """
    if exc.status != 403:
        return str(exc)
    try:
        servers = bot.guilds()
    except NotifyError:
        return str(exc)  # the follow-up failed too; the original stands
    if not servers:
        return (
            "this bot has not been added to any server yet, so no channel is "
            "reachable.\n        Invite it with scope 'bot' and permissions View "
            "Channel + Send Messages\n        + Embed Links, from OAuth2 -> URL "
            "Generator in the developer portal."
        )
    listed = ", ".join(servers)
    return (
        f"{exc}\n        The bot IS in: {listed}. If the channel is in one of those, "
        "this is a\n        permission overwrite on the channel or its category."
    )


@app.command()
def board(
    reset: bool = typer.Option(False, "--reset", help="Post a new message instead of editing."),
) -> None:
    """Write the pinned status message from what the server looks like now.

    The supervisor keeps this current on its own. This is for the times it
    could not: it was killed outright and never got to write "stopped", or the
    message was deleted, or Discord was unreachable at the one moment that
    mattered. It reads the live server rather than any stored phase, so it can
    only say what an observer can see -- a crash is the supervisor's to report.
    """
    paths = Paths.from_env()
    config = _config(paths)
    if config is None:
        fail("Discord is not configured yet -- run 'mc notify setup'.")
    if reset:
        # Forget the id and the next show posts a fresh message. The old one is
        # left where it is: deleting somebody's channel history on a --reset is
        # a bigger promise than this flag is making.
        paths.board_file.unlink(missing_ok=True)

    status = ServerController().status()
    pinboard = DiscordPinboard(DiscordBot(config), paths.board_file, warn=_warn)
    try:
        with console.status("Updating the pinned status message..."):
            pinboard.show(
                Board.observed(
                    status,
                    Online.parse(status.players),
                    Tick.parse(status.tick),
                    version_label(paths),
                )
            )
    except NotifyError as exc:
        fail(str(exc))

    state = PinState.load(paths.board_file)
    console.print(f"[bold green]Board[/] shows [bold]{status.state.description}[/].")
    if state is not None:
        console.print(f"[dim]Message {state.message_id} in channel {state.channel_id}[/]")


def _warn(message: str) -> None:
    console.print(f"[yellow]Note:[/] {message}")


@app.command()
def setup(
    channel: str = typer.Option(None, "--channel", help="Channel id to post in."),
    token: str = typer.Option(None, "--token", help="Bot token; prompted for if omitted."),
) -> None:
    """Write the bot token and channel id, readable only by you."""
    paths = Paths.from_env()
    channel = channel or typer.prompt("Channel id").strip()
    # Prompted rather than passed by default: a token on the command line ends
    # up in shell history, and this one is a password for a Discord identity.
    token = token or typer.prompt("Bot token", hide_input=True).strip()

    try:
        config = DiscordConfig(token=token, channel_id=channel)
    except ValueError as exc:
        fail(f"that is not a usable configuration: {exc}")

    target = paths.notify_config
    if target.exists() and not typer.confirm(f"{target} exists. Overwrite?"):
        console.print("Left alone.")
        raise typer.Exit(1)

    # Created empty at 0600 first: writing then chmod'ing leaves the token
    # world-readable for however long the two calls are apart.
    target.touch(mode=stat.S_IRUSR | stat.S_IWUSR, exist_ok=True)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    target.write_text(
        json.dumps(
            {"token": config.token, "channel_id": config.channel_id, "enabled": True},
            indent=2,
        )
        + "\n"
    )
    console.print(f"[bold green]Wrote[/] {target} [dim](mode 600)[/]")
    console.print("[dim]Check it end to end with: mc notify test[/]")
