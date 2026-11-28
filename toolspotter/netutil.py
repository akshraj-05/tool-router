"""Local-only helpers: loopback detection and the Host header check."""

from __future__ import annotations

import ipaddress


def is_loopback(host: str) -> bool:
    """True for ``localhost``, 127.0.0.0/8, ``::1`` and IPv4-mapped loopback.

    Anything unparseable (including the wildcards ``0.0.0.0`` and ``::``) is False,
    so "bind to every interface" is never mistaken for "local only".
    """
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback


def host_name(header: str) -> str:
    """The host part of a ``Host`` header: no port, no brackets, lower case.

    ``localhost:8080`` -> ``localhost``; ``[::1]:8080`` -> ``::1``. A malformed
    value gives ``""``, which matches nothing.
    """
    value = header.strip().lower()
    if value.startswith("["):
        name, closed, rest = value[1:].partition("]")
        if not closed or (rest and not (rest.startswith(":") and rest[1:].isdigit())):
            return ""
        return name
    name, colon, port = value.partition(":")
    if colon and not port.isdigit():
        return ""
    return name.rstrip(".")
