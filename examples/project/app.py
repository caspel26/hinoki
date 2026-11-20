"""A small app for trying project management commands.

Run ``uv run hinoki manage greet`` from the examples project root, or pass
``project.app:app`` explicitly outside a registered project.
"""

from argparse import ArgumentParser, Namespace

from hinoki import Hinoki, settings
from hinoki.management import Command, CommandContext


class Greet(Command):
    name = "greet"
    help = "print a greeting"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("name", nargs="?", default="world")

    def handle(self, _context: CommandContext, args: Namespace) -> None:
        print(f"Hello, {args.name}!")


class ExampleSettings(settings.AppSettings, prefix="EXAMPLES_"):
    database: settings.DatabaseSettings


app = Hinoki(settings=ExampleSettings, commands=[Greet])
