"""The only module that touches the network.

Everything else receives a `Transport` and tests pass a fake one. Each
transport is bound to a set of allowed hosts, checked on the first URL and on
every redirect hop, and to a minimum interval between requests.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol

from boxd_match import __version__

USER_AGENT = f"boxd-match/{__version__} (+https://github.com/Samuellct/boxd-match)"
LETTERBOXD_HOSTS = frozenset({"letterboxd.com", "boxd.it"})
TMDB_HOSTS = frozenset({"api.themoviedb.org"})


@dataclass
class Response:
    status: int
    url: str  # final URL after redirects
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport(Protocol):
    def get(self, url: str) -> Response: ...


class ForbiddenHost(Exception):
    """A request or a redirect targeted a host outside the allow-list."""


class NetworkError(Exception):
    """No HTTP response at all (DNS, timeout, connection reset)."""


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def with_query(url: str, params: dict[str, str]) -> str:
    return f"{url}?{urllib.parse.urlencode(params)}" if params else url


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: frozenset[str]) -> None:
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if host_of(newurl) not in self.allowed:
            raise ForbiddenHost(f"redirect to {host_of(newurl) or newurl!r} refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    def __init__(
        self,
        allowed_hosts: frozenset[str],
        min_interval: float,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.allowed = allowed_hosts
        self.min_interval = min_interval
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self._opener = urllib.request.build_opener(_CheckedRedirect(allowed_hosts))

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def get(self, url: str) -> Response:
        if host_of(url) not in self.allowed:
            raise ForbiddenHost(f"host {host_of(url) or url!r} is not allowed")
        self._throttle()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                return Response(resp.status, resp.geturl(), resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as error:
            return Response(error.code, error.geturl() or url, error.read() or b"", dict(error.headers.items()))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            # Never echo the URL: TMDB URLs carry the API key.
            reason = getattr(error, "reason", error)
            raise NetworkError(f"{host_of(url)}: {reason}") from None


def letterboxd_transport() -> UrllibTransport:
    return UrllibTransport(LETTERBOXD_HOSTS, min_interval=1.0)


def tmdb_transport() -> UrllibTransport:
    return UrllibTransport(TMDB_HOSTS, min_interval=0.05)
