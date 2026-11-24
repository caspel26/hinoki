"""Deterministic data of the benchmark service, shared by the seed, apps and verifier.

The mode follows the routes, table shapes and row counts of Django Bolt's published
comparison with Litestar and FastAPI (python-api-frameworks-benchmark). That
repository publishes no licence, so nothing is copied from it: the rows and the two
JSON bodies are generated here, with the same counts, field names and body sizes.

* 10 ``benchmark_users``, 50 authors, 100 tags, 500 articles (80 % published,
  2 to 5 tags each), 2 000 comments, one ``auth_user`` (id 1).
* ``json-1k`` is a list of 1 "person" record of 1 171 bytes and ``json-10k`` a list
  of 10 records of 10 864 bytes, compact and ASCII. Handlers return the Python
  objects, so each framework's own JSON encoder writes the body.
* Every service reads the same HS256 JWT from the ``access_token`` cookie.
"""

import json
import time
from datetime import UTC, datetime, timedelta

import jwt

USERS = 10
AUTHORS = 50
TAGS = 100
ARTICLES = 500
COMMENTS = 2_000
AUTH_USER_ID = 1
START = datetime(2026, 1, 1, tzinfo=UTC)

COOKIE = "access_token"
SECRET = "hinoki-bolt-parity-benchmark-secret-not-for-production"
ALGORITHM = "HS256"
TOKEN_TTL = 24 * 3600

PAYLOAD_BYTES = {"json-1k": 1171, "json-10k": 10864}
PAYLOAD_RECORDS = {"json-1k": 1, "json-10k": 10}

WORDS = (
    "amber", "basin", "cedar", "delta", "ember", "fjord", "grove", "harbor", "inlet",
    "juniper", "kestrel", "lagoon", "meadow", "nectar", "orchard", "pebble", "quartz",
    "ridge", "summit", "thicket", "upland", "valley", "willow", "yarrow", "zephyr",
)  # fmt: skip
FIRST_NAMES = ("Ada", "Bruno", "Clara", "Dario", "Elena", "Franco", "Giulia", "Hugo")
LAST_NAMES = ("Rossi", "Bianchi", "Moreau", "Novak", "Silva", "Tanaka", "Weber")
COLOURS = ("blue", "brown", "green")
FRUITS = ("apple", "banana", "strawberry")


def text(seed: int, length: int) -> str:
    """Deterministic words, cut to exactly ``length`` characters, ending in a dot."""
    words: list[str] = []
    size = 0
    index = seed
    while size < length:
        word = WORDS[index * 7 % len(WORDS)]
        words.append(word)
        size += len(word) + 1
        index += 1
    body = " ".join(words)[: length - 1]
    if body.endswith(" "):
        body = body[:-1] + "s"
    return (body + ".").capitalize()


def person_name(n: int) -> str:
    return f"{FIRST_NAMES[n % len(FIRST_NAMES)]} {LAST_NAMES[n % len(LAST_NAMES)]}"


def moment(n: int, step: timedelta = timedelta(minutes=1)) -> datetime:
    """A unique timestamp per row with a sub-second part, in whole milliseconds.

    Whole milliseconds keep the stored value exact in JavaScript ``Date`` too, so
    every stack can return the same instant.
    """
    return START + step * n + timedelta(milliseconds=1 + n)


def users() -> list[tuple]:
    return [
        (n, f"user{n - 1:02d}", f"user{n - 1:02d}@example.com", f"First{n - 1}",
         f"Last{n - 1}", True)
        for n in range(1, USERS + 1)
    ]  # fmt: skip


def authors() -> list[tuple]:
    return [
        (n, person_name(n), f"author{n:02d}@example.com", text(n, 180), moment(n))
        for n in range(1, AUTHORS + 1)
    ]


def tags() -> list[tuple]:
    return [(n, f"Tag {n:03d}", f"tag-{n:03d}") for n in range(1, TAGS + 1)]


def articles() -> list[tuple]:
    rows = []
    for n in range(1, ARTICLES + 1):
        created = moment(n, timedelta(hours=1))
        rows.append(
            (n, text(n, 45), text(n * 3, 1900), n % 5 != 0,
             (n * 7) % AUTHORS + 1, created, created + timedelta(minutes=5))
        )  # fmt: skip
    return rows


def article_tags() -> list[tuple]:
    """2 to 5 distinct tags per article."""
    rows = []
    for article in range(1, ARTICLES + 1):
        for k in range(2 + article % 4):
            rows.append((len(rows) + 1, article, (article * 13 + k * 17) % TAGS + 1))
    return rows


def comments() -> list[tuple]:
    return [
        (n, (n * 11) % ARTICLES + 1, person_name(n + 3), text(n * 5, 450),
         moment(n, timedelta(minutes=7)))
        for n in range(1, COMMENTS + 1)
    ]  # fmt: skip


def auth_user() -> tuple:
    """Django ``auth_user`` columns; the password is unusable (no login route)."""
    return (AUTH_USER_ID, "!benchmark-no-login", None, False, "benchuser",
            "Bench", "User", "benchuser@example.com", False, True, START)  # fmt: skip


def person(index: int) -> dict:
    """One record shaped like the generated "person" fixtures; ``about`` is padding."""
    name = person_name(index)
    return {
        "_id": f"{(index + 1) * 2654435761 % 16**24:024x}",
        "index": index,
        "guid": f"{index + 1:08x}-0000-4000-8000-{(index + 1) * 977:012x}",
        "isActive": index % 2 == 0,
        "balance": f"${1000 + index * 137 % 3000:,}.{index * 7 % 100:02d}",
        "picture": "http://placehold.it/32x32",
        "age": 20 + index * 3 % 40,
        "eyeColor": COLOURS[index % len(COLOURS)],
        "name": name,
        "gender": "female" if index % 2 else "male",
        "company": WORDS[index % len(WORDS)].upper(),
        "email": f"{name.replace(' ', '').lower()}@example.com",
        "phone": f"+1 (800) 555-{1000 + index:04d}",
        "address": f"{100 + index} {WORDS[index * 3 % len(WORDS)].title()} Street",
        "about": "",
        "registered": f"2015-{1 + index % 12:02d}-11T05:34:25 -01:00",
        "latitude": round(-14.326509 + index * 1.5, 6),
        "longitude": round(-32.417451 + index * 2.25, 6),
        "tags": [WORDS[(index + k) % len(WORDS)] for k in range(7)],
        "friends": [{"id": k, "name": person_name(index + k + 1)} for k in range(3)],
        "greeting": f"Hello, {name}! You have {index + 3} unread messages.",
        "favoriteFruit": FRUITS[index % len(FRUITS)],
    }


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def people(count: int, size: int) -> list[dict]:
    """``count`` records whose compact encoding is exactly ``size`` bytes."""
    records = [person(index) for index in range(count)]
    spare = size - len(encode(records))
    if spare < count:
        raise ValueError(f"{count} records do not fit in {size} bytes")
    for index, record in enumerate(records):
        share = spare // count + (1 if index < spare % count else 0)
        record["about"] = text(index * 9, share)
    return records


PAYLOADS = {
    name: people(PAYLOAD_RECORDS[name], size) for name, size in PAYLOAD_BYTES.items()
}
BODIES = {name: encode(value) for name, value in PAYLOADS.items()}


def mint_token(subject: int = AUTH_USER_ID, now: float | None = None) -> str:
    """The one access token every service accepts: HS256, ``sub``, ``iat``, ``exp``."""
    issued = int(time.time() if now is None else now)
    claims = {"sub": str(subject), "iat": issued, "exp": issued + TOKEN_TTL}
    return jwt.encode(claims, SECRET, algorithm=ALGORITHM)
