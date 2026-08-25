"""Log commands: tail the raw file, or digest it."""

from __future__ import annotations

import subprocess
from datetime import datetime

import typer

from ..core import digest as dg
from ..core import logs as lg
from ..core.models import Paths
from ..core.units import parse_duration
from ..ui import logs as view
from ..ui.console import console, fail

app = typer.Typer(no_args_is_help=True, help="Server log: tail it, or digest it.")

@app.command()
def tail(
    lines: int = typer.Option(40, "--lines", "-n", help="How many lines to show."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep streaming."),
) -> None:
    """Show the end of the current log."""
    path = Paths.from_env().latest_log
    if not path.exists():
        fail(f"no log at {path}")
    args = ["tail", "-n", str(lines), *(["-f"] if follow else []), str(path)]
    try:
        raise typer.Exit(subprocess.run(args).returncode)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None


@app.command()
def digest(
    since: str = typer.Option(
        None, "--since", help="Only this far back, e.g. 6h, 90m, 3d. Default: everything."
    ),
    current: bool = typer.Option(
        False, "--current", help="Only the current session (latest.log)."
    ),
    chat: bool = typer.Option(True, "--chat/--no-chat", help="Include chat."),
    learn: bool = typer.Option(
        True, "--learn/--no-learn", help="Add what is reported to the baseline."
    ),
    reset: bool = typer.Option(False, "--reset", help="Forget the baseline and start over."),
) -> None:
    """Summarise the log, showing only problems not seen before."""
    paths = Paths.from_env()
    baseline = dg.Baseline(paths.log_baseline)
    if reset:
        baseline.reset()
        console.print("[yellow]Baseline cleared.[/]")

    files = lg.log_files(paths.logs_dir)
    if current:
        files = [path for path in files if path.name == "latest.log"]
    if not files:
        fail(f"no logs in {paths.logs_dir}")

    records = list(lg.parse(files))
    if since is not None:
        try:
            window = parse_duration(since)
        except ValueError as exc:
            fail(str(exc))
        cutoff = datetime.now() - window
        records = [record for record in records if record.at >= cutoff]

    result = dg.build(records, baseline)
    view.show_digest(result, show_chat=chat)

    if learn and result.records:
        baseline.remember([*result.new_problems, *result.recurring])
