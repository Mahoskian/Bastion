"""The Discord listener: slash commands, over the gateway.

`mc listen start` runs it detached in its own tmux session, the same shape as
`mc start` and for the same reason -- a daemon you have to keep a terminal open
for is a daemon that dies when you close the laptop. `mc listen run` is the
foreground process that session actually runs.

The listener is deliberately independent of the server's lifecycle. It is not
started by `mc start` and not stopped by `mc stop`, because a server that is
down is exactly when you most want to ask a question about it -- coupling the
two would put the bot offline at the only moment it really matters.

The transport is the gateway rather than an interactions endpoint URL: the
gateway is an outbound websocket, so nothing has to be reachable from the
internet and no port on the box running the world is exposed.

Each handler follows the same shape as a CLI command -- parse, call `core`,
hand the result to `ui` -- with two differences Discord imposes:

  * `core` is synchronous and some of it is slow (parsing every log file for
    `/deaths`). It is called through `asyncio.to_thread` so a slow answer
    cannot stall the heartbeat and drop the connection.
  * Discord wants an acknowledgement within three seconds. Every command
    defers first and edits the answer in afterwards, because a cold `/wrapped`
    is comfortably slower than that.
"""

from __future__ import annotations

import asyncio
import os

import typer

from ..core import deaths as dt
from ..core import logs as lg
from ..core import stats as st
from ..core.controller import LifecycleError, Online, ServerController
from ..core.listener import ListenerController, ListenerState
from ..core.models import Paths
from ..core.notify import DiscordConfig, NotifyError
from ..core.properties import properties
from ..ui import discord as view
from ..ui import listener as listener_view
from ..ui.console import console, fail

app = typer.Typer(help="Answer slash commands in Discord.")

# Read-only, and open to everyone -- so nothing here checks a permission. Any
# command that changes the server's state must not be added without one.
PUBLIC_COMMANDS = ("status", "players", "wrapped", "deaths")


def controller() -> ListenerController:
    return ListenerController()


def _config() -> DiscordConfig:
    try:
        config = DiscordConfig.load(Paths.from_env().notify_config)
    except NotifyError as exc:
        fail(str(exc))
    if config is None:
        fail("Discord is not configured yet -- run 'mc notify setup'.")
    return config


# ------------------------------------------------------------------ answers


def _status_embed() -> dict[str, object]:
    control = ServerController()
    status = control.status()
    return view.server_status(status, properties(), Online.parse(status.players))


def _players_embed() -> dict[str, object]:
    raw = ServerController().players()
    return view.players(Online.parse(raw), raw)


def _wrapped_embed(player: str | None) -> dict[str, object]:
    roster = st.load()
    if not player:
        return view.leaderboards(roster, st.boards(roster))
    found = roster.find(player)
    if found is None:
        return view.unknown_player(player, [p.name for p in roster.players])
    return view.player_card(found, st.titles(roster, found), roster.updated)


def _deaths_embed() -> dict[str, object]:
    paths = Paths.from_env()
    files = lg.log_files(paths.logs_dir)
    found = dt.collect(list(lg.parse(files))) if files else dt.DeathMap()
    return view.death_map(found, dt.hotspots(found.located))


# ------------------------------------------------------------------ managing


@app.command()
def start() -> None:
    """Start the listener in a detached tmux session."""
    _config()  # fail here rather than inside a session nobody is watching
    control = controller()
    try:
        control.start()
    except LifecycleError as exc:
        fail(str(exc))
    console.print(f"[green]Listening[/] in tmux session [bold]{control.session.name}[/]")
    console.print("[dim]Check it with: mc listen status[/]")


@app.command()
def stop(
    force: bool = typer.Option(False, "--force", help="Clear a leftover session when stopped."),
) -> None:
    """Stop the listener and close its tmux session."""
    control = controller()
    if not control.running():
        console.print("The listener is not running.")
        if control.session.exists():
            if force:
                control.force_stop()
                console.print("Cleared the leftover tmux session.")
            else:
                console.print("[yellow]Its tmux session is still open.[/]")
                console.print("[dim]Clear it with: mc listen stop --force[/]")
        return
    try:
        control.stop()
    except LifecycleError as exc:
        fail(str(exc))
    console.print("[bold green]Stopped[/] and the tmux session is closed.")


@app.command()
def status() -> None:
    """Show whether the listener is running, and what it is connected to."""
    control = controller()
    listener_view.show_status(control.state(), control.session, PUBLIC_COMMANDS)


@app.command("console")
def attach() -> None:
    """Attach to the listener's console (detach with Ctrl-B then D)."""
    control = controller()
    if not control.session.exists():
        fail(f"no tmux session named {control.session.name!r}.")
    console.print("[dim]Attaching -- detach with Ctrl-B then D.[/]")
    control.session.attach()


# ------------------------------------------------------------------ the daemon


@app.command(hidden=True)
def run() -> None:
    """Run the listener in the foreground.

    This is what `mc listen start` runs inside tmux. Run it directly to hold
    the gateway open in the current terminal instead.
    """
    config = _config()
    paths = Paths.from_env()

    # Imported here rather than at module scope: discord.py is slow to import,
    # and every other `mc` command would otherwise pay for it.
    import discord

    # No intents are requested. Slash commands arrive as interactions, which
    # need none -- in particular this never reads message content, so the
    # privileged intent that would demand stays off.
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    state = ListenerState(pid=os.getpid())
    state.save(paths.listener_file)

    async def respond(interaction: discord.Interaction, builder, label: str) -> None:
        """Defer, do the slow synchronous work off the loop, then edit it in."""
        await interaction.response.defer()
        try:
            embed = await asyncio.to_thread(builder)
        except Exception as exc:  # noqa: BLE001 -- one bad command must not kill the bot
            console.print(f"[red]/{label} failed:[/] {exc}")
            await interaction.followup.send(
                f"`/{label}` failed on the server side: `{exc}`", ephemeral=True
            )
            return
        await interaction.followup.send(embed=discord.Embed.from_dict(embed))

    @tree.command(name="status", description="Server state, players and key settings.")
    async def status_command(interaction: discord.Interaction) -> None:
        await respond(interaction, _status_embed, "status")

    @tree.command(name="players", description="Who is online right now.")
    async def players_command(interaction: discord.Interaction) -> None:
        await respond(interaction, _players_embed, "players")

    @tree.command(name="wrapped", description="Leaderboards, or one player's card.")
    @discord.app_commands.describe(player="Omit for the leaderboards.")
    async def wrapped_command(
        interaction: discord.Interaction, player: str | None = None
    ) -> None:
        await respond(interaction, lambda: _wrapped_embed(player), "wrapped")

    @tree.command(name="deaths", description="Where players die, and the spots that keep killing.")
    async def deaths_command(interaction: discord.Interaction) -> None:
        await respond(interaction, _deaths_embed, "deaths")

    @client.event
    async def on_ready() -> None:
        # Guild-scoped commands appear immediately; global ones can take an
        # hour to propagate, which makes them useless to iterate on.
        for guild in client.guilds:
            target = discord.Object(id=guild.id)
            tree.copy_global_to(guild=target)
            await tree.sync(guild=target)
        names = [guild.name for guild in client.guilds]
        # Republished so `mc listen status` can tell "started" from "connected".
        ListenerState(
            pid=state.pid,
            started_at=state.started_at,
            bot=str(client.user),
            guilds=names,
        ).save(paths.listener_file)
        console.print(
            f"[bold green]Listening[/] as [bold]{client.user}[/] in "
            f"{', '.join(names) or 'nowhere'}.\n"
            f"[dim]Commands: {', '.join('/' + name for name in PUBLIC_COMMANDS)}[/]"
        )

    try:
        client.run(config.token, log_handler=None)
    except discord.LoginFailure as exc:
        fail(f"Discord rejected the bot token: {exc}")
    finally:
        paths.listener_file.unlink(missing_ok=True)
