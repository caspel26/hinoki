"""Load the deterministic rows of the benchmark service. Same rows every run.

uv run python parity_seed.py postgres://user:pass@127.0.0.1:PORT/bench
"""

import sys
from pathlib import Path

import parity_data
import psycopg

INSERTS = (
    ("benchmark_users", "id, username, email, first_name, last_name, is_active",
     parity_data.users),
    ("articles_author", "id, name, email, bio, created_at", parity_data.authors),
    ("articles_tag", "id, name, slug", parity_data.tags),
    ("articles_article",
     "id, title, content, published, author_id, created_at, updated_at",
     parity_data.articles),
    ("articles_article_tags", "id, article_id, tag_id", parity_data.article_tags),
    ("articles_comment", "id, article_id, author_name, content, created_at",
     parity_data.comments),
    ("auth_user", "id, password, last_login, is_superuser, username, first_name, "
     "last_name, email, is_staff, is_active, date_joined",
     lambda: [parity_data.auth_user()]),
)  # fmt: skip


def main(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute((Path(__file__).parent / "parity_schema.sql").read_text())
        with conn.cursor() as cur:
            for table, columns, rows in INSERTS:
                marks = ", ".join(["%s"] * len(columns.split(",")))
                cur.executemany(
                    f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                    rows(),
                )
        conn.execute("ANALYZE")


if __name__ == "__main__":
    main(sys.argv[1])
