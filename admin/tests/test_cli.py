"""Tests for how `mc --help` is laid out.

The arrangement is data (`cli.LAYOUT`), so it can be checked like data: every
command has a home, every home names a real command, and the order the table
declares is the order the help prints.
"""

import typer
from typer.main import get_command, get_command_name

from mcadmin import cli


def registered() -> set[str]:
    """Every command name the root exposes, hidden ones excluded."""
    names = {
        command.name or get_command_name(command.callback.__name__)
        for command in cli.app.registered_commands
        if not command.hidden
    }
    return names | {group.name for group in cli.app.registered_groups if group.name}


def test_every_command_has_a_panel():
    assert registered() - set(cli.PANELS) == set(), (
        "a command with no entry in LAYOUT falls into typer's default panel"
    )


def test_the_layout_names_no_command_that_does_not_exist():
    assert set(cli.PANELS) - registered() == set()


def test_a_command_belongs_to_exactly_one_panel():
    assert len(cli.ORDER) == len(set(cli.ORDER))


def test_the_help_lists_commands_in_the_layouts_order():
    listed = get_command(cli.app).list_commands(None)
    assert [name for name in listed if name in cli.PANELS] == [
        name for name in cli.ORDER if name in listed
    ]


def test_snapshots_are_not_stranded_below_the_developer_commands():
    listed = get_command(cli.app).list_commands(None)
    assert listed.index("snapshot") < listed.index("test")


def test_hidden_commands_stay_out_of_the_help():
    assert "supervise" not in {
        command.name or get_command_name(command.callback.__name__)
        for command in cli.app.registered_commands
        if not command.hidden
    }


def test_a_placed_command_gets_its_panel():
    spare = typer.Typer()

    @spare.command()
    def status() -> None:
        """Named in LAYOUT."""

    cli.apply_layout(spare)
    assert spare.registered_commands[0].rich_help_panel == "Server"


def test_an_unplaced_command_gets_no_panel():
    """Typer prints its default panel above the named ones, so a command
    nobody placed is the first thing in the help rather than the last."""
    spare = typer.Typer()

    @spare.command()
    def brand_new() -> None:
        """Added without a LAYOUT entry."""

    cli.apply_layout(spare)
    assert spare.registered_commands[0].rich_help_panel is None


def test_a_command_named_with_an_underscore_is_found_by_its_dashed_name():
    spare = typer.Typer()

    @spare.command("why-slow")
    def why_slow() -> None:
        """Registered under an explicit dashed name."""

    cli.apply_layout(spare)
    assert spare.registered_commands[0].rich_help_panel == "Performance and health"
