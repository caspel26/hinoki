"""Two APIs inheriting one unprotected action, for the identity regression."""

from hinoki import API, HinokiAPI, Request, delete, get


class Base(API):
    @delete("/{item_id}")
    def remove(self, request: Request, item_id: int) -> None: ...

    @get("/open")
    def untyped(self, request: Request):  # type: ignore[no-untyped-def]
        return {}


class Posts(Base, path="/posts"):
    pass


class Comments(Base, path="/comments"):
    pass


def create_app() -> HinokiAPI:
    app = HinokiAPI()
    app.include(Posts, Comments)
    return app
