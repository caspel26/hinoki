"""Check one benchmark service against an oracle that uses no framework.

    uv run python parity_verify.py NAME http://127.0.0.1:8801

Every service must answer each route with the same status and data as rows read
straight from PostgreSQL, issue the same number of SQL statements (counted with
pg_stat_statements after warming every worker; see ``expected_statements`` for the
one disclosed exception), and return the two ``json-*``
bodies byte for byte. A service that fails any check stops the run.
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import parity_data
import psycopg
from psycopg.rows import dict_row

DB = os.environ.get(
    "BENCH_DATABASE_URL", "postgres://postgres:bench-only@127.0.0.1:54402/bench"
)
# Only data statements count: transaction control, and the empty statement or
# ``SELECT 1`` a pool may send to test a connection, are not part of a handler.
STATEMENT = re.compile(r"^\s*(select|insert|update|delete)\b", re.I)
PING = re.compile(r"^\s*select\s+\$?1\s*;?\s*$", re.I)
LIST = "/articles?page=1&page_size=20"
DETAIL = "/articles/1"
# Statement counts of Django Bolt's own handlers: list = COUNT + articles joined with
# their author + tags; detail = article with author + tags + comments; the
# authenticated routes add the user row.
EXPECTED_STATEMENTS = {
    "json-1k": 0,
    "json-10k": 0,
    "db": 1,
    "articles": 3,
    "article": 3,
    "auth-me": 1,
    "auth-articles": 4,
}
# Hinoki's ORM loads a many-to-many relation in two steps, the link table then
# the targets, so each route that prefetches tags issues one more statement. That
# extra round trip is a cost Hinoki pays, never a shortcut, and is reported.
TWO_STEP_M2M_STACKS = frozenset({"hinoki", "hinoki-granian", "hinoki-kiso"})
TAG_ROUTES = frozenset({"articles", "article", "auth-articles"})


def expected_statements(stack: str, route: str) -> int:
    extra = stack in TWO_STEP_M2M_STACKS and route in TAG_ROUTES
    return EXPECTED_STATEMENTS[route] + int(extra)


def statements(conn: psycopg.Connection) -> list[tuple[str, int]]:
    """Data statements recorded by pg_stat_statements since the last reset."""
    found = conn.execute(
        "SELECT query, calls FROM pg_stat_statements WHERE query NOT ILIKE %s "
        "ORDER BY query",
        ("%pg_stat_statements%",),
    ).fetchall()
    return [
        (query, calls)
        for query, calls in found
        if STATEMENT.match(query) and not PING.match(query)
    ]


def moment(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch(base: str, path: str, token: str | None = None) -> tuple[int, str, bytes]:
    """Status, content type and the exact body bytes."""
    request = urllib.request.Request(base + path)
    if token is not None:
        request.add_header("cookie", f"{parity_data.COOKIE}={token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, reply.headers.get("content-type", ""), reply.read()
    except urllib.error.HTTPError as error:
        return error.code, "", b""


def call(base: str, path: str, token: str | None = None) -> tuple[int, Any]:
    status, _, body = fetch(base, path, token)
    return status, json.loads(body) if status == 200 else None


def warm_database_connections(base: str, token: str) -> None:
    with ThreadPoolExecutor(max_workers=64) as executor:
        replies = list(
            executor.map(lambda _i: call(base, "/auth/articles", token), range(64))
        )
    if any(status != 200 for status, _ in replies):
        raise SystemExit("database connection warm-up failed")
    time.sleep(3)


def rows(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def oracle_users(conn: psycopg.Connection) -> list[dict]:
    return rows(
        conn,
        "SELECT id, username, email, first_name, last_name, is_active "
        "FROM benchmark_users ORDER BY id",
    )


def oracle_auth_user(conn: psycopg.Connection) -> dict:
    (user,) = rows(
        conn,
        "SELECT id, username, email, first_name, last_name, is_active "
        "FROM auth_user WHERE id = %s",
        (parity_data.AUTH_USER_ID,),
    )
    return user


def oracle_article(conn: psycopg.Connection, article: dict) -> dict:
    (author,) = rows(
        conn,
        "SELECT id, name, email, bio, created_at FROM articles_author WHERE id = %s",
        (article.pop("author_id"),),
    )
    tags = rows(
        conn,
        "SELECT t.id, t.name, t.slug FROM articles_tag t JOIN articles_article_tags "
        "l ON l.tag_id = t.id WHERE l.article_id = %s ORDER BY t.id",
        (article["id"],),
    )
    return {**article, "author": author, "tags": tags}


ARTICLE = (
    "SELECT id, title, content, published, author_id, created_at, updated_at "
    "FROM articles_article"
)


def oracle_page(conn: psycopg.Connection, page: int, page_size: int) -> dict:
    total = conn.execute("SELECT count(*) FROM articles_article").fetchone()[0]
    found = rows(
        conn,
        ARTICLE + " ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (page_size, (page - 1) * page_size),
    )
    return {
        "items": [oracle_article(conn, row) for row in found],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size),
    }


def oracle_detail(conn: psycopg.Connection, article_id: int) -> dict:
    (row,) = rows(conn, ARTICLE + " WHERE id = %s", (article_id,))
    comments = rows(
        conn,
        "SELECT id, article_id, author_name, content, created_at FROM "
        "articles_comment WHERE article_id = %s ORDER BY created_at DESC",
        (article_id,),
    )
    return {**oracle_article(conn, row), "comments": comments}


def normal(value: Any, key: str = "") -> Any:
    """Datetimes parsed (``Z`` and ``+00:00`` are equal) and tags in id order;
    tag order is not defined by the SQL of every stack."""
    if isinstance(value, dict):
        return {k: normal(v, k) for k, v in value.items()}
    if isinstance(value, list):
        items = [normal(v) for v in value]
        tagged = key == "tags" and all(isinstance(t, dict) for t in items)
        return sorted(items, key=lambda t: t["id"]) if tagged else items
    if key.endswith("_at"):
        return moment(value)
    return value


def users_by_id(value: Any) -> Any:
    """``/db`` reads ten rows without ``ORDER BY``: any row order is correct."""
    return sorted(value, key=lambda u: u["id"]) if isinstance(value, list) else value


class Checker:
    def __init__(
        self, conn: psycopg.Connection, stack: str, base: str, token: str
    ) -> None:
        self.conn, self.stack, self.base, self.token = conn, stack, base, token
        self.problems: list[str] = []
        self.report: list[str] = []

    def check(self, ok: bool, what: str) -> None:
        if not ok:
            self.problems.append(what)

    def counted(self, path: str) -> tuple[int, str, bytes, list[tuple[str, int]]]:
        for _ in range(60):  # warm worker-side route and response caches
            fetch(self.base, path, self.token)
        self.conn.execute("SELECT pg_stat_statements_reset()")
        status, content_type, body = fetch(self.base, path, self.token)
        return status, content_type, body, statements(self.conn)

    def route(
        self,
        name: str,
        path: str,
        expected: Any,
        shape: Callable[[Any], Any] = lambda value: value,
    ) -> None:
        status, content_type, body, sql = self.counted(path)
        issued = sum(calls for _, calls in sql)
        self.check(status == 200, f"{name} status {status}")
        self.check(
            content_type.startswith("application/json"),
            f"{name} content type {content_type!r}",
        )
        if status == 200:
            self.check(
                normal(shape(json.loads(body))) == normal(shape(expected)),
                f"{name} data differs from the oracle",
            )
        expected_sql = expected_statements(self.stack, name)
        self.check(
            issued == expected_sql,
            f"{name} issued {issued} statements, expected {expected_sql}",
        )
        self.report += [
            f"{name}: {len(body)} bytes, {issued} statements",
            *(f"  {c}x {' '.join(q.split())[:150]}" for q, c in sql),
        ]

    def payload(self, name: str) -> None:
        self.route(name, "/" + name, parity_data.PAYLOADS[name])
        _, _, body = fetch(self.base, "/" + name)
        self.check(
            body == parity_data.BODIES[name],
            f"{name} body is not the {len(parity_data.BODIES[name])} expected bytes",
        )

    def status(self, path: str, expected: int, token: str | None = None) -> None:
        found = fetch(self.base, path, token)[0]
        self.check(found == expected, f"{path} is {found}, expected {expected}")

    def page(self, query: str, page: int, page_size: int) -> None:
        status, data = call(self.base, "/articles?" + query)
        self.check(
            status == 200
            and normal(data) == normal(oracle_page(self.conn, page, page_size)),
            f"/articles?{query} differs from page {page} of {page_size}",
        )


def main(name: str, base: str) -> None:
    token = parity_data.mint_token()
    with psycopg.connect(DB, autocommit=True) as conn:
        checker = Checker(conn, name, base, token)
        checker.check(fetch(base, "/health")[0] == 200, "health")
        warm_database_connections(base, token)
        for payload in parity_data.PAYLOADS:
            checker.payload(payload)
        checker.route("db", "/db", oracle_users(conn), users_by_id)
        checker.route("articles", LIST, oracle_page(conn, 1, 20))
        checker.route("article", DETAIL, oracle_detail(conn, 1))
        checker.route("auth-me", "/auth/me", oracle_auth_user(conn))
        checker.route("auth-articles", "/auth" + LIST, oracle_page(conn, 1, 20))
        checker.page("page=2&page_size=5", 2, 5)
        checker.page("page=0&page_size=1000", 1, 100)
        checker.status("/articles/999999", 404)
        checker.status("/auth/me", 401)
        checker.status("/auth/articles", 401)
        checker.status("/auth/me", 401, token + "x")
        expired = parity_data.mint_token(now=time.time() - 2 * parity_data.TOKEN_TTL)
        checker.status("/auth/me", 401, expired)
    print(f"== {name}", *checker.report, sep="\n")
    if checker.problems:
        print("FAILED:", *checker.problems, sep="\n  ")
        sys.exit(1)
    print("OK: behaviour, bodies and statement counts match the oracle")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
