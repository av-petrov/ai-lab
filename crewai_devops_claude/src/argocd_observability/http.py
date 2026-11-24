"""A GET-only HTTP client. The underlying session is private; every call is checked by the guard."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator

import requests

from .guard import ReadOnlyViolation

GuardFn = Callable[[str, str, Any], None]


class ResponseTooLarge(RuntimeError):
    pass


class GuardedHttpClient:
    def __init__(self, base_url: str, guard: GuardFn, *, headers: dict[str, str] | None = None,
                 verify: bool = True, timeout: float = 15.0, max_bytes: int = 20_000_000,
                 session: requests.Session | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._guard = guard
        self._session = session or requests.Session()
        self._session.headers.update(headers or {})
        self._verify = verify
        self.timeout = timeout
        self.max_bytes = max_bytes
        if not verify:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Any non-GET entry point refuses loudly (used by tests and by anyone who tries).
    def request(self, method: str, path: str, params: Any = None, **_: Any) -> Any:
        self._guard(method, path, params)
        if method.upper() != "GET":  # defence in depth if a guard is misconfigured
            raise ReadOnlyViolation("http", method, path, "only GET is implemented")
        return self.get_json(path, params)

    def post(self, path: str, *a: Any, **k: Any) -> Any:
        return self.request("POST", path)

    def put(self, path: str, *a: Any, **k: Any) -> Any:
        return self.request("PUT", path)

    def patch(self, path: str, *a: Any, **k: Any) -> Any:
        return self.request("PATCH", path)

    def delete(self, path: str, *a: Any, **k: Any) -> Any:
        return self.request("DELETE", path)

    # ------------------------------------------------------------------ GET
    def _get(self, path: str, params: Any, timeout: float | None, stream: bool) -> requests.Response:
        self._guard("GET", path, params)
        resp = self._session.get(self._base + path, params=params, verify=self._verify,
                                 timeout=timeout or self.timeout, stream=stream)
        if resp.status_code >= 400:
            body = resp.text[:500] if not stream else next(resp.iter_content(500), b"").decode(errors="replace")
            resp.close()
            raise requests.HTTPError(f"{resp.status_code} for GET {path}: {body}", response=resp)
        return resp

    def get_bytes(self, path: str, params: Any = None, timeout: float | None = None) -> bytes:
        resp = self._get(path, params, timeout, stream=True)
        chunks, size = [], 0
        try:
            for chunk in resp.iter_content(65536):
                size += len(chunk)
                if size > self.max_bytes:
                    raise ResponseTooLarge(f"response for {path} exceeds {self.max_bytes} bytes")
                chunks.append(chunk)
        finally:
            resp.close()
        return b"".join(chunks)

    def get_json(self, path: str, params: Any = None, timeout: float | None = None) -> Any:
        return json.loads(self.get_bytes(path, params, timeout) or b"null")

    def get_text(self, path: str, params: Any = None, timeout: float | None = None) -> str:
        return self.get_bytes(path, params, timeout).decode("utf-8", errors="replace")

    def iter_json_lines(self, path: str, params: Any = None, timeout: float | None = None,
                        max_lines: int = 5000) -> Iterator[Any]:
        """Newline-delimited JSON stream (Argo CD log endpoint), bounded in lines and bytes."""
        resp = self._get(path, params, timeout, stream=True)
        size = 0
        try:
            for n, line in enumerate(resp.iter_lines()):
                if n >= max_lines:
                    break
                size += len(line)
                if size > self.max_bytes:
                    break
                if line.strip():
                    yield json.loads(line)
        finally:
            resp.close()
