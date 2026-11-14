"""Two APIs inheriting one unprotected action, for the identity regression."""

from hinoki import API, Hinoki, Request, http


class Base(API):
    @http.delete("/{item_id}")
    def remove(self, request: Request, item_id: int) -> None: ...

    @http.get("/open")
    def untyped(self, request: Request):  # type: ignore[no-untyped-def]
        return {}


class Posts(Base, path="/posts"):
    pass


class Comments(Base, path="/comments"):
    pass


def create_app() -> Hinoki:
    app = Hinoki()
    app.include(Posts, Comments)
    return app
