# Benchmarks

Measured results only; nothing here is a performance claim about Hinoki as a whole.

## `routerbench.py` — path lookup, Python → Rust

Question: what does one `Router.find` call cost from Python, compared with a deliberately simple pure-Python regex router (linear scan)? Nine routes, six fixed request lines (static, one/two parameters, miss), 5 samples × 200 000 lookups after warm-up, median and coefficient of variation recorded in `results.json` together with Python, platform and package version.

```sh
uv run python benchmarks/routerbench.py --samples 5 --iterations 200000
```

First observations (2026-11-13, one Linux laptop, Python 3.13, **debug-profile** extension as built by `uv sync`):

| Router | Median per lookup | CV |
| --- | --- | --- |
| `hinoki.native.Router` | ≈ 0.3 µs | ≈ 1–5 % |
| Python regex baseline (9 routes) | ≈ 0.6 µs | ≈ 3 % |

A one-off release-profile build (`uvx maturin develop --release --uv`) gave a statistically indistinguishable result (≈ 0.33 µs vs ≈ 0.6 µs), so the cost is dominated by the Python↔Rust call, argument conversion and the result tuple, not by the matching itself. Consequences:

* Routing alone is **not** where the Rust core pays off at this table size; the advantage must come from work that stays in Rust across many steps (request decoding, validation, ORM), which is exactly why the design keeps crossings few.
* The baseline is a toy. It says nothing about FastAPI, Starlette or any other framework, and no such comparison is made.
* These numbers are not end-to-end: they exclude HTTP, ASGI, parsing, validation and I/O.

Not yet measured (RUNTIME-DESIGN V10): ASGI vs RSGI, extra Tokio threads per worker, full request path under load.
