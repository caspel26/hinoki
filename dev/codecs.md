# Parsers and renderers

A **parser** turns a request body into Python data; a **renderer** turns a handler's result into response bytes. Built-ins use Rust for selected parsing and JSON encoding work; Python still selects codecs, calls customization hooks and assembles framework objects.

| Built in | Rust work | Python work and options |
| --- | --- | --- |
| `JSONParser` | Strict JSON decoding (no NaN, no lone surrogates, depth limit) | `prepare(data)` hook and `media_type` |
| `Form[Schema]` marker | A `Schema` read from form text, converted per field | Rust conversion, then strict `Schema` validation |
| `XMLParser` | `application/xml` into a tree (children → `dict`, repeated name → `list`, text → `str`, empty → `None`), parsed with the GIL released; no DTDs, custom entities, characters XML 1.0 forbids or encodings other than UTF-8; depth and element caps | Rust; `max_depth` (default 32, at most `MAX_XML_DEPTH` = 1000, larger raises `ValueError`), `max_elements` (default 10,000); attributes are not read |
| `XML[Schema]` marker | A `Schema` read from an XML body, converted per field | Rust conversion, then strict `Schema` validation |
| `Query[Schema]` marker | A `Schema` read from the query string, converted per field | Rust conversion, then strict `Schema` validation |
| `FormParser` | `application/x-www-form-urlencoded` parsing with a field cap | Builds the Python mappings; `max_fields` |
| `MultiPartParser` | `multipart/form-data` parsing and byte/part limits | Builds `UploadedFile` and form objects; `max_parts`, `max_part_bytes` (each part, including text fields) |
| `TextParser` | — | Decodes UTF-8 in Python; `media_type` |
| `BytesParser` | — | Hands `request.data` the raw `bytes` of an `application/octet-stream` body |
| `JSONRenderer` | Encodes `dict`, `list`, `Schema`, UUID, datetime and `dump()` objects | `prepare(value, request)` and `default` callbacks run in Python; `indent`, `sort_keys`, `media_type` |
| `HTMLRenderer`, `TextRenderer` | — | Type-check and UTF-8 encode strings in Python; `media_type` |
| `XMLRenderer` | Encodes what `JSONRenderer` encodes (`dict`, `list`, `Schema`, UUID, datetime, `dump()` objects) as UTF-8 XML, with the GIL held; ASCII element names, U+FFFD for characters XML 1.0 forbids, depth limit 200 | `root`, `item` |

Content negotiation (`Accept`, quality values, wildcards) also runs in Rust; with nothing acceptable the answer is 406 before the handler runs.

## Upload files

An action that binds an `UploadedFile` and has no parsers of its own gets `MultiPartParser()` with its default limits (100 parts, 1 MiB per part), unless the application's `parsers=` already include a `MultiPartParser`, which is then used. Other actions accept multipart only when it is listed. The parser handles bounded multipart bytes in Rust, then Python creates `UploadedFile` objects. For typed handler binding, annotate a parameter as `UploadedFile`, `UploadedFile | None = None`, or `list[UploadedFile]`; the parameter name selects the multipart field. A single value rejects duplicates, while a list preserves repeated values. The same objects are visible through `request.FILES`; text fields are available in `request.POST`. See [endpoint upload declarations](https://hinoki.caspel.dev/dev/endpoints.md#upload-files) for a complete example and status-code behavior.

The complete request body and every part are buffered in memory. The default body cap is 1 MiB; increasing it also increases potential memory use across concurrent requests. This parser does not stream or spool files. Keep both the runtime body cap and `max_part_bytes` bounded, and treat filenames and media types as untrusted client input.

`JSONParser`, `JSONRenderer`, `HTMLRenderer`, `XMLParser`, `XMLRenderer`, `FormParser`, `MultiPartParser` and `BytesParser` are importable from `hinoki`; `TextParser` and `TextRenderer` come from `hinoki.parsers` and `hinoki.renderers`.

## Choose them

Per action, or for the whole application (an action's own choice wins):

```python
# notes/main.py
from hinoki import Hinoki, JSONRenderer
from hinoki.renderers import TextRenderer

from notes.rules import project_rules
from notes.settings import ProjectSettings

app = Hinoki(
    settings=ProjectSettings,
    rules=project_rules,
    renderers=[JSONRenderer(sort_keys=True), TextRenderer()],
)
```

```python
# notes/apps/billing/api.py (continued)
from hinoki import JSONParser, JSONRenderer, Request
from hinoki.parsers import TextParser


@invoices.get("/report", renderers=[JSONRenderer(indent=2)])
async def report() -> InvoiceReport: ...


@invoices.post("/hook", parsers=[JSONParser(), TextParser()], atomic=True)
async def hook(request: Request) -> HookReceipt: ...  # request.data is a dict or a str
```

## Answer XML

`XMLRenderer` renders the handler's result as `application/xml; charset=utf-8` with an XML declaration. It is written by the Rust encoder, which accepts what the JSON encoder accepts: `dict`, `list`, `tuple`, `str`, `int`, `float`, `bool`, `None`, `Schema` instances, UUIDs, datetimes and `dump()` objects, at any depth. `date`, `time`, `timedelta`, `Decimal`, `bytes`, sets and frozensets, IP addresses and networks, `PurePath`, `Secret` (written masked) and enum members are written in their wire form at any depth, as in `JSONRenderer`: a `Decimal` as its text, `bytes` as base64, a set as a sorted list, an enum member as its value. A `JSONRenderer`'s own options (`default`, `prepare`, `sort_keys`) do not apply to it:

```python
from hinoki import JSONRenderer, XMLRenderer


@http.get("/report", renderers=[JSONRenderer(), XMLRenderer(root="report")])
async def report() -> dict[str, object]:
    return {"id": 1, "tags": ["a", "b"], "note": None}
```

`Accept: application/xml` answers `<?xml version="1.0" encoding="UTF-8"?><report><id>1</id><tags><item>a</item><item>b</item></tags><note/></report>`; without it the first renderer (JSON) answers. A mapping becomes child elements named by its keys, a list becomes repeated `item` elements (`item=` renames them), `None` an empty element and booleans `true`/`false`. Keys are spelled as the JSON encoder spells them: a string as is, an integer in decimal, `None` as `null` and booleans as `true`/`false`; any other key (a float, a UUID, a tuple) raises an error (`500`), as it does for JSON. A key that is not a valid XML name (a space, a leading digit, a name starting with `xml`, so also an integer key) is written as `<item key="...">`, escaped, instead of failing the request. Tab and line breaks are written as character references so a parser keeps them, and characters XML 1.0 cannot carry are replaced by U+FFFD. Non-finite floats, which JSON refuses, are written `NaN`, `INF` and `-INF`; finite floats are spelled as the JSON encoder spells them (`1e-7`, `1e+16`). Element names are ASCII (`[A-Za-z_][A-Za-z0-9_.-]*`, not starting with `xml` in any case, no colon), so `root=` and `item=` that do not match raise `ValueError` when the renderer is created. Nesting deeper than 200 levels (the JSON encoder's limit; the reading limit is separate: `XMLParser(max_depth=)`, default 32, at most 1000), an integer of more than 4300 digits or a value it cannot write raises an error (`500`) whose message names the type or the limit. The encoder holds the GIL, as the JSON encoder does, because it reads Python objects as it writes; it never reads the request, so an action that lists the renderer stays on the native handler lane.

### What a round trip keeps

`XMLParser` is not the inverse of `XMLRenderer`. Reading back what was written, with `XMLParser` or `XML[Schema]`:

| Written | Read back |
| --- | --- |
| `None`, `""`, `{}` and `[]` (all an empty element) | `None` |
| `1`, `1.5`, `true` | the strings `"1"`, `"1.5"` and `"true"` (use `XML[Schema]` to convert per field) |
| a list of one item | `{"item": "1"}` (a mapping with the item name as its key) |
| a list of two or more items | `{"item": ["1", "2"]}` |
| a key written as `<item key="...">` | the key `item` |
| the root element's name | lost |
| a character XML 1.0 cannot carry | U+FFFD |

Attributes and namespace prefixes are ignored on reading, and mixed text and elements are refused. A document of more than 32 levels needs `XMLParser(max_depth=...)`. Use XML for interchange, not to store Python values.

## Configure the Rust JSON encoder

```python
JSONRenderer(indent=2, sort_keys=True)  # pretty, stable output
JSONRenderer(default=str)  # values outside a Schema the hook sees first
```

`default` is a Python callback for a value the encoder does not know and must return something that is JSON (returning the same object is an error). It does not override what the encoder always handles (UUID, datetime, `Schema`, anything with `dump()`). Without `default`, `date`, `time`, `timedelta`, `Decimal`, `bytes`, sets, IP addresses, paths, `Secret` and enum members are written in their wire form anywhere, including at the top level; with `default`, such a value outside a `Schema` instance is passed to the hook first, and inside a `Schema` the wire form always applies. A `Secret` is the exception: it is always written as `"**********"` and never passed to `default`. With `sort_keys=True` object keys are ordered by their encoded text (quotes and escapes included), which is deterministic but not always `json.dumps` order for unusual keys, and integer keys sort as text. The public renderer accepts `indent` from 0 to 16, or `None` for compact output.

## Inherit and override

```python
# notes/apps/billing/codecs.py
from hinoki import JSONParser, JSONRenderer


class Envelope(JSONRenderer):
    media_type = "application/vnd.myapp+json"

    def prepare(self, value, request):  # reshape, then Rust encodes
        return {"data": value, "request_id": request.request_id}


class SnakeCase(JSONParser):
    def prepare(self, data):  # Rust decodes, then reshape
        return rename_keys(data) if isinstance(data, dict) else data
```

For a different format write a `Renderer` (`media_type`, `render(value, request) -> bytes`) or a `Parser` (`media_type`, `parse(raw, content_type) -> Parsed`); set `consumes_objects = True` on a renderer that can take `Schema` instances directly. Every instance in a position that declares a `Schema` ([subclass instances](https://hinoki.caspel.dev/dev/model-schemas.md#subclass-instances))—whether a subclass or an exact instance of the declared class—reaches a custom object-consuming renderer (such as one with `consumes_objects = True`, or a `JSONRenderer` subclass overriding `prepare`) as a `hinoki.core.Narrowed` view, never the raw instance: `isinstance(value, Declared)` holds, the declared fields and `@computed_field` values read as attributes, and `dump()` (with the keywords of `Schema.dump`: `include`, `exclude`, `exclude_unset`, `by_alias`) gives the declared fields only; any fields a subclass added are not readable through it, and a field or computed value that holds a `Schema` (or subclass) reads as a `Narrowed` of the class its own type declares, at every depth. `Narrowed(value, declared)` raises `TypeError` unless `declared` is a `Schema` class that `value` is an instance of. A subclass returned for a top-level declared class that overrides `dump()` reaches the renderer as the dict returned by the declared class's `dump()` restricted to the declared fields (an exact instance of that class arrives as a `Narrowed` whose `dump()` runs the same declared `dump()`). The built-in `JSONRenderer`, `XMLRenderer` and `Schema.dump()` narrow fields directly without wrapping exact instances in `Narrowed`. Media types must be distinct within one list; a failing `parse` raises `InvalidBody(code, message)` (a 400 with that stable code).
