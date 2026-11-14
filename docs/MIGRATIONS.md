---
status: implemented
related:
  - MODELS.md
  - CLI.md
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
hinoki migrations --dir examples/blog/migrations status --database "$BLOG_DATABASE__URL"
hinoki migrations --dir examples/blog/migrations sql 0001-initial
hinoki migrations --dir examples/blog/migrations apply --database "$BLOG_DATABASE__URL"
```

`status` and `apply` connect to PostgreSQL. Pass `--database` or set `HINOKI_DATABASE_URL`; use a disposable database to review a migration before running it against important data.

## Destructive changes

Operations that can remove data are reported as destructive and are not written unless `--allow-destructive` is passed to `plan`. Read the generated migration before opting in. Some changes, such as changing a constraint on an existing column, require a hand-written migration instead of an automatic plan.

Migrations are files committed with the application. Do not run them from every server worker's startup hook. For the connection lifecycle, see [settings](SETTINGS.md#8-database-connection-ownership).
