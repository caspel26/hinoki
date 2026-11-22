"""The Notes API through the whole stack: no network, a database per test."""

from hinoki.testing import ASGIClient


async def test_a_note_is_created_read_updated_and_deleted(client: ASGIClient) -> None:
    made = await client.post("/notes", json={"title": "  First  ", "body": "Hello"})
    assert made.status == 201
    assert made.body["title"] == "First"  # Field(strip=True)
    path = f"/notes/{made.body['id']}"

    assert (await client.get(path)).body == made.body
    patched = await client.patch(path, json={"body": "Hello again"})
    assert (patched.body["title"], patched.body["body"]) == ("First", "Hello again")

    assert (await client.delete(path)).status == 204
    assert (await client.get(path)).status == 404


async def test_the_list_is_paginated_and_ordered_by_title(client: ASGIClient) -> None:
    for title in ("Banana", "Apple", "Cherry"):
        await client.post("/notes", json={"title": title})

    listing = await client.get("/notes", query="page_size=2")

    assert [note["title"] for note in listing.body["items"]] == ["Apple", "Banana"]


async def test_invalid_input_is_refused_before_the_database(
    client: ASGIClient,
) -> None:
    empty = await client.post("/notes", json={"title": ""})
    unknown = await client.post("/notes", json={"title": "x", "id": "set-by-me"})

    assert (empty.status, unknown.status) == (422, 422)
    assert (await client.get("/notes")).body["items"] == []


async def test_the_openapi_document_describes_the_generated_actions(
    client: ASGIClient,
) -> None:
    document = (await client.get("/openapi.json")).body

    assert set(document["paths"]) == {"/notes", "/notes/{item_id}"}
