"""Polite HTTP fetching: robots.txt, per-host delay, size cap, HTML-only."""
from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional
from urllib import robotparser
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)


@dataclass
class Page:
    url: str  # final URL after redirects
    status: int
    text: str


CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


def decode_body(body: bytes, content_type: str) -> str:
    """Header charset, then <meta charset>, then UTF-8, then Windows-1252 (common on old French sites)."""
    declared = re.search(r"charset=([\w-]+)", content_type, re.I)
    meta = CHARSET_RE.search(body[:4096])
    for enc in filter(None, [declared and declared.group(1), meta and meta.group(1).decode("ascii", "ignore")]):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


class Fetcher:
    """Thread-safe polite fetcher with a page cache (discovery and scraping share pages)."""

    def __init__(
        self,
        user_agent: str,
        timeout: int = 15,
        max_bytes: int = 2_000_000,
        per_host_delay: float = 1.0,
        respect_robots_txt: bool = True,
        session: Optional[requests.Session] = None,
        connect_timeout: float = 5.0,
        cache_size: int = 1024,
    ):
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "fr-FR,fr;q=0.9"}
        )
        self.user_agent = user_agent
        # Short connect timeout: guessed domains that do not answer fail in seconds, not 15 s.
        self.timeout = (min(connect_timeout, timeout), timeout)
        self.max_bytes = max_bytes
        self.per_host_delay = per_host_delay
        self.respect_robots = respect_robots_txt
        self.cache_size = cache_size
        self.requests = 0  # HTTP requests actually made (for stats)
        self._robots: dict[str, Optional[robotparser.RobotFileParser]] = {}
        self._unreachable: set[str] = set()
        self._dns: dict[str, bool] = {}
        self._cache: OrderedDict[str, Optional[Page]] = OrderedDict()
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, http_cfg: dict) -> "Fetcher":
        return cls(
            user_agent=http_cfg["user_agent"],
            timeout=http_cfg["timeout"],
            max_bytes=http_cfg["max_bytes"],
            per_host_delay=http_cfg["per_host_delay"],
            respect_robots_txt=http_cfg["respect_robots_txt"],
            connect_timeout=http_cfg.get("connect_timeout", 5),
        )

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    def resolves(self, url: str) -> bool:
        """Cheap DNS check before any HTTP request (most guessed domains do not exist)."""
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            return True  # behind a proxy, names are resolved by the proxy, not locally
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            if host in self._dns:
                return self._dns[host]
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ok = True
        except (socket.gaierror, UnicodeError, OSError):
            ok = False
        with self._lock:
            self._dns[host] = ok
        return ok

    def _http_get(self, url: str, **kwargs: Any) -> requests.Response:
        with self._lock:
            self.requests += 1
        try:
            return self.session.get(url, timeout=self.timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if not isinstance(exc, requests.ReadTimeout):
                with self._lock:
                    self._unreachable.add(self._origin(url))  # do not try this site again
            raise

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        origin = self._origin(url)
        with self._lock:
            known = origin in self._robots
        if not known:
            rp: Optional[robotparser.RobotFileParser] = robotparser.RobotFileParser()
            try:
                resp = self._http_get(origin + "/robots.txt")
                if resp.status_code >= 400:
                    rp = None  # no robots.txt -> everything allowed
                else:
                    rp.parse(resp.text.splitlines())
            except requests.RequestException:
                rp = None
            with self._lock:
                self._robots[origin] = rp
        rp = self._robots[origin]
        return rp is None or rp.can_fetch(self.user_agent, url)

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self.per_host_delay - (time.monotonic() - self._last_hit.get(host, 0.0))
            self._last_hit[host] = time.monotonic() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def _remember(self, url: str, page: Optional[Page]) -> Optional[Page]:
        with self._lock:
            self._cache[url] = page
            if page is not None and page.url != url:
                self._cache[page.url] = page
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return page

    def get(self, url: str) -> Optional[Page]:
        with self._lock:
            if url in self._cache:
                self._cache.move_to_end(url)
                return self._cache[url]
            if self._origin(url) in self._unreachable:
                return None
        try:
            if not self._allowed(url):
                log.debug("robots.txt disallows %s", url)
                return self._remember(url, None)
            if self._origin(url) in self._unreachable:
                return None
            self._throttle(urlsplit(url).netloc)
            with self._http_get(url, stream=True, allow_redirects=True) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if resp.status_code >= 400 or ("html" not in ctype and "text" not in ctype):
                    return self._remember(url, None)
                chunks, size = [], 0
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.max_bytes:
                        break
                body = b"".join(chunks)
                return self._remember(url, Page(url=resp.url, status=resp.status_code, text=decode_body(body, ctype)))
        except (requests.RequestException, UnicodeError, ValueError) as exc:
            log.debug("fetch failed %s: %s", url, exc)
            return None
