---
status: implemented
related:
  - models.md
  - cli.md
---

# Migrations

Hinoki compares the models in a module with the migration state recorded in files. It can write a migration plan, but never applies schema changes when an application starts.

## Create a migration

```sh
hinoki migrations --dir examples/blog/migrations plan \
  --models examples.blog.models --name add-published
```

The planner writes a numbered `.sql` file and a matching `.state.json` file. Review both before applying the change. To inspect a plan without writing files, add `--dry-run`.

## Apply and inspect

```sh
export HINOKI_DATABASE_URL="$BLOG_DATABASE__URL"
hinoki migrations --dir examples/blog/migrations status
hinoki migrations --dir examples/blog/migrations sql 0001-initial
hinoki migrations --dir examples/blog/migrations apply
```

`status` and `apply` connect to PostgreSQL. Set `HINOKI_DATABASE_URL`; `--database URL` overrides it, but an argument is visible in the process list and the shell history, so do not pass a URL that contains a password that way. A failed connection is reported as `error: database: …` with exit code 1, without the URL.

Use a disposable database to review a migration before running it against important data.

## Destructive changes

Operations that can remove data are reported as destructive and are not written unless `--allow-destructive` is passed to `plan`. Read the generated migration before opting in. Changing a constraint on an existing column may require a hand-written migration.

Migrations are files committed with the application. Do not run them from every server worker's startup hook. For the connection lifecycle, see [settings](settings.md#8-database-connection-ownership).
