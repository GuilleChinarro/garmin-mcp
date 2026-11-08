"""Fallback for curl_cffi's own DNS resolver.

curl_cffi ships a libcurl built with c-ares, which talks to the DNS servers itself
instead of asking the operating system. Some home routers send replies that c-ares
rejects ("curl: (6) Could not resolve host: connectapi.garmin.com (Misformatted DNS
reply)") while Windows resolves the same name without trouble. Every curl_cffi call
then fails although the network is fine.

`install()` patches `curl_cffi.requests.Session.request` once per process. When a call
fails to resolve, the name is looked up through the OS (`socket.getaddrinfo`), pinned
with CURLOPT_RESOLVE and the call is retried once. A resolve failure means nothing was
sent, so the retry is safe for writes too. Pins expire after `PIN_SECONDS` so a moved
Garmin endpoint is picked up again. The patch covers every curl_cffi caller in the
process: the data calls in transport.py and garminconnect's own token refresh and login.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

PIN_SECONDS = 30 * 60

_lock = threading.Lock()
_pins: dict[tuple[str, int], tuple[str, float]] = {}  # (host, port) -> (ip, expires)
_installed = False

fallbacks = 0
last_fallback: datetime | None = None
last_host: str | None = None


def is_dns_error(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == 6 or "Could not resolve host" in str(exc)


def system_resolve(host: str, port: int) -> str:
    """The address the OS resolver gives for host, IPv4 first."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    return infos[0][4][0]


def _host_port(url: str) -> tuple[str, int] | None:
    parts = urlsplit(url)
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _entry(host: str, port: int, ip: str) -> str:
    return f"{host}:{port}:[{ip}]" if ":" in ip else f"{host}:{port}:{ip}"


def _resolve_option(now: float) -> list[str]:
    """CURLOPT_RESOLVE entries: live pins, plus removals for expired ones.

    curl keeps a pinned address in the handle's DNS cache for good, so an expired pin
    has to be removed explicitly with a "-host:port" entry.
    """
    entries = []
    for (host, port), (ip, expires) in list(_pins.items()):
        if expires <= now:
            entries.append(f"-{host}:{port}")
            del _pins[(host, port)]
        else:
            entries.append(_entry(host, port, ip))
    return entries


def _apply(session: Any, now: float) -> None:
    from curl_cffi import CurlOpt

    with _lock:
        entries = _resolve_option(now)
    options = session.curl_options
    if entries:
        options[CurlOpt.RESOLVE] = entries
    else:
        options.pop(CurlOpt.RESOLVE, None)


def pin(host: str, port: int, ip: str, now: float | None = None) -> None:
    global fallbacks, last_fallback, last_host
    now = time.monotonic() if now is None else now
    with _lock:
        _pins[(host, port)] = (ip, now + PIN_SECONDS)
        fallbacks += 1
        last_fallback = datetime.now().astimezone()
        last_host = host


def wrap(original):
    def request(self, method, url, *args, **kwargs):
        if _pins:
            _apply(self, time.monotonic())
        try:
            return original(self, method, url, *args, **kwargs)
        except Exception as exc:
            target = _host_port(url) if is_dns_error(exc) else None
            if target is None:
                raise
            try:
                ip = system_resolve(*target)
            except OSError:
                raise exc from None
            pin(*target, ip)
            _apply(self, time.monotonic())
            return original(self, method, url, *args, **kwargs)

    request.__wrapped__ = original
    request._garmin_mcp_dns_fallback = True
    return request


def install() -> bool:
    """Patch curl_cffi once. False if curl_cffi isn't importable."""
    global _installed
    with _lock:
        if _installed:
            return True
        try:
            from curl_cffi.requests import Session
        except ImportError:
            return False
        if not getattr(Session.request, "_garmin_mcp_dns_fallback", False):
            Session.request = wrap(Session.request)
        _installed = True
        return True


def describe() -> str:
    if not fallbacks:
        return ""
    return (
        f"DNS fallback used {fallbacks}x (last {last_fallback:%Y-%m-%d %H:%M} for "
        f"{last_host}): curl_cffi could not resolve it, the system resolver could"
    )
