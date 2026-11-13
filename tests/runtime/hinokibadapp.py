"""Application with an invalid handler signature (path parameter mismatch)."""

from hinoki import Hinoki, get
from hinoki.request import Request


@get("/users/{id}")
def user(request: Request, user_id: int) -> None: ...


app = Hinoki()
app.include(user)
