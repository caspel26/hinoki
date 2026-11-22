# Third-party code in this directory

Served unmodified from `static/`. Versions are pinned; update them deliberately
and refresh the hashes (a test compares them).

| File | Project | Version | Licence | sha256 |
| --- | --- | --- | --- | --- |
| `swagger-ui-bundle.js`, `swagger-ui.css` | [Swagger UI](https://github.com/swagger-api/swagger-ui) (`swagger-ui-dist`) | 5.35.1 | Apache-2.0 (`LICENSE-swagger-ui`) | `d0e0bee93732408c91fbad05cb0c2812e9ad37e94d12799c5503a9fe7afa2b82`, `5ed08757e53da3e026d6cbecc61fe5d43585c9560b302d534ad56692519a273d` |
| `redoc.standalone.js` | [ReDoc](https://github.com/Redocly/redoc) | 3.2.0 | MIT (`LICENSE-redoc`) | `c82174cbccc3a141f8d8fc7722dbb9e73cc70716b46ba3c897bc4bce06ece1b6` |

Obtained from the npm packages `swagger-ui-dist` and `redoc` on 2026-11-22. Both
bundles contain further third-party code under their own licences, listed in the
upstream packages. Swagger UI 5.35.1 and ReDoc 3.2.0 read OpenAPI 3.2 documents;
the earlier 5.17.14 and 2.1.5 refuse them. ReDoc 3 is an ES module (`export { init }`),
so its page loads it with `type="module"`.
