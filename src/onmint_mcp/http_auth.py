"""Per-caller API credentials for the hosted (streamable-http) transport.

Over stdio the server is a single user's process and its credentials come from the
environment. Hosted, one process serves every caller, so the credentials MUST travel with
each request: the caller sends the same `x-api-key` / `x-api-secret` pair the on:mint API
already issues (POST /keys/mcp), and this module carries them from the ASGI layer down to
the tool that builds the HTTP client.

Why a ContextVar rather than threading an argument through every tool: the MCP tool
signature is the model-facing API. Adding two credential parameters would invite a model to
invent or echo them, and they would show up in transcripts and tool-call logs. The transport
is the right place for transport credentials.

WHY THIS IS SAFE ONLY IN STATELESS MODE — this is load-bearing, do not relax it. A ContextVar
set here is visible to the tool because the streamable-http manager spawns the per-request
server task from inside the request's own context, and a task inherits a copy of the context
that was current when it was spawned. In STATELESS mode a fresh transport and task are
created per request (`_handle_stateless_request`), so each tool call sees its own caller's
credentials. In SESSION mode the server task is started once, on the session's FIRST request,
and every later request is serviced by that same long-lived task — which froze its context
copy at request one. The second caller on a shared session would then act as the first.
`server.http_app()` therefore constructs the app with `stateless_http=True`, and
`assert_stateless()` below refuses to start otherwise.
"""
from contextvars import ContextVar
from typing import Optional, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

# (api_key, api_secret) for the request currently being served. Empty tuple = no HTTP
# request in scope, which is the normal state under stdio.
_caller_credentials: ContextVar[Tuple[Optional[str], Optional[str]]] = ContextVar(
    "onmint_caller_credentials", default=(None, None)
)

API_KEY_HEADER = "x-api-key"
API_SECRET_HEADER = "x-api-secret"


class CallerCredentialsMiddleware:
    """Pure-ASGI middleware that publishes the request's API credentials to the ContextVar.

    Deliberately pure-ASGI rather than BaseHTTPMiddleware: BaseHTTPMiddleware runs the
    downstream app in a separate task, which both breaks the ContextVar propagation this
    module depends on and buffers streaming responses — and streamable-http is a streaming
    transport.

    Missing or malformed headers are NOT rejected here. Authentication is the API's job, and
    the MCP protocol has unauthenticated exchanges (initialize, tools/list) that must keep
    working so a client can connect and discover tools before it has credentials. A tool that
    actually calls the API resolves credentials through `require_credentials()` and fails
    there with a message the caller can act on.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        key = secret = None
        for raw_name, raw_value in scope.get("headers") or []:
            name = raw_name.decode("latin-1").lower()
            if name == API_KEY_HEADER:
                key = raw_value.decode("latin-1")
            elif name == API_SECRET_HEADER:
                secret = raw_value.decode("latin-1")

        token = _caller_credentials.set((key, secret))
        try:
            await self.app(scope, receive, send)
        finally:
            _caller_credentials.reset(token)


def current_credentials() -> Tuple[Optional[str], Optional[str]]:
    """The credentials carried by the in-flight HTTP request, if any."""
    return _caller_credentials.get()


class MissingCredentialsError(ValueError):
    """Raised when a hosted tool call arrives without usable API credentials."""


def require_credentials() -> Tuple[str, str]:
    """Resolve the caller's credentials for a hosted tool call, or fail closed.

    There is no fallback to the process environment on purpose. A hosted deployment that
    silently borrowed a server-wide credential would let any caller who can reach the URL
    submit content, spend credits, and read provenance as that one tenant.
    """
    key, secret = current_credentials()
    if not key or not secret:
        raise MissingCredentialsError(
            "This on:mint MCP server is hosted and identifies you by your own API "
            f"credentials. Send `{API_KEY_HEADER}` and `{API_SECRET_HEADER}` headers with "
            "each request (issue a pair with POST /keys/mcp on the on:mint API, or set them "
            "as headers in your MCP client's server configuration)."
        )
    return key, secret


def assert_stateless(mcp) -> None:
    """Refuse to serve HTTP unless the server is stateless — see the module docstring."""
    if not mcp.settings.stateless_http:
        raise RuntimeError(
            "Refusing to start the hosted transport with stateless_http disabled: session "
            "mode services every request of a session from one long-lived task, so one "
            "caller's credentials would be reused for another's tool calls."
        )
