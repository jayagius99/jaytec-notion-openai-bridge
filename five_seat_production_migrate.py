from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import psycopg2
import psycopg2.extras


MIGRATION_FLAG = "FIVE_SEAT_PROD_APPLY_MIGRATION"
EXPECTED_HOST_SHA_ENV = "FIVE_SEAT_PROD_EXPECTED_HOST_SHA256"
PRODUCTION_DATABASE = "jaytec_orchestration_prod"
MIGRATION_FILE = Path(__file__).with_name("five_seat_schema.sql")
MIGRATION_LOCK = "JAYTEC_FS08_PROD_SCHEMA_V1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ProductionMigrationRefused(RuntimeError):
    pass


def database_identity(database_url: str) -> dict[str, str | None]:
    parsed = urlparse(str(database_url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    database = unquote((parsed.path or "").lstrip("/")).strip()
    return {
        "scheme": parsed.scheme or None,
        "database_from_url": database or None,
        "host_sha256": (
            hashlib.sha256(host.encode("utf-8")).hexdigest()
            if host
            else None
        ),
    }


def safe_preflight_payload(database_url: str, *, migration_enabled: bool) -> dict[str, Any]:
    identity = database_identity(database_url)
    return {
        "migration_enabled": bool(migration_enabled),
        "database_from_url": identity["database_from_url"],
        "host_sha256": identity["host_sha256"],
        "expected_database": PRODUCTION_DATABASE,
    }


def assert_url_identity(database_url: str, expected_host_sha256: str) -> dict[str, str | None]:
    identity = database_identity(database_url)
    expected = str(expected_host_sha256 or "").strip().lower()
    if not HEX64.fullmatch(expected):
        raise ProductionMigrationRefused(
            "EXPECTED_HOST_SHA256_MISSING_OR_INVALID"
        )
    if identity["database_from_url"] != PRODUCTION_DATABASE:
        raise ProductionMigrationRefused(
            "DATABASE_NAME_MISMATCH:"
            + str(identity["database_from_url"])
        )
    if identity["host_sha256"] != expected:
        raise ProductionMigrationRefused("DATABASE_HOST_FINGERPRINT_MISMATCH")
    if identity["scheme"] not in {"postgres", "postgresql"}:
        raise ProductionMigrationRefused("DATABASE_SCHEME_INVALID")
    return identity


def apply_migration(database_url: str, expected_host_sha256: str) -> dict[str, Any]:
    identity = assert_url_identity(database_url, expected_host_sha256)

    with psycopg2.connect(database_url) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (MIGRATION_LOCK,),
            )
            cur.execute(
                """
                SELECT current_database() AS current_database,
                       to_regclass(current_schema() || '.execution_registry') IS NOT NULL
                         AS execution_registry,
                       to_regclass(current_schema() || '.jaytec_jobs') IS NOT NULL
                         AS jaytec_jobs,
                       to_regclass(current_schema() || '.jaytec_job_events') IS NOT NULL
                         AS jaytec_job_events
                """
            )
            markers = dict(cur.fetchone() or {})
            if markers.get("current_database") != PRODUCTION_DATABASE:
                raise ProductionMigrationRefused(
                    "CONNECTED_DATABASE_NAME_MISMATCH:"
                    + str(markers.get("current_database"))
                )
            missing = [
                key
                for key in ("execution_registry", "jaytec_jobs", "jaytec_job_events")
                if not bool(markers.get(key))
            ]
            if missing:
                raise ProductionMigrationRefused(
                    "CANONICAL_PRODUCTION_MARKERS_MISSING:" + ",".join(missing)
                )

            cur.execute(
                """
                SELECT count(*)::int AS active_jobs
                FROM jaytec_jobs
                WHERE status='RUNNING'
                   OR (
                     lease_expires_at IS NOT NULL
                     AND lease_expires_at > now()
                   )
                """
            )
            active_jobs = int((cur.fetchone() or {}).get("active_jobs") or 0)
            if active_jobs:
                raise ProductionMigrationRefused(
                    "ACTIVE_JOBS_PRESENT:" + str(active_jobs)
                )

            migration = MIGRATION_FILE.read_text(encoding="utf-8")
            cur.execute(migration)

            cur.execute(
                """
                SELECT array_agg(seat_id ORDER BY seat_id) AS seats
                FROM jaytec_worker_seats
                """
            )
            seats = list((cur.fetchone() or {}).get("seats") or [])
            expected_seats = [f"WORKER-SEAT-{index}" for index in range(1, 6)]
            if seats != expected_seats:
                raise ProductionMigrationRefused(
                    "EXACT_FIVE_SEAT_POSTCONDITION_FAILED"
                )

            cur.execute(
                """
                SELECT count(*)::int AS watch_rows
                FROM jaytec_watch_leader
                WHERE controller_id='WATCH'
                """
            )
            watch_rows = int((cur.fetchone() or {}).get("watch_rows") or 0)
            if watch_rows != 1:
                raise ProductionMigrationRefused(
                    "SINGLETON_WATCH_POSTCONDITION_FAILED:"
                    + str(watch_rows)
                )

    return {
        "status": "MIGRATION_APPLIED",
        "database": PRODUCTION_DATABASE,
        "host_sha256": identity["host_sha256"],
        "seats": expected_seats,
        "watch_rows": 1,
    }


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    enabled = os.environ.get(MIGRATION_FLAG, "0").strip() == "1"

    # Safe diagnostic only. Never prints credentials, username, password or host.
    print(
        "FIVE_SEAT_PROD_DB_PREFLIGHT="
        + json.dumps(
            safe_preflight_payload(database_url, migration_enabled=enabled),
            sort_keys=True,
        ),
        flush=True,
    )

    if not enabled:
        return
    if not database_url:
        raise ProductionMigrationRefused("DATABASE_URL_REQUIRED")

    result = apply_migration(
        database_url,
        os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
    )
    print(
        "FIVE_SEAT_PROD_MIGRATION="
        + json.dumps(result, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    main()
