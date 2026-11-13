# Pagination

An action opts into page-number pagination with `pagination=PageNumberPagination(...)`. The framework parses and validates `page` and `page_size`, injects a `PageParams` value, and documents both query parameters in OpenAPI.

```python
@get("/tasks", pagination=PageNumberPagination(page_size=20, max_page_size=100))
async def list_tasks(page: PageParams) -> Page[TaskOut]:
    rows = await Task.all().apaginate(page)
    return rows.map(TaskOut.from_model)
```

Pages are one-based. `page_size` defaults to the strategy's configured size and cannot exceed its maximum. Invalid values produce a validation response before the handler runs.

`Page` contains `items`, `page`, `page_size`, `total` and the computed `pages`. Empty result sets have zero pages; a page beyond the end has an empty `items` list. `Page.map()` converts each item while preserving the counts and requested page.

Use the queryset's `apaginate`/`paginate` methods for database rows. `Page.of(rows, params)` slices an in-memory sequence and counts that sequence; it is not a substitute for database pagination on large tables.
