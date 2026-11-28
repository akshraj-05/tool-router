"""Safety rules for scanning a REMOTE MCP server (Streamable HTTP).

Authentication is limited to static HTTP headers, given as
``Header-Name=ENV_VARIABLE``: an API key, ``Authorization: Bearer …`` or ``Basic …``,
or several of them. OAuth is not supported.

Rules enforced here
- ``https`` only; plain ``http`` is accepted for loopback addresses, for local testing.
- No user name or password inside the URL.
- Secret values come from environment variables, never from the command line.
- Anything shown to a person has the URL's query string and secret values removed.
- Requests only ever go to the server you named (same scheme, host and port; an
  upgrade from http to https on default ports also counts), so a redirect cannot
  carry credentials to another server. The MCP SDK enforces this when following
  redirects; ``origin_guard`` checks every request again so it does not depend on
  the SDK alone. A redirect that stays on the same server is followed.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from tool_router.domain.errors import ToolRouterError

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# Set by the HTTP client or the MCP protocol; overriding them would break the session.
_RESERVED_HEADERS = frozenset(
    "accept connection content-length content-type host mcp-protocol-version mcp-session-id "
    "transfer-encoding".split()
)


class MCPImportError(ToolRouterError):
    """An MCP server could not be reached or its tools could not be listed."""


def is_url(source: str) -> bool:
    """True when an ``--import-mcp`` value is a URL rather than a command."""
    return bool(_URL.match(source.strip()))


def redact_url(url: str) -> str:
    """The URL without credentials, query string or fragment — safe to print."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        shown = urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<unreadable URL>"
    return shown + ("?…" if parts.query else "")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> str:
    """Return the URL if it is acceptable, otherwise raise ``MCPImportError``."""
    url = url.strip()
    shown = redact_url(url)
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port      # reading .port validates it
    except ValueError as exc:
        raise MCPImportError(f"not a valid URL: {shown}") from exc
    if parts.scheme not in ("http", "https"):
        raise MCPImportError(f"unsupported URL scheme in {shown} (use https://)")
    if not host:
        raise MCPImportError(f"the URL {shown} has no host name")
    if parts.username is not None or parts.password is not None:
        raise MCPImportError(
            f"do not put a user name or password in the URL ({shown}); use --mcp-header-env"
        )
    if parts.scheme == "http" and not _is_loopback(host):
        raise MCPImportError(
            f"refusing plain http for {shown}: use https (http is allowed for localhost only)"
        )
    return url


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def same_server(endpoint: str, target: str) -> bool:
    """May a request meant for ``endpoint`` (with its credentials) be sent to ``target``?"""
    scheme, host, port = _origin(endpoint)
    new_scheme, new_host, new_port = _origin(target)
    if (scheme, host, port) == (new_scheme, new_host, new_port):
        return True
    return host == new_host and (scheme, port, new_scheme, new_port) == ("http", 80, "https", 443)


def origin_guard(endpoint: str):
    """An httpx request hook that blocks any request leaving the endpoint's server."""

    async def check(request) -> None:
        if not same_server(endpoint, str(request.url)):
            raise MCPImportError(
                f"{redact_url(endpoint)} tried to send the request to another server "
                f"({redact_url(str(request.url))}); blocked"
            )

    return check


def _secret(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise MCPImportError(f"environment variable {variable} is not set or empty")
    if not value.isascii() or any(c in value for c in "\r\n\0"):
        raise MCPImportError(f"environment variable {variable} has characters a header cannot carry")
    return value


def build_headers(header_envs: Optional[list[str]] = None) -> dict[str, str]:
    """Authentication headers, with every value read from the environment.

    Each item is ``Header-Name=ENV_VARIABLE``. The variable holds the complete header
    value, so for a bearer token it contains ``Bearer <token>``.
    """
    headers: dict[str, str] = {}
    for item in header_envs or []:
        name, separator, variable = item.partition("=")
        name, variable = name.strip(), variable.strip()
        # The item is not echoed back: someone may have typed a secret in it.
        if not separator or not _HEADER_NAME.match(name) or not re.match(r"^[A-Za-z_]\w*$", variable):
            raise MCPImportError(
                "a header option must look like Header-Name=ENV_VARIABLE "
                "(give the variable's NAME, not its value)"
            )
        if name.lower() in _RESERVED_HEADERS:
            raise MCPImportError(f"the header {name!r} is managed by the protocol and cannot be set")
        if name.lower() in {h.lower() for h in headers}:
            raise MCPImportError(f"the header {name!r} is given more than once")
        headers[name] = _secret(variable)
    return headers


def auth_header(name: str, token: str) -> dict[str, str]:
    """One authentication header from a token typed by the user.

    For ``Authorization`` a bare token is sent as ``Bearer <token>``; a value that
    already names its scheme (``Basic ...``) is sent as it is. Errors never repeat
    the token.
    """
    name, token = name.strip(), token.strip()
    if not _HEADER_NAME.match(name):
        raise MCPImportError("that is not a valid header name")
    if name.lower() in _RESERVED_HEADERS:
        raise MCPImportError(f"the header {name!r} is managed by the protocol and cannot be set")
    if not token or not token.isascii() or any(c in token for c in "\r\n\0"):
        raise MCPImportError("the token is empty or has characters a header cannot carry")
    if name.lower() == "authorization" and " " not in token:
        token = f"Bearer {token}"
    return {name: token}


def scrub(text: str, url: str, headers: dict[str, str]) -> str:
    """Remove the URL's query string and every secret value from a message."""
    text = text.replace(url, redact_url(url))
    query = urlsplit(url).query
    if query:
        text = text.replace(query, "…")
    for value in headers.values():
        for secret in (value, value.partition(" ")[2]):
            if len(secret) >= 4:
                text = text.replace(secret, "***")
    return text
