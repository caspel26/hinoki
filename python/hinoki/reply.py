"""Deprecated compatibility module: import from ``hinoki.http.response``.

``Reply`` is the former name of ``Response``. This module and the alias are
kept for one compatibility cycle.
"""

import warnings

from hinoki.http.response import Response, ResponseHeaders, encode_header, set_cookie

warnings.warn(
    "hinoki.reply is deprecated; import Response from hinoki.http.response",
    DeprecationWarning,
    stacklevel=2,
)

Reply = Response

__all__ = ["Reply", "Response", "ResponseHeaders", "encode_header", "set_cookie"]
