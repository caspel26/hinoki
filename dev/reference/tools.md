# Application MCP and scaffold API

- `hinoki.ai.mcpapp.expose_as_mcp(...)`: The MCP action to ``app.include(...)``, publishing the matching actions. [reference](https://hinoki.caspel.dev/dev/reference/tools/#hinoki.ai.mcpapp.expose_as_mcp)

- `hinoki.ai.mcpapp.ExposureError`: The MCP exposure is configured wrongly (raised when it is built). [reference](https://hinoki.caspel.dev/dev/reference/tools/#hinoki.ai.mcpapp.ExposureError)

- `hinoki.ai.llms.render_llms(document: 'Mapping[str, object]', *, openapi_url: 'str | None' = None) -> 'str'`: The llms.txt Markdown for an OpenAPI ``document``, with a trailing newline. [reference](https://hinoki.caspel.dev/dev/reference/tools/#hinoki.ai.llms.render_llms)

Project scaffolding is a command-line interface; see [command line](https://hinoki.caspel.dev/dev/cli.md). For the stdio development server and its tools, see the [development MCP guide](https://hinoki.caspel.dev/dev/dev-mcp.md). Application MCP is mounted separately on selected routes.
