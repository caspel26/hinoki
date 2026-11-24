# Parsers and renderers

- `hinoki.parsers.Parser()`: Base class of body parsers. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.Parser)

- `hinoki.parsers.JSONParser()`: JSON, decoded by the Rust parser (strict: no NaN, no lone surrogates, depth limit). [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.JSONParser)

- `hinoki.parsers.FormParser(max_fields: 'int' = 1000) -> 'None'`: ``application/x-www-form-urlencoded``. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.FormParser)

- `hinoki.parsers.MultiPartParser(max_parts: 'int' = 100, max_part_bytes: 'int' = 1048576) -> 'None'`: ``multipart/form-data`` with at most ``max_parts`` parts of at most ``max_part_bytes`` bytes each (the whole body is capped separately). [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.MultiPartParser)

- `hinoki.parsers.TextParser()`: ``text/plain`` bodies as a ``str`` (UTF-8, strict). [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.TextParser)

- `hinoki.parsers.BytesParser()`: ``application/octet-stream`` bodies as the raw ``bytes``, unchanged. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.BytesParser)

- `hinoki.parsers.XMLParser(max_depth: 'int' = 32, max_elements: 'int' = 10000) -> 'None'`: ``application/xml``. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.XMLParser)

- `hinoki.parsers.Parsed(data: 'object', fields: 'Mapping[str, list[str]]' = <factory>, files: 'Mapping[str, list[UploadedFile]]' = <factory>) -> None`: What a parser produced: ``data`` for ``request.data`` plus form fields and files for ``request.POST`` / ``request.FILES``. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.parsers.Parsed)

- `hinoki.errors.InvalidBody(code: 'str', message: 'str') -> 'None'`: The request body cannot be used at all (wrong media type, bad JSON). [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.errors.InvalidBody)

- `hinoki.UploadedFile(name: 'str', filename: 'str', content_type: 'str', content: 'bytes') -> None`: A multipart file. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.UploadedFile)

- `hinoki.renderers.Renderer()`: Base class of response renderers. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.renderers.Renderer)

- `hinoki.renderers.JSONRenderer(indent: 'int | None' = None, sort_keys: 'bool' = False, default: 'Callable[[Any], Any] | None' = None) -> 'None'`: JSON, written by the Rust encoder. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.renderers.JSONRenderer)

- `hinoki.renderers.HTMLRenderer()`: A handler that returns ``str`` answers HTML (UTF-8). [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.renderers.HTMLRenderer)

- `hinoki.renderers.TextRenderer()`: A handler that returns ``str`` answers plain text (UTF-8). [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.renderers.TextRenderer)

- `hinoki.renderers.XMLRenderer(root: 'str' = 'response', item: 'str' = 'item') -> 'None'`: The result as XML (UTF-8, with an XML declaration), written by the Rust encoder, which accepts what the JSON encoder accepts. [reference](https://hinoki.caspel.dev/dev/reference/codecs/#hinoki.renderers.XMLRenderer)
