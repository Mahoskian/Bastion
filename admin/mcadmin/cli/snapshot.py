"""Deduplicated snapshot commands, backed by restic."""

from __future__ import annotations

from pathlib import Path

import typer

from ..core.repository import ResticError, ResticRepository
from ..ui import snapshot as view
from ..ui.console import console, fail
from .server import controller

app = typer.Typer(no_args_is_help=True, help="Deduplicated snapshots (restic).")


def repository() -> ResticRepository:
    return ResticRepository()


def _require_repo() -> ResticRepository:
    repo = repository()
    if not ResticRepository.available():
        fail("restic is not installed -- `sudo apt install restic`")
    if not repo.exists():
        fail(f"no repository at {repo.location} -- create one with 'mc snapshot init'")
    return repo


@app.command("init")
def init() -> None:
    """Create the snapshot repository."""
    repo = repository()
    if not ResticRepository.available():
        fail("restic is not installed -- `sudo apt install restic`")
    try:
        repo.init()
    except ResticError as exc:
        fail(str(exc))
    console.print(f"[bold green]Created[/] {repo.location}")
    console.print(f"[dim]password file: {repo.password_file} (mode 600)[/]")
    console.print(
        "[yellow]Keep that password file.[/] Without it the snapshots cannot be read -- "
        "copy it alongside the repository if you ever move it off this disk."
    )


@app.command("now")
def now(
    scheduled: bool = typer.Option(
        False, "--scheduled", help="Honour the pause flag (used by cron)."
    ),
    forget: bool = typer.Option(True, "--forget/--no-forget", help="Apply retention after."),
) -> None:
    """Take a snapshot."""
    repo = _require_repo()
    if scheduled and repo.paths.paused_flag.exists():
        reason = repo.paths.paused_flag.read_text().strip() or "no reason given"
        console.print(f"[yellow]Skipped:[/] {reason}")
        return

    with console.status("Snapshotting..."):
        try:
            with controller().quiesced() as quiesced:
                summary = repo.backup()
            if forget:
                repo.forget()
            stats = repo.stats()
        except ResticError as exc:
            fail(str(exc))
    view.show_summary(summary, stats)
    if not quiesced:
        console.print("[dim]cold snapshot -- the world was not quiesced[/]")


@app.command("list")
def list_snapshots() -> None:
    """List snapshots, newest first."""
    repo = _require_repo()
    try:
        view.show_snapshots(repo.snapshots(), repo.stats())
    except ResticError as exc:
        fail(str(exc))
    if repo.paths.paused_flag.exists():
        reason = repo.paths.paused_flag.read_text().strip() or "no reason given"
        console.print(f"[yellow]Scheduled snapshots are PAUSED[/] -- {reason}")


@app.command("stats")
def stats() -> None:
    """Show repository size after deduplication."""
    repo = _require_repo()
    try:
        view.show_stats(repo.stats(), repo.location)
    except ResticError as exc:
        fail(str(exc))


@app.command("check")
def check(
    read_data: bool = typer.Option(
        False, "--read-data", help="Also re-read a 5% sample of the actual data."
    ),
) -> None:
    """Verify repository integrity."""
    repo = _require_repo()
    with console.status("Checking..."):
        try:
            repo.check(read_data=read_data)
        except ResticError as exc:
            fail(str(exc))
    console.print("[bold green]Repository is intact.[/]")


@app.command("forget")
def forget(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would go, delete nothing."),
) -> None:
    """Apply retention: 12h hourly + 7d daily + 4w weekly."""
    repo = _require_repo()
    try:
        removed = repo.forget(dry_run=dry_run)
    except ResticError as exc:
        fail(str(exc))
    if not removed:
        console.print("Nothing to forget.")
        return
    verb = "would remove" if dry_run else "removed"
    for short_id in removed:
        console.print(f"  [red]{verb}[/] {short_id}")
    console.print(f"[dim]{len(removed)} snapshot(s)[/]")


@app.command("pause")
def pause(reason: str = typer.Argument("maintenance", help="Why snapshots are paused.")) -> None:
    """Stop scheduled (cron) snapshots until resumed. Manual 'now' still works."""
    flag = repository().paths.paused_flag
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(reason)
    console.print(f"[yellow]Scheduled snapshots PAUSED[/] -- {reason}")
    console.print("[dim]Resume with: mc snapshot resume[/]")


@app.command("resume")
def resume() -> None:
    """Re-enable scheduled snapshots."""
    flag = repository().paths.paused_flag
    if not flag.exists():
        console.print("Scheduled snapshots were not paused.")
        return
    reason = flag.read_text().strip()
    flag.unlink()
    console.print(f"[green]Scheduled snapshots RESUMED[/] [dim](was: {reason})[/]")


@app.command("restore")
def restore(
    selector: str = typer.Argument("latest", help="Snapshot number, id, or 'latest'."),
    target: Path = typer.Option(
        None, "--target", help="Restore here instead of back over the server."
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Restore a snapshot (server must be stopped for an in-place restore)."""
    repo = _require_repo()
    try:
        snapshot = repo.resolve(selector)
    except LookupError as exc:
        fail(str(exc))

    in_place = target is None
    if in_place and controller().state().is_live:
        fail(
            "the server is running -- stop it first.\n"
            "        Restoring under a live server corrupts the world."
        )

    where = str(repo.paths.server_dir) if in_place else str(target)
    console.print(
        f"[bold]{snapshot.short_id}[/] from {snapshot.time:%Y-%m-%d %H:%M:%S} -> {where}"
    )
    if in_place:
        console.print("[yellow]This overwrites the live server files.[/]")
    if not yes and console.input("\nType [bold]restore[/] to proceed: ").strip() != "restore":
        console.print("Aborted.")
        raise typer.Exit(1)

    with console.status("Restoring..."):
        try:
            if in_place:
                repo.restore_in_place(snapshot)
            else:
                repo.restore(snapshot, target)
        except ResticError as exc:
            fail(str(exc))
    console.print(f"[bold green]Restored[/] {snapshot.short_id}")
