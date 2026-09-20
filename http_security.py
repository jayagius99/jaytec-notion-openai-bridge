"""Fail-closed HTTP Host/Origin guard configuration for JAYTEC MCP services."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


class HttpSecurityConfigError(RuntimeError):
    pass


HOSTS_ENV = "JAYTEC_MCP_ALLOWED_HOSTS_JSON"
ORIGINS_ENV = "JAYTEC_MCP_ALLOWED_ORIGINS_JSON"


@dataclass(frozen=True)
class HostOriginPolicy:
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]


def _json_string_list(raw: str, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise HttpSecurityConfigError(f"{field}_INVALID")
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HttpSecurityConfigError(f"{field}_INVALID_JSON") from exc
    if not isinstance(value, list):
        raise HttpSecurityConfigError(f"{field}_NOT_ARRAY")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise HttpSecurityConfigError(f"{field}_ENTRY_INVALID")
        if item in seen:
            raise HttpSecurityConfigError(f"{field}_DUPLICATE")
        seen.add(item)
        out.append(item)
    return tuple(out)


def _validate_host(host: str) -> str:
    if (
        host == "*"
        or "://" in host
        or "/" in host
        or "@" in host
        or any(ch.isspace() for ch in host)
    ):
        raise HttpSecurityConfigError("HTTP_ALLOWED_HOST_INVALID")
    if len(host) > 253:
        raise HttpSecurityConfigError("HTTP_ALLOWED_HOST_INVALID")
    return host.casefold()


def _validate_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.port not in (None, 443)
    ):
        raise HttpSecurityConfigError("HTTP_ALLOWED_ORIGIN_INVALID")
    return f"https://{parsed.hostname.casefold()}"


def load_host_origin_policy(
    env: Mapping[str, str],
    *,
    require_hosts: bool = True,
) -> HostOriginPolicy:
    hosts = tuple(
        _validate_host(value)
        for value in _json_string_list(env.get(HOSTS_ENV, ""), field=HOSTS_ENV)
    )
    origins = tuple(
        _validate_origin(value)
        for value in _json_string_list(env.get(ORIGINS_ENV, ""), field=ORIGINS_ENV)
    )
    if require_hosts and not hosts:
        raise HttpSecurityConfigError("HTTP_ALLOWED_HOSTS_REQUIRED")
    return HostOriginPolicy(
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
