"""Bounded public-ATS HTTP access, shared safely by collection workers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

_HOST_LOCK = threading.Lock()
_HOST_LIMITS: dict[str, threading.BoundedSemaphore] = {}


class SourceHTTPError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _host_limit(host: str) -> threading.BoundedSemaphore:
    with _HOST_LOCK:
        return _HOST_LIMITS.setdefault(host, threading.BoundedSemaphore(2))


class SourceClient:
    """One source's request deadline; callers may inject a MockTransport client."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        deadline_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client or httpx.Client(
            timeout=20,
            follow_redirects=False,
            headers={
                "User-Agent": "MechE-Job-Tracker/0.1 (public career postings)",
                "Accept": "application/json",
            },
        )
        self._owns_client = client is None
        self.clock = clock
        self.sleep = sleep
        self.deadline = clock() + deadline_seconds

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise SourceHTTPError("Source collection exceeded its 300-second deadline")
        return remaining

    def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        host = urlsplit(url).hostname or ""
        semaphore = _host_limit(host)
        for attempt in range(3):
            remaining = self.remaining()
            if not semaphore.acquire(timeout=remaining):
                raise SourceHTTPError("Source deadline reached waiting for host request capacity")
            response: httpx.Response | None = None
            try:
                response = self.client.request(
                    method, url, timeout=min(20.0, self.remaining()), **kwargs
                )
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SourceHTTPError(f"Transport failure: {type(exc).__name__}") from exc
            finally:
                semaphore.release()
            self.remaining()
            if response is not None:
                if response.status_code not in {408, 429} and response.status_code < 500:
                    if not 200 <= response.status_code < 300:
                        raise SourceHTTPError(
                            f"HTTP {response.status_code} from {host}",
                            status_code=response.status_code,
                        )
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise SourceHTTPError(
                            "Expected JSON; received an invalid response"
                        ) from exc
                if attempt == 2:
                    raise SourceHTTPError(
                        f"HTTP {response.status_code} after 3 attempts from {host}",
                        status_code=response.status_code,
                    )
            delay = 2.0**attempt
            if response is not None and (retry := response.headers.get("Retry-After")):
                try:
                    delay = max(delay, float(retry))
                except ValueError:
                    try:
                        until = parsedate_to_datetime(retry)
                        if until.tzinfo is None:
                            until = until.replace(tzinfo=UTC)
                        delay = max(delay, (until - datetime.now(UTC)).total_seconds())
                    except TypeError, ValueError, OverflowError:
                        pass
            if delay >= self.remaining():
                raise SourceHTTPError("Retry-After/backoff exceeds source collection deadline")
            self.sleep(delay)
        raise SourceHTTPError("Request retry budget exhausted")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request_json("GET", url, **kwargs)

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        return self.request_json("POST", url, json=payload)
