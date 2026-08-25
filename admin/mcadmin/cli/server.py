"""Lifecycle and inspection commands."""

from __future__ import annotations

import typer

from ..core.controller import START_TIMEOUT, LifecycleError, ServerController
from ..core.models import JvmOptions, Paths, ServerState
from ..core.properties import properties
from ..core.rcon import RconError, connect
from ..core.supervisor import Supervisor
from ..ui import server as view
from ..ui.console import console, fail

app = typer.Typer()


def controller() -> ServerController:
    return ServerController()


def _jvm(heap: str | None) -> JvmOptions:
    try:
        return JvmOptions(heap=heap) if heap else JvmOptions()
    except ValueError as exc:
        fail(f"bad --heap value: {exc}")


@app.command()
def status() -> None:
    """Show server state, players, and key settings."""
    view.show_status(controller().status(), properties())


@app.command()
def start(
    heap: str = typer.Option(None, "--heap", help="Override the heap, e.g. 24G for pregen."),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Block until the server accepts RCON."
    ),
) -> None:
    """Start the server in a detached tmux session."""
    control = controller()
    jvm = _jvm(heap)
    try:
        control.start(jvm)
    except LifecycleError as exc:
        fail(str(exc))
    console.print(
        f"[green]Launched[/] in tmux session [bold]{control.session.name}[/] (heap {jvm.heap})"
    )
    if not wait:
        console.print("[dim]Watch it boot with: mc console[/]")
        return
    with console.status("Booting (this mod set takes a while)..."):
        ready = control.wait_until_ready()
    if not ready:
        fail(
            f"still not answering RCON after {START_TIMEOUT:.0f}s.\n"
            "        It may just be slow -- check with 'mc console'."
        )
    console.print("[bold green]Server is up[/] and answering RCON.")


@app.command()
def stop(
    force: bool = typer.Option(False, "--force", help="Clear a leftover session when stopped."),
) -> None:
    """Stop the server and close its tmux session."""
    control = controller()
    state = control.state()
    if not state.is_live:
        console.print("Server is not running.")
        if state is ServerState.ORPHANED:
            if force:
                control.force_stop()
                console.print("Cleared the leftover tmux session.")
            else:
                console.print("[yellow]Its tmux session is still open.[/]")
                console.print("[dim]Clear it with: mc stop --force[/]")
        return

    with console.status("Saving and shutting down..."):
        try:
            control.stop()
        except LifecycleError as exc:
            fail(str(exc))
    console.print("[bold green]Stopped[/] and the tmux session is closed.")


@app.command()
def restart(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Restart the server in place, keeping the same tmux session."""
    control = controller()
    online = control.players()
    if online and not yes and "0 of a max" not in online:
        console.print(f"[yellow]{online}[/]")
        if console.input("Restart anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            console.print("Aborted.")
            raise typer.Exit(1)
    with console.status("Restarting (the supervisor brings it back)..."):
        try:
            control.restart()
        except LifecycleError as exc:
            fail(str(exc))
    console.print("[bold green]Restarted[/] in the same session.")


@app.command("console")
def attach() -> None:
    """Attach to the server console (detach with Ctrl-B then D)."""
    control = controller()
    if not control.session.exists():
        extra = "  It IS running, just not under 'mc start'." if control.state().is_live else ""
        fail(f"no tmux session named {control.session.name!r}.{extra}")
    console.print("[dim]Attaching -- detach with Ctrl-B then D.[/]")
    control.session.attach()


@app.command()
def rcon(
    command: list[str] = typer.Argument(None, help="Command to run; omit for interactive."),
    file: typer.FileText = typer.Option(None, "--file", "-f", help="Run commands from a file."),
    delay: float = typer.Option(0.25, help="Pause between batched commands."),
) -> None:
    """Run console commands over RCON."""
    import time

    try:
        with connect() as client:
            if file is not None:
                for raw in file:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    console.print(f"[cyan]>[/] {line}")
                    out = client.command(line)
                    if out:
                        console.print(f"  {out}")
                    time.sleep(delay)
            elif command:
                out = client.command(" ".join(command))
                if out:
                    console.print(out)
            else:
                port = properties().rcon_port
                console.print(f"[dim]RCON 127.0.0.1:{port} -- 'quit' or Ctrl-D to exit[/]")
                while True:
                    try:
                        line = console.input("[bold cyan]rcon>[/] ").strip()
                    except (EOFError, KeyboardInterrupt):
                        console.print()
                        break
                    if not line:
                        continue
                    if line in ("quit", "exit"):
                        break
                    out = client.command(line)
                    if out:
                        console.print(out)
    except RconError as exc:
        fail(str(exc))


@app.command(hidden=True)
def supervise(
    heap: str = typer.Option("12G", "--heap", help="Heap for the JVM this supervises."),
) -> None:
    """Run the server in the foreground, restarting it on crash.

    This is what `mc start` runs inside tmux. Run it directly to supervise the
    server in the current terminal instead.
    """
    raise typer.Exit(Supervisor(Paths.from_env(), _jvm(heap)).run())
