"""``python -m examples.blog setup`` creates tables on BLOG_DATABASE_URL."""

import asyncio
import os
import sys

from examples.blog.tables import SQL
from hinoki.db import Database


async def setup(url: str) -> None:
    database = await Database.connect(url, max_size=1)
    for statement in SQL:
        await database.execute(statement)
    database.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["setup"]:
        sys.exit("usage: python -m examples.blog setup")
    asyncio.run(setup(os.environ["BLOG_DATABASE_URL"]))
