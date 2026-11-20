---
status: implemented
---

# Pagination

An action opts into page-number pagination with `pagination=PageNumberPagination()`. A size you leave out comes from the application's `pagination` settings group; without settings the defaults are `page_size=20` and `max_page_size=100`. The framework parses and validates `page` and `page_size`, injects a `PageParams` value, and documents both query parameters in OpenAPI.

The Billing app lists invoices a page at a time. The handler asks the model for one page, and `Page.map()` turns its rows into schemas:

```python
# notes/apps/billing/api.py
from hinoki import RouteGroup
from hinoki.pagination import Page, PageNumberPagination, PageParams

from notes.security import billing_staff

from .models import Invoice
from .schemas import InvoiceOut

invoices = RouteGroup("/invoices", access=billing_staff, atomic=True)


@invoices.get("/", pagination=PageNumberPagination())
async def list_invoices(page: PageParams) -> Page[InvoiceOut]:
    return (await Invoice.all().apaginate(page)).map(InvoiceOut.from_model)
```

Pages are one-based. `page_size` defaults to the strategy's configured size and cannot exceed its maximum. Invalid values produce a validation response before the handler runs; `page` is limited to 1 000 000 000, and longer or larger values answer `422` with the code `le`.

`Page` contains `items`, `page`, `page_size`, `total` and the computed `pages`. Empty result sets have zero pages; a page beyond the end has an empty `items` list. `Page.map()` converts each item while preserving the counts and requested page.

## Defaults for the whole application

`settings.AppSettings` has a `pagination` group, set in code, in the environment or in TOML. With the project's settings class:

```python
# notes/settings.py
from hinoki import settings


class ProjectSettings(settings.AppSettings, prefix="NOTES_"):
    pass
```

```sh
export NOTES_PAGINATION__PAGE_SIZE=25
export NOTES_PAGINATION__MAX_PAGE_SIZE=200
```

The application in `notes/main.py` reads them through `Hinoki(settings=ProjectSettings, ...)`.

Precedence, from strongest to weakest: a size set on the action's `PageNumberPagination(...)`, the application's `pagination` settings, then 20 and 100. Each size is resolved separately, so `PageNumberPagination(page_size=10)` keeps its 10 and takes its maximum from the settings. The result must satisfy `1 <= page_size <= max_page_size`: invalid settings fail when they load, and an action whose own size does not fit the resolved other one (for example `PageNumberPagination(max_page_size=10)` under a settings `page_size` of 25) fails when it is included, naming the action. The generated OpenAPI parameters show the resolved values.

Use the queryset's `apaginate`/`paginate` methods for database rows. They append the primary key as the last sort key (after any `order_by`), so rows with equal sort values keep one position across pages. `Page.of(rows, params)` slices an in-memory sequence and counts that sequence; it is not a substitute for database pagination on large tables.
