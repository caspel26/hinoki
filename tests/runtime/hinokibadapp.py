"""Application with an invalid handler signature (path parameter mismatch)."""

from hinoki import HinokiAPI, get


@get("/users/{id}")
def user(user_id: int) -> None: ...


app = HinokiAPI()
app.include(user)
