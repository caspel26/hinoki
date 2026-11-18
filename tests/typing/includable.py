"""What the type checker lets a project list for ``include``."""

from hinoki import API, Hinoki, Includable, Mount, RouteGroup, http


@http.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class Items(API, path="/items"):
    @http.get("/")
    def listing(self) -> list[str]:
        return []


group = RouteGroup("/tasks")
routes: list[Includable] = [health, Items, group, [health, (Items,)]]
app = Hinoki(include=[*routes, Mount("/api", routes)])
app.include(health, Mount("/more", Items))

bad: list[Includable] = [health, 3]  # type: ignore[list-item]
