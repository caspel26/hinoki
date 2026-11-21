"""Fixed JSON bodies for the ``json-1k`` and ``json-10k`` scenarios.

The bytes are built once, deterministically, with compact separators, and every
implementation returns exactly these bytes with ``application/json``. The
scenarios therefore compare the request and response path of each stack for a
body of a given size, not the speed of each stack's JSON serializer.
"""

import json

CATEGORIES = ("books", "garden", "kitchen", "music", "outdoor", "toys")
TARGET_BYTES = {"json-1k": 1024, "json-10k": 10240}


def make_items(count: int) -> list[dict]:
    return [
        {
            "id": number,
            "name": f"Item {number:04d}",
            "description": f"Deterministic sample item number {number}.",
            "price_cents": 199 + number * 37 % 9000,
            "category": CATEGORIES[number % len(CATEGORIES)],
            "in_stock": number % 3 != 0,
        }
        for number in range(1, count + 1)
    ]


def serialize(items: list[dict]) -> bytes:
    return json.dumps(items, separators=(",", ":")).encode()


def items_for(target: int) -> list[dict]:
    """The longest list whose body does not exceed ``target`` bytes."""
    count = 1
    while len(serialize(make_items(count + 1))) <= target:
        count += 1
    return make_items(count)


ITEMS = {name: items_for(size) for name, size in TARGET_BYTES.items()}
BODIES = {name: serialize(items) for name, items in ITEMS.items()}
BODY_1K = BODIES["json-1k"]
BODY_10K = BODIES["json-10k"]
