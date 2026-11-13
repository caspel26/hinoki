# 6. Run and test it

## Settings and serving

The app is built by a factory that loads its typed settings ([settings guide](../SETTINGS.md)) and installs the database module:

```python
def create_app(database_url: str | None = None) -> HinokiAPI:
    app = HinokiAPI(title="Tracker", openapi_url="/openapi.json", docs_url="/docs",
                    redoc_url="/redoc", rules=tracker_rules)
    app.include(api.health, api.list_tasks, api.create_task, api.complete_task, api.delete_task)
    app.install(DatabaseModule(database_url or os.environ["TRACKER_DATABASE_URL"]))
    return app
```

```sh
TRACKER_DATABASE_URL=postgres://... python -m hinoki dev examples.tracker.app:create_app
```

`dev` reloads on change; `serve` runs without reload and takes host, port and workers from the `server` settings ([serving](../SERVING.md)). Open `/docs` or `/redoc`.

## Test it

Operations need no HTTP at all, so test them directly; then drive the whole stack with the in-process client:

```python
async with ASGIClient(create_app(URL), strict_errors=True) as client:
    made = await client.post("/tasks", json={"title": "x"}, token="ann-token")
    path = f"/tasks/{made.body['id']}"
    assert (await client.post(f"{path}/complete", token="ben-token")).status == 404
    assert (await client.post(f"{path}/complete", token="ann-token")).status == 200
    assert (await client.post(f"{path}/complete", token="ann-token")).status == 409
```

`strict_errors=True` fails the test if an action answers an error it did not declare. For tests without `async`, use `SyncClient`.

## Where next

[Patterns and best practices](../PATTERNS.md), [the application MCP](../APP-MCP-DESIGN.md) (the tracker already exposes `list_tasks` to agents), and [building with an AI agent](../AI-WORKFLOW.md).
