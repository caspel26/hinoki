"""``hinoki manage seed``: create a demo member after ``hinoki migrations apply``."""

import asyncio
from argparse import ArgumentParser, Namespace
from typing import cast

from hinoki.crypto import random_token
from hinoki.db import Database
from hinoki.management import Command, CommandContext
from hinoki.orm import use_database
from tracker.apps.members.models import Member
from tracker.apps.members.security import hash_token
from tracker.settings import TrackerSettings


async def create_member(url: str, name: str, token: str) -> None:
    database = await Database.connect(url, max_size=1)
    try:
        with use_database(database):
            await Member.acreate(name=name, token_hash=hash_token(token))
    finally:
        database.close()


class Seed(Command):
    name = "seed"
    help = "create a demo member (synthetic data)"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--name", default="Ada Demo")
        parser.add_argument("--token", help="default: a new random token")

    def handle(self, context: CommandContext, args: Namespace) -> None:
        settings = cast("TrackerSettings", context.settings)
        url = settings.database.url.reveal()
        token = args.token or random_token()
        asyncio.run(create_member(url, args.name, token))
        print(f"member {args.name!r} created; token {token!r} (shown once)")
