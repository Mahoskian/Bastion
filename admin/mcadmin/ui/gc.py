"""Rendering the GC report."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..core.gclog import Analysis, Level, Run, analyze
from ..core.units import human_bytes, human_seconds
from .console import console, rows

LEVEL_STYLE = {Level.INFO: "", Level.WARN: "yellow", Level.ERROR: "red"}

CAVEAT = (
    "Measured over this run only -- pregeneration and a full server retain more."
    " Re-run after a busy session before committing to a smaller heap."
)


def show_report(analysis: Analysis, index: int, total: int) -> None:
    run = analysis.run
    heap = run.heap_max
    console.print(
        Panel(
            f"[bold]JVM run {index} of {total}[/]  started {run.started:%Y-%m-%d %H:%M:%S}"
            f"  ran {human_seconds(run.seconds)}",
            expand=False,
        )
    )
    region = run.settings.get("Heap Region Size", "?")
    rows(
        {
            "heap": f"{human_bytes(heap)}  [dim]region {region}[/]",
            "cpus": run.settings.get("CPUs", "?"),
            "collections": (
                f"{len(run.pauses)}  [dim]+ {run.concurrent_cycles} concurrent mark cycles[/]"
            ),
        }
    )

    _pauses(analysis)
    _memory(analysis)
    _sizing(analysis, heap)
    _findings(analysis)
    console.print(f"\n[dim]{CAVEAT}[/]")


def _pauses(analysis: Analysis) -> None:
    console.print()
    console.print("[bold]Pauses[/]")
    pct = analysis.pause_percentiles
    overhead = f"{analysis.gc_overhead * 100:.3f}%"
    if analysis.gc_overhead < 0.02:
        overhead += " [dim](negligible)[/]"
    block = {
        "p50 / p95 / p99": f"{pct['p50']:.2f} / {pct['p95']:.2f} / {pct['p99']:.2f} ms",
    }
    if analysis.worst is not None:
        worst = analysis.worst
        block["max"] = f"{pct['max']:.2f} ms  [dim]{worst.label} at {worst.at:%H:%M:%S}[/]"
    block["total stopped"] = f"{analysis.stw_total_ms:.0f} ms"
    block["GC overhead"] = overhead
    if analysis.non_gc_stw_ms > 1.0:
        top = max(analysis.non_gc_worst.items(), key=lambda kv: kv[1][1])
        block["non-GC pauses"] = (
            f"{analysis.non_gc_stw_ms:.0f} ms  [dim]worst: {top[0]} x{top[1][0]}[/]"
        )
    rows(block)

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("kind")
    for name in ("count", "total", "mean"):
        table.add_column(name, justify="right")
    for label, (count, total_ms) in sorted(analysis.by_label.items(), key=lambda kv: -kv[1][1]):
        table.add_row(label, str(count), f"{total_ms:.0f} ms", f"{total_ms / count:.2f} ms")
    console.print(table)


def _memory(analysis: Analysis) -> None:
    console.print()
    console.print("[bold]Memory[/]")
    block = {
        "live set": (
            f"[bold]{human_bytes(analysis.live_bytes)}[/]  [dim]{analysis.live_source}[/]"
        ),
    }
    if analysis.old_bytes:
        block["old + humongous"] = f"{human_bytes(analysis.old_bytes)}  [dim]cross-check[/]"
    block["peak used"] = human_bytes(analysis.peak_bytes)
    block["allocation"] = f"{human_bytes(int(analysis.alloc_rate))}/s"
    block["promotion"] = f"{human_bytes(int(analysis.promo_rate))}/s  [dim]into old gen[/]"
    rows(block)


def _sizing(analysis: Analysis, heap: int) -> None:
    from ..core.gclog import LIVE_SET_MULTIPLIER, TARGET_YOUNG_INTERVAL

    console.print()
    console.print("[bold]Sizing[/]")
    colour = "green" if analysis.recommended >= heap else "yellow"
    rows(
        {
            "current -Xmx": f"{human_bytes(heap)}  [dim]{analysis.headroom:.0f}x the live set[/]",
            "suggested": (
                f"[bold {colour}]{human_bytes(analysis.recommended)}[/]"
                f"  [dim]max(live x{LIVE_SET_MULTIPLIER:.0f}, live + "
                f"{TARGET_YOUNG_INTERVAL:.0f}s of allocation)[/]"
            ),
        }
    )
    if analysis.recommended < heap:
        heap_arg = human_bytes(analysis.recommended).replace(".00", "")
        console.print(f"\n[dim]  mc start --heap {heap_arg}[/]")


def _findings(analysis: Analysis) -> None:
    if not analysis.notes:
        return
    console.print()
    # A table gives the findings a hanging indent; plain prints wrap to column 0.
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim", width=3)
    table.add_column(overflow="fold")
    for note in analysis.notes:
        style = LEVEL_STYLE[note.level]
        table.add_row(" *", f"[{style}]{note.message}[/]" if style else note.message)
    console.print(table)


def show_runs(runs: list[Run]) -> None:
    table = Table(header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("started")
    for name in ("ran", "GCs", "live set", "alloc/s", "p99", "suggest"):
        table.add_column(name, justify="right")
    for i, run in enumerate(reversed(runs), 1):
        a = analyze(run)
        table.add_row(
            str(i),
            f"{run.started:%m-%d %H:%M}",
            human_seconds(run.seconds),
            str(len(run.pauses)),
            human_bytes(a.live_bytes),
            human_bytes(int(a.alloc_rate)),
            f"{a.pause_percentiles['p99']:.1f}ms",
            human_bytes(a.recommended),
        )
    console.print(table)
    console.print("[dim]Inspect one with: mc gc --run N[/]")
