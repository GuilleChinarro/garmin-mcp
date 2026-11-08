"""curl_cffi's own resolver failing while the OS resolver works ("Misformatted DNS reply")."""

import http.server
import threading
import time

import pytest

from garmin_mcp import dns_fallback


class DnsError(Exception):
    code = 6


class FakeCffiSession:
    def __init__(self, fail_times=1):
        self.curl_options = {}
        self.fail_times = fail_times
        self.calls = []

    def request(self, method, url, **kwargs):
        from curl_cffi import CurlOpt

        self.calls.append((method, url, list(self.curl_options.get(CurlOpt.RESOLVE, []))))
        if self.fail_times:
            self.fail_times -= 1
            raise DnsError("curl: (6) Could not resolve host: connectapi.garmin.com "
                           "(Misformatted DNS reply)")
        return "ok"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    dns_fallback._pins.clear()
    monkeypatch.setattr(dns_fallback, "fallbacks", 0)
    monkeypatch.setattr(dns_fallback, "system_resolve", lambda host, port: "203.0.113.7")
    yield
    dns_fallback._pins.clear()


def patched(session):
    return lambda *a, **k: dns_fallback.wrap(type(session).request)(session, *a, **k)


def test_a_dns_failure_is_retried_once_with_the_system_address():
    s = FakeCffiSession()
    assert patched(s)("GET", "https://connectapi.garmin.com/x") == "ok"
    assert s.calls[1][2] == ["connectapi.garmin.com:443:203.0.113.7"]
    assert "DNS fallback used 1x" in dns_fallback.describe()


def test_later_calls_reuse_the_pin_without_failing_first():
    s = FakeCffiSession()
    patched(s)("GET", "https://connectapi.garmin.com/a")
    other = FakeCffiSession(fail_times=0)
    patched(other)("GET", "https://connectapi.garmin.com/b")
    assert other.calls == [("GET", "https://connectapi.garmin.com/b",
                            ["connectapi.garmin.com:443:203.0.113.7"])]


def test_an_expired_pin_is_removed_from_curls_cache():
    dns_fallback.pin("connectapi.garmin.com", 443, "203.0.113.7",
                     now=time.monotonic() - dns_fallback.PIN_SECONDS - 1)
    s = FakeCffiSession(fail_times=0)
    patched(s)("GET", "https://connectapi.garmin.com/a")
    assert s.calls[0][2] == ["-connectapi.garmin.com:443"]
    assert dns_fallback._pins == {}


def test_a_second_dns_failure_is_raised():
    s = FakeCffiSession(fail_times=2)
    with pytest.raises(DnsError):
        patched(s)("GET", "https://connectapi.garmin.com/x")
    assert len(s.calls) == 2


def test_other_errors_are_not_retried():
    class Boom(FakeCffiSession):
        def request(self, method, url, **kwargs):
            self.calls.append(url)
            raise ConnectionError("curl: (28) Operation timed out")

    s = Boom()
    with pytest.raises(ConnectionError):
        patched(s)("GET", "https://connectapi.garmin.com/x")
    assert len(s.calls) == 1


def test_when_the_system_cannot_resolve_either_the_original_error_is_raised(monkeypatch):
    def nope(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(dns_fallback, "system_resolve", nope)
    with pytest.raises(DnsError):
        patched(FakeCffiSession())("GET", "https://connectapi.garmin.com/x")


def test_ipv6_addresses_are_bracketed():
    assert dns_fallback._entry("h", 443, "2001:db8::1") == "h:443:[2001:db8::1]"


def test_real_curl_cffi_reaches_a_host_only_the_system_resolver_knows(monkeypatch):
    """End to end with the real curl_cffi: c-ares can't resolve the name, the OS 'can'."""
    curl_cffi = pytest.importorskip("curl_cffi.requests")
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(dns_fallback, "system_resolve", lambda host, p: "127.0.0.1")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    original = curl_cffi.Session.request
    raw = getattr(original, "__wrapped__", original)  # other tests may have installed it
    curl_cffi.Session.request = raw
    dns_fallback._installed = False
    try:
        assert dns_fallback.install()
        dns_fallback.install()  # idempotent
        assert curl_cffi.Session.request.__wrapped__ is raw
        resp = curl_cffi.get(f"http://connectapi.garmin.invalid:{port}/", timeout=5)
        assert resp.status_code == 200
        assert dns_fallback.fallbacks == 1
    finally:
        curl_cffi.Session.request = original
        dns_fallback._installed = False
        server.shutdown()
