---
status: implemented
---

# Pagination

An action opts into page-number pagination with `pagination=PageNumberPagination()`. A size you leave out comes from the application's `pagination` settings group; without settings the defaults are `page_size=20` and `max_page_size=100`. The framework parses and validates `page` and `page_size`, injects a `PageParams` value, and documents both query parameters in OpenAPI.

```python
@http.get("/tasks", pagination=PageNumberPagination())
async def list_tasks(page: PageParams) -> Page[TaskOut]:
    rows = await Task.all().apaginate(page)
    return rows.map(TaskOut.from_model)
```

Pages are one-based. `page_size` defaults to the strategy's configured size and cannot exceed its maximum. Invalid values produce a validation response before the handler runs; `page` is limited to 1 000 000 000, and longer or larger values answer `422` with the code `le`.

`Page` contains `items`, `page`, `page_size`, `total` and the computed `pages`. Empty result sets have zero pages; a page beyond the end has an empty `items` list. `Page.map()` converts each item while preserving the counts and requested page.

## Defaults for the whole application

`settings.AppSettings` has a `pagination` group, set in code, in the environment (`BLOG_PAGINATION__PAGE_SIZE=25`, `BLOG_PAGINATION__MAX_PAGE_SIZE=200`) or in TOML:

```python
class BlogSettings(settings.AppSettings, prefix="BLOG_"):
    pass

# BLOG_PAGINATION__PAGE_SIZE=25  BLOG_PAGINATION__MAX_PAGE_SIZE=200
app = Hinoki(settings=BlogSettings, include=[tasks])
```

Precedence, from strongest to weakest: a size set on the action's `PageNumberPagination(...)`, the application's `pagination` settings, then 20 and 100. Each size is resolved separately, so `PageNumberPagination(page_size=10)` keeps its 10 and takes its maximum from the settings. The result must satisfy `1 <= page_size <= max_page_size`: invalid settings fail when they load, and an action whose own size does not fit the resolved other one (for example `PageNumberPagination(max_page_size=10)` under a settings `page_size` of 25) fails when it is included, naming the action. The generated OpenAPI parameters show the resolved values.

Use the queryset's `apaginate`/`paginate` methods for database rows. `Page.of(rows, params)` slices an in-memory sequence and counts that sequence; it is not a substitute for database pagination on large tables.
