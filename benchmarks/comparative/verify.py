"""Check one running service against an oracle that does not use any framework.

    uv run python verify.py NAME http://127.0.0.1:8801

For the same requests, every service must return the same status and the same data
(compared after parsing, so ``Z`` versus ``+00:00`` does not matter) as the rows
read straight from PostgreSQL, and must issue the same number of SQL statements
(counted with pg_stat_statements, transaction control excluded). The ``json-1k`` and
``json-10k`` bodies must be byte-identical to ``payloads.py`` and issue no SQL. A service that
fails any check makes the run stop: a speed number for a different behaviour would
be meaningless.
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import payloads
import psycopg
from seed import author_id, post_id

DB = os.environ.get(
    "BENCH_DATABASE_URL", "postgres://postgres:bench-only@127.0.0.1:54402/bench"
)
STATEMENT = re.compile(r"^\s*(select|insert|update|delete)\b", re.I)


def call(base: str, method: str, path: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    if data:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None


def fetch_raw(base: str, path: str) -> tuple[int, str, bytes]:
    """Status, content type and the exact body bytes."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as reply:
            return reply.status, reply.headers.get("content-type", ""), reply.read()
    except urllib.error.HTTPError as error:
        return error.code, "", b""


def verify_payload(
    conn: psycopg.Connection, base: str, scenario: str
) -> tuple[list[str], list[str]]:
    """Problems and report lines for one fixed JSON body: same bytes, no SQL."""
    path = "/" + scenario
    for _ in range(60):
        fetch_raw(base, path)
    conn.execute("SELECT pg_stat_statements_reset()")
    status, content_type, body = fetch_raw(base, path)
    sql = statements(conn)
    problems = []
    if status != 200:
        problems.append(f"{scenario} status {status}")
    if not content_type.startswith("application/json"):
        problems.append(f"{scenario} content type {content_type!r}")
    if body != payloads.BODIES[scenario]:
        problems.append(
            f"{scenario} body is not the {len(payloads.BODIES[scenario])} expected bytes"
        )
    elif json.loads(body) != payloads.ITEMS[scenario]:
        problems.append(f"{scenario} data differs")
    if sql:
        problems.append(
            f"{scenario} issued {sum(c for _, c in sql)} statements, expected 0"
        )
    return problems, [
        f"{scenario}: {len(body)} bytes, {sum(c for _, c in sql)} statements"
    ]


def warm_database_connections(base: str) -> None:
    with ThreadPoolExecutor(max_workers=64) as executor:
        replies = list(
            executor.map(
                lambda _index: call(base, "GET", "/posts?page=1&page_size=20"),
                range(64),
            )
        )
    if any(status != 200 for status, _data in replies):
        raise SystemExit("database connection warm-up failed")
    time.sleep(3)


def statements(conn: psycopg.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT query, calls FROM pg_stat_statements WHERE query NOT ILIKE %s ORDER BY query",
        ("%pg_stat_statements%",),
    ).fetchall()
    # Foreign-key checks are PostgreSQL's own trigger statements, not the service's.
    return [(q, c) for q, c in rows if STATEMENT.match(q) and "FOR KEY SHARE" not in q]


def moment(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def shape(post: dict) -> dict:
    return {**post, "id": str(post["id"]), "created_at": moment(post["created_at"]),
            "author": {"id": str(post["author"]["id"]), "name": post["author"]["name"]}}  # fmt: skip


def oracle(
    conn: psycopg.Connection, where: str, params: tuple, tail: str = ""
) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.title, p.body, p.published, p.created_at, a.id, a.name "
        f"FROM posts p JOIN authors a ON a.id = p.author_id WHERE {where} {tail}",
        params,
    ).fetchall()
    return [
        {"id": str(r[0]), "title": r[1], "body": r[2], "published": r[3], "created_at": r[4],
         "author": {"id": str(r[5]), "name": r[6]}}
        for r in rows
    ]  # fmt: skip


def main(name: str, base: str) -> None:
    problems: list[str] = []
    report: list[str] = [f"== {name}"]

    def check(ok: bool, what: str) -> None:
        if not ok:
            problems.append(what)

    with psycopg.connect(DB, autocommit=True) as conn:

        def counted(
            method: str, path: str, body: object = None
        ) -> tuple[int, object, list]:
            for _ in range(60):  # warm worker-side route and response caches
                call(base, method, path, body)
            conn.execute("SELECT pg_stat_statements_reset()")
            status, data = call(base, method, path, body)
            return status, data, statements(conn)

        check(call(base, "GET", "/health") == (200, {"status": "ok"}), "health")
        warm_database_connections(base)
        for scenario in payloads.BODIES:
            found, lines = verify_payload(conn, base, scenario)
            problems += found
            report += lines

        status, data, sql = counted("GET", "/posts?page=2&page_size=5")
        expected = oracle(
            conn,
            "p.published",
            (),
            "ORDER BY p.created_at DESC, p.id DESC LIMIT 5 OFFSET 5",
        )
        total = conn.execute("SELECT count(*) FROM posts WHERE published").fetchone()[0]
        check(status == 200, "list status")
        if status == 200:
            check(
                [shape(i) for i in data["items"]] == [shape(i) for i in expected],
                "list items",
            )
            check(
                (data["page"], data["page_size"], data["total"], data["pages"])
                == (2, 5, total, math.ceil(total / 5)),
                "list envelope",
            )
        check(
            sum(c for _, c in sql) == 2,
            f"list issued {sum(c for _, c in sql)} statements, expected 2",
        )
        report += ["list:", *(f"  {c}x {' '.join(q.split())[:150]}" for q, c in sql)]

        target = str(post_id(1))
        status, data, sql = counted("GET", f"/posts/{target}")
        (row,) = oracle(conn, "p.id = %s", (target,))
        check(status == 200 and shape(data) == shape(row), "detail data")
        check(
            sum(c for _, c in sql) == 1,
            f"detail issued {sum(c for _, c in sql)} statements, expected 1",
        )
        report += ["detail:", *(f"  {c}x {' '.join(q.split())[:150]}" for q, c in sql)]

        check(
            call(base, "GET", f"/posts/{post_id(0)}")[0] == 404,
            "unpublished post is 404",
        )
        check(
            call(base, "GET", f"/posts/{uuid.uuid4()}")[0] == 404, "missing post is 404"
        )
        check(call(base, "GET", "/posts?page=0")[0] == 422, "page=0 is 422")
        check(
            call(base, "GET", "/posts?page_size=101")[0] == 422, "page_size=101 is 422"
        )

        good = {
            "author_id": str(author_id(3)),
            "title": "verify-new",
            "body": "Created by verify.",
        }
        status, data, sql = counted("POST", "/posts", good)
        check(status == 201, f"create status {status}")
        if status in {200, 201}:
            check(
                data["title"] == "verify-new"
                and data["author"]["name"] == "Author 003"
                and data["published"] is True,
                "create data",
            )
            stored = oracle(conn, "p.id = %s", (data["id"],))
            check(
                len(stored) == 1 and shape(stored[0])["created_at"] is not None,
                "create stored",
            )
        check(
            sum(c for _, c in sql) == 2,
            f"create issued {sum(c for _, c in sql)} statements, expected 2 (INSERT + author SELECT)",
        )
        report += ["create:", *(f"  {c}x {' '.join(q.split())[:150]}" for q, c in sql)]
        conn.execute("DELETE FROM posts WHERE title = 'verify-new'")

        for bad in (
            {**good, "title": ""},
            {**good, "title": "x" * 121},
            {**good, "body": ""},
            {"title": "no author", "body": "x"},
        ):
            check(
                call(base, "POST", "/posts", bad)[0] == 422,
                f"invalid body {list(bad)} is 422",
            )

    print("\n".join(report))
    if problems:
        print("FAILED:", *problems, sep="\n  ")
        sys.exit(1)
    print("OK: behaviour and statement counts match the oracle")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
