"""Rendering the log digest."""

from __future__ import annotations

from datetime import datetime, timedelta

from rich.panel import Panel
from rich.table import Table

from ..core.digest import Digest, Problem
from ..core.logs import Level
from .console import console, rows

LEVEL_STYLE = {Level.WARN: "yellow", Level.ERROR: "red", Level.FATAL: "bold red"}
CHAT_PREVIEW = 12


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def show_digest(digest: Digest, show_chat: bool, now: datetime | None = None) -> None:
    if not digest.records:
        console.print("No log records in that window.")
        return

    console.print(
        Panel(
            f"[bold]{digest.start:%Y-%m-%d %H:%M}[/] -> [bold]{digest.end:%Y-%m-%d %H:%M}[/]"
            f"  [dim]{digest.records} lines, {digest.problem_records} of them warnings "
            f"or errors[/]",
            expand=False,
        )
    )

    _players(digest, now)
    _problems(digest)
    if show_chat:
        _chat(digest)


def _players(digest: Digest, now: datetime | None) -> None:
    if digest.sessions:
        console.print("\n[bold]Players[/]")
        table = Table(box=None, pad_edge=False, header_style="dim")
        table.add_column("player")
        table.add_column("sessions", justify="right")
        table.add_column("playtime", justify="right")
        counts: dict[str, int] = {}
        for session in digest.sessions:
            counts[session.player] = counts.get(session.player, 0) + 1
        online = {s.player for s in digest.sessions if s.still_online}
        for player, total in digest.playtime(now).items():
            name = f"{player} [green]*[/]" if player in online else player
            table.add_row(name, str(counts[player]), _duration(total))
        console.print(table)
        if online:
            console.print("[dim]* still online[/]")

    if digest.deaths:
        console.print(f"\n[bold]Deaths[/] [dim]({len(digest.deaths)})[/]")
        for event in digest.deaths[-8:]:
            console.print(f"  [dim]{event.at:%m-%d %H:%M}[/]  {event.player} {event.detail}")
        if len(digest.deaths) > 8:
            console.print(f"  [dim]... {len(digest.deaths) - 8} earlier[/]")

    if digest.advancements:
        console.print(f"\n[bold]Advancements[/] [dim]({len(digest.advancements)})[/]")
        recent = digest.advancements[-6:]
        for event in recent:
            console.print(f"  [dim]{event.at:%m-%d %H:%M}[/]  {event.player} -- {event.detail}")
        if len(digest.advancements) > len(recent):
            console.print(f"  [dim]... {len(digest.advancements) - len(recent)} earlier[/]")

    if digest.mob_deaths:
        total = sum(digest.mob_deaths.values())
        console.print(f"\n[bold]Mob deaths[/] [dim]({total})[/]")
        for detail, count in list(digest.mob_deaths.items())[:5]:
            console.print(f"  {count:3d}  [dim]{detail}[/]")

    if digest.disconnects:
        reasons: dict[str, int] = {}
        for event in digest.disconnects:
            reasons[event.detail] = reasons.get(event.detail, 0) + 1
        console.print(f"\n[bold]Disconnects[/] [dim]({len(digest.disconnects)})[/]")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:4]:
            console.print(f"  {count:3d}  [dim]{reason}[/]")


def _problem_table(problems: list[Problem], limit: int) -> Table:
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("n", justify="right", width=5)
    table.add_column("level", width=5)
    table.add_column("first", width=11)
    table.add_column("message", overflow="fold")
    for problem in problems[:limit]:
        style = LEVEL_STYLE.get(problem.level, "")
        table.add_row(
            str(problem.count),
            f"[{style}]{problem.level.value}[/]" if style else problem.level.value,
            f"{problem.first_seen:%m-%d %H:%M}",
            problem.sample,
        )
    return table


def _problems(digest: Digest, limit: int = 15) -> None:
    if digest.learned:
        console.print(
            f"\n[bold]Baseline established[/] from {digest.distinct_problems} distinct "
            f"problem patterns."
        )
        console.print(
            "[dim]Nothing is 'new' on a first run. From now on this reports only "
            "patterns that were not already in the baseline.[/]"
        )
        return

    if digest.new_problems:
        console.print(
            f"\n[bold red]New problems[/] [dim]({len(digest.new_problems)} pattern(s) "
            f"not seen before)[/]"
        )
        console.print(_problem_table(digest.new_problems, limit))
        if len(digest.new_problems) > limit:
            console.print(f"  [dim]... {len(digest.new_problems) - limit} more[/]")
    else:
        console.print(
            "\n[bold green]No new problems.[/] "
            "[dim]Nothing unfamiliar in this window.[/]"
        )

    if digest.recurring:
        console.print("\n[bold]Loudest known problems[/] [dim](already in the baseline)[/]")
        console.print(_problem_table(digest.recurring, len(digest.recurring)))

    rows({"baseline": f"{digest.baseline_size} known pattern(s)"})


def _chat(digest: Digest) -> None:
    if not digest.chat:
        return
    console.print(f"\n[bold]Chat[/] [dim]({len(digest.chat)})[/]")
    for event in digest.chat[-CHAT_PREVIEW:]:
        console.print(f"  [dim]{event.at:%m-%d %H:%M}[/]  <{event.player}> {event.detail}")
    if len(digest.chat) > CHAT_PREVIEW:
        console.print(f"  [dim]... {len(digest.chat) - CHAT_PREVIEW} earlier[/]")
