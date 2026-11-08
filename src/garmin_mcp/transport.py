"""The Windows 403 fallback.

garminconnect sends every data call through `Client._api_session.request(...)`, a plain
`requests.Session`. On some Windows 11 machines Cloudflare answers those calls with 403
while the very same request made through curl_cffi (impersonating Chrome) succeeds
(python-garminconnect #444, taxuspt #366). `HybridSession` stands in for that session:
in `auto` mode a 403 is retried once through curl_cffi, and if the retry gets through,
every later call in this process goes through curl_cffi too. Uploads (`files=`) always
stay on requests, because curl_cffi can't send them that way.

`_api_session` is a private attribute, so if a future garminconnect renames it the
fallback switches itself off and `garmin_status` says so (garminconnect is pinned <0.4).
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from . import dns_fallback


class HybridSession:
    def __init__(self, inner: Any, mode: str = "auto"):
        self._inner = inner
        self.mode = mode  # auto | requests | curl_cffi
        self.switched_at: datetime | None = None
        self.retries = 0
        self._cffi = None
        self._lock = threading.Lock()

    @property
    def using_cffi(self) -> bool:
        return self.mode == "curl_cffi" or self.switched_at is not None

    def _cffi_session(self):
        with self._lock:
            if self._cffi is None:
                from curl_cffi import requests as cffi_requests

                self._cffi = cffi_requests.Session(impersonate="chrome")
            return self._cffi

    def request(self, method: str, url: str, **kwargs: Any):
        uploads = "files" in kwargs
        if not uploads and self.using_cffi:
            return self._cffi_session().request(method, url, **kwargs)

        resp = self._inner.request(method, url, **kwargs)
        if self.mode == "auto" and not uploads and resp.status_code == 403:
            # A 403 means the request was refused, not applied, so one retry is safe
            # even for writes.
            self.retries += 1
            retry = self._cffi_session().request(method, url, **kwargs)
            if retry.status_code != 403:
                self.switched_at = datetime.now().astimezone()
            return retry
        return resp

    def describe(self) -> str:
        text = self._describe_mode()
        dns = dns_fallback.describe()
        return f"{text}; {dns}" if dns else text

    def _describe_mode(self) -> str:
        if self.mode == "requests":
            return "requests only (fallback disabled by GARMIN_MCP_HTTP=requests)"
        if self.mode == "curl_cffi":
            return "curl_cffi for every call (GARMIN_MCP_HTTP=curl_cffi)"
        if self.switched_at:
            return (
                "curl_cffi: switched at "
                f"{self.switched_at:%Y-%m-%d %H:%M} after a 403 that curl_cffi got past"
            )
        return "requests, with the curl_cffi 403 fallback armed"

    def __getattr__(self, name: str) -> Any:
        # cookies, mount, close, headers... belong to the wrapped session.
        return getattr(self._inner, name)


def install(garmin: Any, mode: str) -> HybridSession | None:
    """Swap garminconnect's API session for a HybridSession. None if it can't be done.

    Also arms the curl_cffi DNS fallback (dns_fallback.py) for the whole process.
    """
    dns_fallback.install()
    client = getattr(garmin, "client", None)
    inner = getattr(client, "_api_session", None)
    if inner is None or not hasattr(inner, "request"):
        return None
    if isinstance(inner, HybridSession):
        return inner
    hybrid = HybridSession(inner, mode)
    client._api_session = hybrid
    return hybrid
