"""One Garmin client for the life of the server, and the rules for using it.

The client is created on the first tool call, not at startup, so the MCP handshake
never waits on Garmin (taxuspt #255). Calls are serialised by one lock: token refresh
stays safe and Garmin's rate limits are respected. Every failure is translated into a
`GarminMcpError` whose message keeps Garmin's status and text (taxuspt #183, #299:
a 4xx is not "unreachable").
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from . import auth, dates
from .config import Config
from .errors import (
    AuthExpired,
    Blocked,
    GarminMcpError,
    InputError,
    NotFound,
    NotLoggedIn,
    RateLimited,
    TokenUnreadable,
    UploadConsent,
    Upstream,
)

T = TypeVar("T")
_STATUS = re.compile(r"API Error (\d{3})")


def http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    match = _STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def map_error(exc: BaseException, hybrid_note: str = "") -> GarminMcpError:
    """Translate anything raised while talking to Garmin into a message for the model."""
    if isinstance(exc, GarminMcpError):
        return exc

    import requests
    from garminconnect import (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectInvalidFileFormatError,
        GarminConnectNotFoundError,
        GarminConnectTooManyRequestsError,
    )

    text = str(exc)
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return RateLimited(text)
    if isinstance(exc, GarminConnectAuthenticationError):
        return AuthExpired(text)
    if isinstance(exc, GarminConnectInvalidFileFormatError):
        return InputError(text)
    if isinstance(exc, GarminConnectNotFoundError):
        return NotFound(f"Garmin has no such item (HTTP 404): {text}")
    if isinstance(exc, GarminConnectConnectionError):
        status = http_status(exc)
        if status == 401:
            return AuthExpired(text)
        if status == 403:
            note = f" {hybrid_note}" if hybrid_note else ""
            return Blocked(
                f"Garmin refused the request with HTTP 403 ({text}).{note} If every call "
                "fails like this, Garmin's bot protection may be blocking this connection: "
                "try GARMIN_MCP_HTTP=curl_cffi, and avoid VPNs and datacenter networks. "
                "If only this item fails, Garmin is denying access to it."
            )
        if status == 412:
            return UploadConsent()
        if status == 429:
            return RateLimited(text)
        if status and status >= 500:
            return Upstream(f"Garmin had a server error ({text}). Try again in a minute.")
        if status:
            return Upstream(f"Garmin refused the request ({text}).")
        return Upstream(f"Could not reach Garmin: {text}")
    if isinstance(exc, requests.exceptions.RequestException):
        return Upstream(f"Could not reach Garmin ({type(exc).__name__}): {text}")
    if isinstance(exc, ValueError) and "Invalid API path" in text:
        return InputError(text)
    return Upstream(f"Unexpected {type(exc).__name__}: {text}")


class Cache:
    """A small LRU, optionally with expiry. Past days and static lookups don't change."""

    def __init__(self, maxsize: int = 1024):
        self.maxsize = maxsize
        self._data: OrderedDict[Any, tuple[float | None, Any]] = OrderedDict()

    def get_or(self, key: Any, fn: Callable[[], T], ttl: float | None = None) -> T:
        hit = self._data.get(key)
        now = time.monotonic()
        if hit is not None and (hit[0] is None or hit[0] > now):
            self._data.move_to_end(key)
            return hit[1]
        value = fn()
        self._data[key] = (None if ttl is None else now + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)
        return value

    def drop(self, key: Any) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


@dataclass
class Ctx:
    """What a tool's logic gets: the logged-in client plus the account's calendar."""

    g: Any
    cfg: Config
    tz: ZoneInfo
    cache: Cache
    session: GarminSession | None = field(default=None, repr=False)

    def today(self) -> date:
        return dates.today(self.tz)

    def day(self, value: str | None, *, default: str = "today", name: str = "date") -> date:
        return dates.parse(value, self.tz, default=default, name=name)

    def span(self, start: str | None, end: str | None, *, max_days: int, default_days: int = 7):
        return dates.parse_range(start, end, self.tz, max_days=max_days, default_days=default_days)

    def past(self, d: date) -> bool:
        """Days before today are final, so their data can be cached."""
        return d < self.today()

    def cached(self, key: Any, fn: Callable[[], T], *, past: date | None = None) -> T:
        """Cache forever for static lookups, or for a finished day; else don't cache."""
        if past is not None and not self.past(past):
            return fn()
        return self.cache.get_or(key, fn)


def local_tz():
    """The computer's own zone, the last resort when Garmin doesn't say."""
    return datetime.now().astimezone().tzinfo


def tz_name(tz) -> str:
    return getattr(tz, "key", None) or str(tz)


class GarminSession:
    def __init__(self, cfg: Config, opener: Callable[[], tuple[Any, Any]] | None = None):
        self.cfg = cfg
        # GARMIN_MCP_TIMEZONE, else the account's zone once logged in, else this computer's.
        self.tz = ZoneInfo(cfg.timezone) if cfg.timezone else local_tz()
        self._opener = opener or (lambda: auth.open_with_tokens(cfg))
        self._lock = threading.RLock()
        self._garmin: Any = None
        self.hybrid: Any = None
        self.cache = Cache()
        self.logged_in_at: datetime | None = None
        self.last_ok_at: datetime | None = None
        self.last_error: tuple[datetime, str] | None = None

    @property
    def logged_in(self) -> bool:
        return self._garmin is not None

    def _ensure(self) -> Any:
        if self._garmin is None:
            self._garmin, self.hybrid = self._opener()
            if not self.cfg.timezone:
                self.tz = self._account_tz(self._garmin) or self.tz
            self.logged_in_at = datetime.now(self.tz)
        return self._garmin

    @staticmethod
    def _account_tz(garmin: Any):
        """The time zone set on the Garmin account, so 'today' is the user's today."""
        try:
            name = (garmin.get_userprofile_settings() or {}).get("timeZone")
            return ZoneInfo(name) if name else None
        except Exception:  # noqa: BLE001 - fall back to this computer's zone
            return None

    def ctx(self) -> Ctx:
        return Ctx(self._ensure(), self.cfg, self.tz, self.cache, self)

    def run(self, fn: Callable[[Ctx], T]) -> T:
        """Run one tool's logic against the logged-in client, under the lock."""
        with self._lock:
            # Several servers can share one token file (one per client or window). When
            # another one renews the tokens, ours are revoked even though the file on disk
            # is good. So on an auth failure, drop the login, re-read the file and retry
            # once before reporting anything.
            previous_error = self.last_error
            for attempt in (0, 1):
                try:
                    result = fn(self.ctx())
                except Exception as exc:
                    err = self.failure(exc)  # also drops a dead login
                    if attempt == 0 and isinstance(err, AuthExpired):
                        self.last_error = previous_error  # recovered: don't report it
                        self.cache.clear()
                        continue
                    raise err from exc
                break
            self.last_ok_at = datetime.now(self.tz)
            return result

    def run_raw(self, fn: Callable[[GarminSession], T]) -> T:
        """Like run, but without forcing a login first (garmin_status needs this)."""
        with self._lock:
            return fn(self)

    def failure(self, exc: BaseException) -> GarminMcpError:
        """Translate a failure, remember it for garmin_status, drop a dead login."""
        note = ""
        if self.hybrid is not None and getattr(self.hybrid, "retries", 0):
            note = "It was already retried once through curl_cffi."
        err = map_error(exc, note)
        if isinstance(err, (AuthExpired, NotLoggedIn, TokenUnreadable)):
            self._garmin = None  # pick up a fresh login next time
        if not isinstance(err, InputError):
            self.last_error = (datetime.now(self.tz), str(err))
        return err
