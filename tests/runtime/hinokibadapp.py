"""Application with an invalid handler signature (path parameter mismatch)."""

from hinoki import HinokiAPI, get
from hinoki.request import Request


@get("/users/{id}")
def user(request: Request, user_id: int) -> None: ...


app = HinokiAPI()
app.include(user)
