"""Deterministic rows for the native generated CRUD orientation run."""

import sys
import uuid

import psycopg

COUNT = 10_000
NAMESPACE = uuid.UUID("c0f4d724-89e3-4b49-b981-77c02fd8aa01")
FIXED_ID = uuid.uuid5(NAMESPACE, "post-1")
BASE_TITLE = "Post 00001"
BODY = "Orientation fixture body. " * 8


def post_id(index: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"post-{index}")


def seed(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("TRUNCATE phase1_posts")
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO phase1_posts (id, title, body) VALUES (%s, %s, %s)",
                [
                    (post_id(index), f"Post {index:05d}", BODY)
                    for index in range(1, COUNT + 1)
                ],
            )
        connection.execute("ANALYZE phase1_posts")


if __name__ == "__main__":
    seed(sys.argv[1])
