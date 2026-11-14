"""Deterministic seed: 100 authors, 10 000 posts (80 % published). Same rows every run.

uv run python seed.py postgres://user:pass@127.0.0.1:PORT/bench
"""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

NAMESPACE = uuid.UUID("5b1d7c52-0000-4000-8000-000000000053")
AUTHORS = 100
POSTS = 10_000
START = datetime(2026, 1, 1, tzinfo=UTC)


def author_id(n: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"author-{n}")


def post_id(n: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"post-{n}")


def main(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute((Path(__file__).parent / "schema.sql").read_text())
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO authors (id, name) VALUES (%s, %s)",
                [(author_id(n), f"Author {n:03d}") for n in range(AUTHORS)],
            )
            cur.executemany(
                "INSERT INTO posts (id, author_id, title, body, published, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (
                        post_id(n),
                        author_id(n % AUTHORS),
                        f"Post number {n}",
                        f"Body of post {n}. " * 12,
                        n % 5 != 0,
                        START + timedelta(minutes=n),
                    )
                    for n in range(POSTS)
                ],
            )
        conn.execute("ANALYZE")


if __name__ == "__main__":
    main(sys.argv[1])
