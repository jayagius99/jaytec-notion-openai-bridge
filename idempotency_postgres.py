from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import psycopg2
import psycopg2.extras


class PostgresIdempotencyError(RuntimeError):
    pass


@dataclass
class PostgresExecutionRegistry:
    """Durable idempotency store with ExecutionRegistry semantics.

    - Same idempotency_key + same packet_hash => return cached result
    - Same idempotency_key + different packet_hash (unexpired) => conflicting duplicate (atomic reject)
    - Expired row may be replaced

    NOTE: Caller maps conflicting duplicates to PacketValidationError("CONFLICTING_DUPLICATE").
    """

    database_url: str
    ttl_seconds: int

    def _connect(self):
        return psycopg2.connect(self.database_url)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS execution_registry (
                        idempotency_key TEXT PRIMARY KEY,
                        packet_hash TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    );
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS execution_registry_created_at_idx ON execution_registry(created_at);"
                )

    def prune(self, *, now: Optional[datetime] = None) -> int:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = current.timestamp() - float(self.ttl_seconds)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM execution_registry WHERE EXTRACT(EPOCH FROM created_at) < %s",
                    (cutoff,),
                )
                return int(cur.rowcount or 0)

    def lookup(self, key: str, packet_hash: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = current.timestamp() - float(self.ttl_seconds)
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT idempotency_key, packet_hash, result_json, created_at
                    FROM execution_registry
                    WHERE idempotency_key = %s
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                created_at = row["created_at"]
                if created_at is None:
                    raise PostgresIdempotencyError("missing_created_at")
                if created_at.timestamp() < cutoff:
                    cur.execute("DELETE FROM execution_registry WHERE idempotency_key = %s", (key,))
                    return None
                if row["packet_hash"] != packet_hash:
                    raise ValueError("CONFLICTING_DUPLICATE")
                import json

                return copy.deepcopy(json.loads(row["result_json"]))

    def store(
        self,
        key: str,
        packet_hash: str,
        result: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """Atomically store unless an unexpired conflicting duplicate exists."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = current.timestamp() - float(self.ttl_seconds)
        import json

        payload = json.dumps(dict(result), ensure_ascii=False, sort_keys=True)

        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Lock existing row (if any) to avoid concurrent overwrites.
                cur.execute(
                    """
                    SELECT packet_hash, created_at
                    FROM execution_registry
                    WHERE idempotency_key = %s
                    FOR UPDATE
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if row is not None:
                    existing_hash = row.get("packet_hash")
                    created_at = row.get("created_at")
                    if created_at is None:
                        raise PostgresIdempotencyError("missing_created_at")
                    expired = created_at.timestamp() < cutoff
                    if not expired and existing_hash != packet_hash:
                        raise ValueError("CONFLICTING_DUPLICATE")
                    # same-hash OR expired: replace
                    cur.execute(
                        """
                        UPDATE execution_registry
                        SET packet_hash = %s,
                            result_json = %s,
                            created_at = %s
                        WHERE idempotency_key = %s
                        """,
                        (packet_hash, payload, current, key),
                    )
                    return

                cur.execute(
                    """
                    INSERT INTO execution_registry (idempotency_key, packet_hash, result_json, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (key, packet_hash, payload, current),
                )
