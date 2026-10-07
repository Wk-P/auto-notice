from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class HttpError(RuntimeError):
    pass


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        for candidate in (charset, "utf-8", "euc-kr"):
            try:
                return self.body.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode("utf-8", errors="replace")


class HttpClient:
    def __init__(self, timeout: int = 30, requests_per_second: float = 1.0):
        self.timeout = timeout
        self.min_interval = 1.0 / max(requests_per_second, 0.1)
        self.last_request = 0.0

    def request(self, url: str, *, method: str = "GET", json_body: dict | None = None,
                headers: dict[str, str] | None = None, retries: int = 3) -> HttpResponse:
        delay = self.min_interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        data = json.dumps(json_body).encode() if json_body is not None else None
        request_headers = {"User-Agent": "PNU-Notice-Monitor/1.0", **(headers or {})}
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.last_request = time.monotonic()
                    return HttpResponse(response.status, response.read(),
                                        {key.lower(): value for key, value in response.headers.items()})
            except urllib.error.HTTPError as exc:
                self.last_request = time.monotonic()
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                # A 429 for an exhausted quota will not clear by waiting a few seconds, so it is not retried.
                quota = exc.code == 429 and "insufficient_quota" in detail
                if exc.code not in (429, 500, 502, 503, 504) or quota or attempt == retries:
                    raise HttpError(f"HTTP {exc.code} for {url}: {detail}") from exc
            except urllib.error.URLError as exc:
                self.last_request = time.monotonic()
                if attempt == retries:
                    raise HttpError(f"Request failed for {url}: {exc.reason}") from exc
            time.sleep(min(2 ** attempt, 8))
        raise HttpError(f"Request failed for {url}")
