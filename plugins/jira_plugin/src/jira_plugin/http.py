"""Bounded JSON HTTP transport; never logs credentials, request bodies or URLs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .errors import JiraServiceError

NO_BODY = object()


@dataclass(frozen=True)
class Response:
    status: int
    data: Any
    headers: dict[str, str]


def request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any = NO_BODY,
    timeout: float = 30.0,
) -> Response:
    """Send JSON without redirecting authenticated requests to another origin."""
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    payload = None if body is NO_BODY else json.dumps(body).encode()
    req = Request(url, data=payload, headers=headers, method=method)
    try:
        with build_opener(NoRedirect).open(req, timeout=timeout) as result:
            return _response(result.status, result.read(), dict(result.headers.items()))
    except HTTPError as exc:
        return _response(exc.code, exc.read(), dict(exc.headers.items()))
    except (URLError, OSError) as exc:
        raise JiraServiceError("jira.connection_failed", "Jira connection failed.") from exc


def _response(status: int, content: bytes, headers: dict[str, str]) -> Response:
    if not content:
        data: Any = None
    else:
        try:
            data = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            data = {"message": "Vendor returned a non-JSON response."}
    return Response(status, data, {k.lower(): v for k, v in headers.items()})
