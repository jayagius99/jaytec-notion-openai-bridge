from __future__ import annotations

import json
import select
from typing import Optional

import psycopg2
import psycopg2.extensions


WORK_AVAILABLE_CHANNEL = "jaytec_five_seat_work"
REVIEW_AVAILABLE_CHANNEL = "jaytec_five_seat_review"
ALLOWED_CHANNELS = frozenset({WORK_AVAILABLE_CHANNEL, REVIEW_AVAILABLE_CHANNEL})


class FabricSignalError(RuntimeError):
    pass


def _channel(value: str) -> str:
    name = str(value or "").strip()
    if name not in ALLOWED_CHANNELS:
        raise ValueError("unsupported fabric signal channel")
    return name


class PostgresFabricSignal:
    """Best-effort wakeup accelerator with bounded timeout fallback.

    Durable Postgres rows remain authority. NOTIFY only reduces idle latency;
    losing a notification can never lose work because workers always re-check
    the durable queue after the bounded wait.
    """

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def notify(
        self,
        channel: str,
        *,
        reason: str,
        job_id: Optional[str] = None,
    ) -> None:
        channel = _channel(channel)
        payload = json.dumps(
            {
                "reason": str(reason or "")[:120],
                "job_id": str(job_id or "")[:200],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with psycopg2.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_notify(%s,%s)", (channel, payload))

    def wait(self, channel: str, *, timeout_seconds: float = 15.0) -> bool:
        channel = _channel(channel)
        timeout = max(0.1, min(float(timeout_seconds), 60.0))
        conn = psycopg2.connect(self.database_url)
        try:
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            try:
                # Channel is allowlisted above; identifier is not user-controlled.
                cur.execute("LISTEN " + channel)
                ready, _, _ = select.select([conn], [], [], timeout)
                if not ready:
                    return False
                conn.poll()
                observed = bool(conn.notifies)
                while conn.notifies:
                    conn.notifies.pop(0)
                return observed
            finally:
                cur.close()
        finally:
            conn.close()
