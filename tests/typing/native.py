"""The packaged extension stub describes the ``hinoki.native`` module."""

from hinoki.native import Router, core_version, encode_json

router = Router()
router.add("GET", "/health", 0)
match = router.find("GET", "/health")
version: str = core_version()
encoded: bytes = encode_json({"status": "ok"})

print(match, version, encoded)
