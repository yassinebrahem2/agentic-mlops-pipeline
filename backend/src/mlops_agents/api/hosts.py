"""The Host and Origin checks on every request: they stop other websites calling the app and
DNS rebinding. Behind a trusted proxy, the host, scheme and client come from its headers."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from mlops_agents.api.errors import error_response
from mlops_agents.config import RunMode, Settings


def _split(host: str) -> tuple[str, str | None]:
    """`name[:port]` (IPv6 names in brackets) as the lowercased name and the port, if any."""
    host = host.strip().lower()
    if host.startswith("["):
        name, _, rest = host.partition("]")
        return name + "]", rest.removeprefix(":") or None
    name, _, port = host.partition(":")
    return name, port or None


def _hops(value: str) -> list[str]:
    """Hops in a comma-separated forwarded header value."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _last(value: str) -> str:
    """The last hop of a forwarded header: the one the trusted proxy appended."""
    hops = _hops(value)
    return hops[-1] if hops else ""


class HostRules:
    """Which hosts, origins and proxies these settings allow."""

    def __init__(self, settings: Settings) -> None:
        if settings.mode is RunMode.SERVER:
            entries = settings.allowed_hosts
            self.scheme = "https"
        else:
            entries = (f"127.0.0.1:{settings.port}", f"localhost:{settings.port}")
            self.scheme = "http"
        self._hosts = [_split(entry) for entry in entries]
        self._dev_origins = {origin.strip().rstrip("/").lower() for origin in settings.dev_origins}
        self._proxies = frozenset(settings.trusted_proxies)

    def host_allowed(self, host: str) -> bool:
        name, port = _split(host)
        # An entry without a port allows its name on any port, as the proxy may add one.
        return any(
            name == allowed and (want is None or port == want) for allowed, want in self._hosts
        )

    def origin_allowed(self, origin: str) -> bool:
        origin = origin.strip().rstrip("/").lower()
        if origin in self._dev_origins:
            return True
        scheme, sep, host = origin.partition("://")
        return bool(sep) and scheme == self.scheme and "/" not in host and self.host_allowed(host)

    def trusted(self, scope: Scope) -> bool:
        client = scope.get("client")
        return client is not None and client[0] in self._proxies

    def client_address(self, scope: Scope, headers: Headers) -> str:
        """The browser's address: the right-most X-Forwarded-For hop not in trusted_proxies
        when a trusted proxy sent it, or the peer address."""
        client = scope.get("client")
        peer = client[0] if client is not None else ""
        if not self.trusted(scope):
            return peer
        forwarded = headers.get("x-forwarded-for", "")
        for hop in reversed(_hops(forwarded)):
            if hop not in self._proxies:
                return hop
        return peer


class HostCheck:
    """Refuses a request whose Host is not the app's (400 `bad_host`), or whose Origin is
    another website (403 `bad_origin`). Pure ASGI, so streamed responses pass unbuffered."""

    def __init__(self, app: ASGIApp, rules: HostRules) -> None:
        self.app = app
        self.rules = rules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if self.rules.trusted(scope):
            host = _last(headers.get("x-forwarded-host", "")) or host
            proto = _last(headers.get("x-forwarded-proto", "")).lower()
            scheme = proto if proto in ("http", "https") else scope["scheme"]
            scope = _with_host(scope, host, scheme)
        path = scope.get("path", "").rstrip("/")
        is_heartbeat = scope.get("method") == "GET" and path == "/api/health/heartbeat"
        if not host or not self._is_host_allowed(scope, host, is_heartbeat=is_heartbeat):
            refusal = error_response(
                400,
                "bad_host",
                "This address is not allowed. Open the app at the address it printed.",
            )
            await refusal(scope, receive, send)
            return
        origin = headers.get("origin")
        if not is_heartbeat and origin is not None and not self.rules.origin_allowed(origin):
            refusal = error_response(
                403,
                "bad_origin",
                "This request came from another website and was refused. "
                "Open the app at the address it printed.",
            )
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _is_host_allowed(self, scope: Scope, host: str, *, is_heartbeat: bool) -> bool:
        if self.rules.host_allowed(host):
            return True
        if not is_heartbeat:
            return False
        name, _ = _split(host)
        app = scope.get("app")
        if app is None:
            return False
        services = getattr(getattr(app, "state", None), "services", None)
        if services is None:
            return False
        integration_settings = getattr(services, "integration_settings", None)
        if integration_settings is None:
            return False
        cached_host = integration_settings.cached_public_url_host
        return bool(cached_host and name == cached_host.lower())


def _with_host(scope: Scope, host: str, scheme: str) -> Scope:
    """A copy of `scope` as the browser sent it to the proxy, so URLs the app builds match."""
    headers = [(key, value) for key, value in scope["headers"] if key != b"host"]
    headers.append((b"host", host.encode("latin-1")))
    return {**scope, "headers": headers, "scheme": scheme}
