"""Short-lived, durable, single-use authorization for staging startup probes."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol, Any


AUTH_ID_ENV = "JAYTEC_STARTUP_PROBE_AUTH_ID"
AUTH_PROBE_ENV = "JAYTEC_STARTUP_PROBE_AUTH_PROBE"
AUTH_EXPIRES_AT_ENV = "JAYTEC_STARTUP_PROBE_AUTH_EXPIRES_AT"
AUTH_PAYLOAD_SHA256_ENV = "JAYTEC_STARTUP_PROBE_AUTH_PAYLOAD_SHA256"
MAX_AUTH_LIFETIME_SECONDS = 15 * 60

_AUTH_ID_RE = re.compile(r"^[a-f0-9]{32,128}$")
_PROBE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class OneShotClaimRegistry(Protocol):
    def claim_once(
        self,
        key: str,
        packet_hash: str,
        result: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> bool:
        ...


@dataclass(frozen=True)
class StartupProbeAuthorization:
    allowed: bool
    reason: str
    probe_name: str
    payload_sha256: str


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def authorize_startup_probe(
    *,
    env: Mapping[str, str],
    registry: OneShotClaimRegistry,
    idempotency_store: str,
    probe_name: str,
    payload_sha256: str,
    now_epoch: int | None = None,
) -> StartupProbeAuthorization:
    """Authorize one provider-bearing startup probe exactly once.

    The authorization is:
    - bound to one exact probe name,
    - bound to one exact payload digest,
    - valid for at most 15 minutes,
    - rejected without durable Postgres state,
    - consumed atomically so restart/redeploy cannot repeat the call.
    """
    if not isinstance(probe_name, str) or not _PROBE_RE.fullmatch(probe_name):
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_NAME_INVALID", str(probe_name), payload_sha256
        )
    if not isinstance(payload_sha256, str) or not _SHA256_RE.fullmatch(payload_sha256):
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_PAYLOAD_DIGEST_INVALID", probe_name, str(payload_sha256)
        )
    if idempotency_store != "postgres":
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_DURABLE_STATE_REQUIRED", probe_name, payload_sha256
        )
    claim_once = getattr(registry, "claim_once", None)
    if not callable(claim_once):
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_CLAIM_ONCE_UNAVAILABLE", probe_name, payload_sha256
        )

    auth_id = str(env.get(AUTH_ID_ENV, "") or "").strip()
    auth_probe = str(env.get(AUTH_PROBE_ENV, "") or "").strip()
    expires_raw = str(env.get(AUTH_EXPIRES_AT_ENV, "") or "").strip()
    auth_payload = str(env.get(AUTH_PAYLOAD_SHA256_ENV, "") or "").strip()

    if not _AUTH_ID_RE.fullmatch(auth_id):
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_ID_INVALID", probe_name, payload_sha256
        )
    if auth_probe != probe_name:
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_PROBE_MISMATCH", probe_name, payload_sha256
        )
    if auth_payload != payload_sha256:
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_PAYLOAD_MISMATCH", probe_name, payload_sha256
        )
    if not expires_raw.isdigit():
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_EXPIRY_INVALID", probe_name, payload_sha256
        )

    now_value = int(time.time()) if now_epoch is None else int(now_epoch)
    expires_at = int(expires_raw)
    if expires_at <= now_value:
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_EXPIRED", probe_name, payload_sha256
        )
    if expires_at - now_value > MAX_AUTH_LIFETIME_SECONDS:
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_LIFETIME_TOO_LONG", probe_name, payload_sha256
        )

    claim_payload = {
        "auth_id": auth_id,
        "expires_at": expires_at,
        "payload_sha256": payload_sha256,
        "probe_name": probe_name,
    }
    claim_hash = sha256_json(claim_payload)
    claim_key = "startup-probe-auth:" + auth_id
    try:
        claimed = bool(
            claim_once(
                claim_key,
                claim_hash,
                {
                    "status": "CONSUMED",
                    "probe_name": probe_name,
                    "payload_sha256": payload_sha256,
                    "expires_at": expires_at,
                },
                now=datetime.fromtimestamp(now_value, tz=timezone.utc),
            )
        )
    except Exception:
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_CLAIM_FAILED", probe_name, payload_sha256
        )
    if not claimed:
        return StartupProbeAuthorization(
            False, "STARTUP_PROBE_AUTH_REPLAY", probe_name, payload_sha256
        )

    return StartupProbeAuthorization(
        True, "STARTUP_PROBE_AUTHORIZED_ONCE", probe_name, payload_sha256
    )
