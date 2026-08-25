"""Development commands for the admin package itself."""

from __future__ import annotations

import shutil
import subprocess

import typer

from ..core.models import Paths
from ..ui.console import console, fail

app = typer.Typer()


@app.command()
def test(
    lint: bool = typer.Option(True, "--lint/--no-lint", help="Also run ruff."),
    fix: bool = typer.Option(False, "--fix", help="Let ruff apply what it can fix."),
    pytest_args: list[str] = typer.Argument(None, help="Extra arguments for pytest."),
) -> None:
    """Run the admin test suite and linter through uv."""
    admin = Paths.from_env().admin_dir
    if shutil.which("uv"):
        runner = ["uv", "run", "--directory", str(admin)]
    else:
        console.print("[yellow]uv not found -- falling back to the venv.[/]")
        runner = [str(admin / ".venv" / "bin" / "python"), "-m"]

    failed: list[str] = []
    if lint:
        console.print("[bold]ruff[/]")
        ruff = [*runner, "ruff", "check", *(["--fix"] if fix else []), "."]
        if subprocess.run(ruff, cwd=admin).returncode != 0:
            failed.append("ruff")
        console.print()

    console.print("[bold]pytest[/]")
    if subprocess.run([*runner, "pytest", "-q", *(pytest_args or [])], cwd=admin).returncode != 0:
        failed.append("pytest")

    console.print()
    if failed:
        fail(f"{' and '.join(failed)} failed.")
    console.print("[bold green]All green.[/]")
