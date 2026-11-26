"""The real seed path: tokens are printed once and stored only as digests."""

import re
from pathlib import Path

from blog.apps.blog.models import User
from blog.apps.blog.security import hash_token
from blog.apps.blog.seed import seed
from blog.main import build_app
from blog.settings import BlogSettings
from blogworld import ADMIN_SECRET, JWT_SECRET

from hinoki.db import Database
from hinoki.orm import use_database
from hinoki.testing import ASGIClient


async def test_seeded_tokens_are_random_and_stored_only_as_digests(
    tmp_path: Path,
) -> None:
    url = f"sqlite://{tmp_path / 'seeded.sqlite3'}"
    tokens = await seed(url)

    database = await Database.connect(url, max_size=1)
    with use_database(database):
        ada = await User.aget(username="ada")
    database.close()
    assert len(set(tokens.values())) == 3
    assert re.fullmatch(r"[0-9a-f]{64}", ada.token_hash)
    assert ada.token_hash not in tokens.values()
    assert ada.token_hash == hash_token(tokens["ada"])

    settings = BlogSettings.load(
        overrides={
            "database": {"url": url},
            "admin": {"secret": ADMIN_SECRET},
            "jwt_secret": JWT_SECRET,
        }
    )
    async with ASGIClient(build_app(settings)) as client:
        drafts = await client.get("/posts/drafts", token=tokens["ada"])
        refused = await client.get("/posts/drafts", token=hash_token(tokens["ada"]))
    assert drafts.status == 200
    assert refused.status == 401
