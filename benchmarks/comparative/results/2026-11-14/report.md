# Measured results

Run on 2026-11-14 16:15:49 +0100. Intel(R) Core(TM) i5-10300H CPU @ 2.50GHz, 8 logical CPUs, Linux-7.2.11-arch1-1-x86_64-with-glibc2.44.
Load average when the run started: 2.87, 2.23, 2.09.
4 server processes pinned to CPUs 0-3, load generator on CPUs 6-7, 64 concurrent connections, 10 s warm-up, 3 measured runs of 20 s per scenario. postgres (PostgreSQL) 17.12 (Debian 17.12-1.pgdg13+1); oha 1.17.0.

## health

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `200` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 41,014 | 39,489 – 41,941 | 1.5 | 2.9 | 3.8 | 0 |
| FastAPI | 17,592 | 17,488 – 18,026 | 3.5 | 5.2 | 6.5 | 0 |
| Django Ninja | 4,408 | 4,369 – 4,474 | 12.9 | 22.0 | 27.3 | 0 |
| Django Bolt | 63,500 | 62,453 – 63,652 | 0.9 | 1.4 | 1.6 | 0 |

## list

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `200` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 1,484 | 1,151 – 1,544 | 43.4 | 64.0 | 83.7 | 0 |
| FastAPI | 1,325 | 1,304 – 1,327 | 31.8 | 128.7 | 204.1 | 0 |
| Django Ninja | 906 | 905 – 922 | 67.1 | 99.8 | 110.5 | 0 |
| Django Bolt | 1,563 | 1,550 – 1,601 | 39.3 | 62.1 | 73.7 | 0 |

## detail

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `200` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 9,211 | 9,197 – 9,240 | 6.7 | 10.4 | 12.6 | 0 |
| FastAPI | 2,742 | 2,724 – 2,785 | 21.8 | 55.8 | 73.1 | 0 |
| Django Ninja | 1,729 | 1,707 – 1,733 | 36.0 | 54.7 | 63.4 | 0 |
| Django Bolt | 4,551 | 4,541 – 4,662 | 13.3 | 22.4 | 25.7 | 0 |

## create

| Framework | Requests/s (median of runs) | Range across runs | p50 ms | p95 ms | p99 ms | Responses not `201` |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Hinoki | 2,732 | 1,866 – 3,001 | 17.2 | 25.5 | 76.9 | 0 |
| FastAPI | 1,761 | 1,759 – 1,784 | 34.5 | 61.4 | 83.4 | 0 |
| Django Ninja | 981 | 764 – 1,120 | 46.4 | 82.8 | 849.8 | 0 |
| Django Bolt | 1,406 | 1,246 – 1,996 | 28.2 | 41.7 | 65.1 | 0 |

Requests still in flight when a run ends are aborted by the load generator (64 per run) and are not counted as errors.

## Statements per request (from the verification step)

### Hinoki

```text
== hinoki
list:
  1x SELECT count(*) FROM "posts" LEFT JOIN "authors" AS "rel_author" ON "rel_author"."id" = "posts"."author_id" WHERE "posts"."published" = $1
  1x SELECT "posts"."id", "posts"."author_id", "posts"."title", "posts"."body", "posts"."published", "posts"."created_at", "rel_author"."id" AS "rel_author
detail:
  1x SELECT "posts"."id", "posts"."author_id", "posts"."title", "posts"."body", "posts"."published", "posts"."created_at", "rel_author"."id" AS "rel_author
create:
  1x INSERT INTO "posts" ("id", "author_id", "title", "body", "published", "created_at") VALUES ($1, $2, $3, $4, $5, $6) RETURNING "id", "author_id", "titl
  1x SELECT "id", "name" FROM "authors" WHERE "id" = $1 LIMIT 2
OK: behaviour and statement counts match the oracle
```

### FastAPI

```text
== fastapi
list:
  1x SELECT count(*) AS count_1 FROM posts WHERE posts.published IS true
  1x SELECT posts.id, posts.author_id, posts.title, posts.body, posts.published, posts.created_at, authors_1.id AS id_1, authors_1.name FROM posts LEFT OUT
detail:
  1x SELECT posts.id, posts.author_id, posts.title, posts.body, posts.published, posts.created_at, authors_1.id AS id_1, authors_1.name FROM posts LEFT OUT
create:
  1x INSERT INTO posts (id, author_id, title, body, published, created_at) VALUES ($1::UUID, $2::UUID, $3::VARCHAR, $4::VARCHAR, $5::BOOLEAN, $6::TIMESTAMP
  1x SELECT authors.id, authors.name FROM authors WHERE authors.id = $1::UUID
OK: behaviour and statement counts match the oracle
```

### Django Ninja

```text
== ninja
list:
  1x SELECT COUNT(*) AS "__count" FROM "posts" WHERE "posts"."published"
  1x SELECT "posts"."id", "posts"."author_id", "posts"."title", "posts"."body", "posts"."published", "posts"."created_at", "authors"."id", "authors"."name"
detail:
  1x SELECT "posts"."id", "posts"."author_id", "posts"."title", "posts"."body", "posts"."published", "posts"."created_at", "authors"."id", "authors"."name"
create:
  1x INSERT INTO "posts" ("id", "author_id", "title", "body", "published", "created_at") VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::timestamptz)
  1x SELECT "authors"."id", "authors"."name" FROM "authors" WHERE "authors"."id" = $1::uuid LIMIT $2
OK: behaviour and statement counts match the oracle
```

### Django Bolt

```text
== bolt
list:
  1x SELECT COUNT(*) AS "__count" FROM "posts" WHERE "posts"."published"
  1x SELECT "posts"."id", "posts"."author_id", "posts"."title", "posts"."body", "posts"."published", "posts"."created_at", "authors"."id", "authors"."name"
detail:
  1x SELECT "posts"."id", "posts"."author_id", "posts"."title", "posts"."body", "posts"."published", "posts"."created_at", "authors"."id", "authors"."name"
create:
  1x INSERT INTO "posts" ("id", "author_id", "title", "body", "published", "created_at") VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::timestamptz)
  1x SELECT "authors"."id", "authors"."name" FROM "authors" WHERE "authors"."id" = $1::uuid LIMIT $2
OK: behaviour and statement counts match the oracle
```
