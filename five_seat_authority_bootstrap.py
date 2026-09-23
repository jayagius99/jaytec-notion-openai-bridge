from __future__ import annotations

import json
import os
from typing import Any

import psycopg2
import psycopg2.extras

from five_seat_production_migrate import (
    EXPECTED_HOST_SHA_ENV,
    PRODUCTION_DATABASE,
    assert_url_identity,
)


BOOTSTRAP_FLAG = "FIVE_SEAT_PROD_BOOTSTRAP_AUTHORITY"
EXPECTED_VERSION_ENV = "FIVE_SEAT_PROD_BOOTSTRAP_EXPECTED_SOURCE_VERSION"
BOOTSTRAP_LOCK = "JAYTEC_FS08_AUTHORITY_BOOTSTRAP_V1"


class ProductionAuthorityBootstrapRefused(RuntimeError):
    pass


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_bootstrap_evidence(database_url: str) -> dict[str, Any]:
    with psycopg2.connect(database_url) as conn:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT current_database() AS current_database,
                       to_regclass(current_schema() || '.jaytec_jobs') IS NOT NULL
                         AS jaytec_jobs,
                       to_regclass(current_schema() || '.jaytec_fabric_authority_state') IS NOT NULL
                         AS authority_state,
                       to_regclass(current_schema() || '.jaytec_worker_seats') IS NOT NULL
                         AS worker_seats
                """
            )
            markers = dict(cur.fetchone() or {})
            if markers.get("current_database") != PRODUCTION_DATABASE:
                raise ProductionAuthorityBootstrapRefused(
                    "CONNECTED_DATABASE_NAME_MISMATCH"
                )
            if not all(
                bool(markers.get(key))
                for key in ("jaytec_jobs", "authority_state", "worker_seats")
            ):
                raise ProductionAuthorityBootstrapRefused(
                    "REQUIRED_FABRIC_MARKERS_MISSING"
                )

            cur.execute(
                """
                SELECT current_shared_state_version,authority_epoch,fence_token,
                       updated_by,updated_at
                FROM jaytec_fabric_authority_state
                WHERE authority_id='FABRIC'
                """
            )
            authority = dict(cur.fetchone() or {})
            if not authority:
                raise ProductionAuthorityBootstrapRefused(
                    "FABRIC_AUTHORITY_ROW_MISSING"
                )

            cur.execute(
                """
                SELECT
                  max(source_shared_state_version) FILTER (
                    WHERE source_shared_state_version > 0
                      AND assignment_type <> 'FIVE_SEAT_FABRIC'
                  ) AS durable_max_nonfabric_source_version,
                  count(*) FILTER (
                    WHERE source_shared_state_version > 0
                      AND assignment_type <> 'FIVE_SEAT_FABRIC'
                  )::int AS durable_nonfabric_version_rows,
                  count(*) FILTER (
                    WHERE assignment_type='FIVE_SEAT_FABRIC'
                      AND status IN ('QUEUED','RUNNING','PAUSED')
                  )::int AS active_fabric_jobs
                FROM jaytec_jobs
                """
            )
            durable = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT
                  count(*)::int AS seats_total,
                  count(*) FILTER (
                    WHERE state='FREE'
                      AND current_job_id IS NULL
                      AND lease_owner IS NULL
                      AND lease_expires_at IS NULL
                  )::int AS seats_free
                FROM jaytec_worker_seats
                """
            )
            seats = dict(cur.fetchone() or {})

    return {
        "current_shared_state_version": _safe_int(
            authority.get("current_shared_state_version")
        ),
        "authority_epoch": _safe_int(authority.get("authority_epoch")),
        "authority_fence_token": _safe_int(authority.get("fence_token")),
        "durable_max_nonfabric_source_version": _safe_int(
            durable.get("durable_max_nonfabric_source_version")
        ),
        "durable_nonfabric_version_rows": _safe_int(
            durable.get("durable_nonfabric_version_rows")
        ),
        "active_fabric_jobs": _safe_int(durable.get("active_fabric_jobs")),
        "seats_total": _safe_int(seats.get("seats_total")),
        "seats_free": _safe_int(seats.get("seats_free")),
    }


def validate_bootstrap_evidence(
    evidence: dict[str, Any],
    expected_source_version: int,
) -> None:
    expected = int(expected_source_version)
    if expected <= 0:
        raise ProductionAuthorityBootstrapRefused(
            "EXPECTED_SOURCE_VERSION_INVALID"
        )
    current = _safe_int(evidence.get("current_shared_state_version"))
    durable_max = _safe_int(
        evidence.get("durable_max_nonfabric_source_version")
    )
    if current not in {0, expected}:
        raise ProductionAuthorityBootstrapRefused(
            "AUTHORITY_ALREADY_INITIALIZED_DIFFERENT_VERSION"
        )
    if durable_max != expected:
        raise ProductionAuthorityBootstrapRefused(
            "DURABLE_SOURCE_VERSION_MISMATCH"
        )
    if _safe_int(evidence.get("durable_nonfabric_version_rows")) <= 0:
        raise ProductionAuthorityBootstrapRefused(
            "NO_DURABLE_SOURCE_VERSION_EVIDENCE"
        )
    if _safe_int(evidence.get("active_fabric_jobs")) != 0:
        raise ProductionAuthorityBootstrapRefused(
            "ACTIVE_FABRIC_JOBS_PRESENT"
        )
    if _safe_int(evidence.get("seats_total")) != 5:
        raise ProductionAuthorityBootstrapRefused("EXACT_FIVE_SEATS_REQUIRED")
    if _safe_int(evidence.get("seats_free")) != 5:
        raise ProductionAuthorityBootstrapRefused("ALL_FIVE_SEATS_MUST_BE_FREE")


def bootstrap_authority(
    database_url: str,
    *,
    expected_host_sha256: str,
    expected_source_version: int,
) -> dict[str, Any]:
    identity = assert_url_identity(database_url, expected_host_sha256)
    evidence = read_bootstrap_evidence(database_url)
    validate_bootstrap_evidence(evidence, expected_source_version)
    expected = int(expected_source_version)

    if _safe_int(evidence.get("current_shared_state_version")) == expected:
        return {
            "status": "ALREADY_INITIALIZED",
            "source_shared_state_version": expected,
            "host_sha256": identity["host_sha256"],
        }

    with psycopg2.connect(database_url) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (BOOTSTRAP_LOCK,),
            )
            cur.execute(
                """
                SELECT current_shared_state_version,authority_epoch,fence_token
                FROM jaytec_fabric_authority_state
                WHERE authority_id='FABRIC'
                FOR UPDATE
                """
            )
            row = dict(cur.fetchone() or {})
            if not row:
                raise ProductionAuthorityBootstrapRefused(
                    "FABRIC_AUTHORITY_ROW_MISSING"
                )
            if _safe_int(row.get("current_shared_state_version")) != 0:
                raise ProductionAuthorityBootstrapRefused(
                    "AUTHORITY_CHANGED_DURING_BOOTSTRAP"
                )

            cur.execute(
                """
                SELECT
                  max(source_shared_state_version) FILTER (
                    WHERE source_shared_state_version > 0
                      AND assignment_type <> 'FIVE_SEAT_FABRIC'
                  ) AS durable_max_nonfabric_source_version,
                  count(*) FILTER (
                    WHERE assignment_type='FIVE_SEAT_FABRIC'
                      AND status IN ('QUEUED','RUNNING','PAUSED')
                  )::int AS active_fabric_jobs
                FROM jaytec_jobs
                """
            )
            live = dict(cur.fetchone() or {})
            if _safe_int(
                live.get("durable_max_nonfabric_source_version")
            ) != expected:
                raise ProductionAuthorityBootstrapRefused(
                    "DURABLE_SOURCE_VERSION_CHANGED"
                )
            if _safe_int(live.get("active_fabric_jobs")) != 0:
                raise ProductionAuthorityBootstrapRefused(
                    "ACTIVE_FABRIC_JOBS_PRESENT"
                )

            cur.execute(
                """
                UPDATE jaytec_fabric_authority_state
                SET current_shared_state_version=%s,
                    authority_epoch=authority_epoch+1,
                    fence_token=fence_token+1,
                    updated_by='FS08_DURABLE_AUTHORITY_BOOTSTRAP',
                    updated_at=now()
                WHERE authority_id='FABRIC'
                  AND current_shared_state_version=0
                RETURNING current_shared_state_version,authority_epoch,fence_token
                """,
                (expected,),
            )
            updated = dict(cur.fetchone() or {})
            if _safe_int(updated.get("current_shared_state_version")) != expected:
                raise ProductionAuthorityBootstrapRefused(
                    "AUTHORITY_BOOTSTRAP_COMPARE_AND_SET_FAILED"
                )

            cur.execute(
                """
                INSERT INTO jaytec_job_events(
                  job_id,event_type,source,source_version,payload
                ) VALUES (
                  NULL,'FABRIC_AUTHORITY_BOOTSTRAPPED',
                  'FIVE_SEAT_AUTHORITY_BOOTSTRAP',%s,%s::jsonb
                )
                """,
                (
                    expected,
                    json.dumps(
                        {
                            "mode": "DURABLE_NONFABRIC_MAX_COMPARE_AND_SET",
                            "previous_source_shared_state_version": 0,
                            "new_source_shared_state_version": expected,
                        },
                        sort_keys=True,
                    ),
                ),
            )

    return {
        "status": "AUTHORITY_BOOTSTRAPPED",
        "source_shared_state_version": expected,
        "authority_epoch": _safe_int(updated.get("authority_epoch")),
        "authority_fence_token": _safe_int(updated.get("fence_token")),
        "host_sha256": identity["host_sha256"],
    }


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    enabled = os.environ.get(BOOTSTRAP_FLAG, "0").strip() == "1"

    if not database_url:
        print(
            "FIVE_SEAT_AUTHORITY_PREFLIGHT="
            + json.dumps(
                {
                    "bootstrap_enabled": enabled,
                    "error_code": "DATABASE_URL_REQUIRED",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if enabled:
            raise ProductionAuthorityBootstrapRefused("DATABASE_URL_REQUIRED")
        return

    evidence = read_bootstrap_evidence(database_url)
    print(
        "FIVE_SEAT_AUTHORITY_PREFLIGHT="
        + json.dumps(
            {"bootstrap_enabled": enabled, **evidence},
            sort_keys=True,
            default=str,
        ),
        flush=True,
    )

    if not enabled:
        return

    expected_text = os.environ.get(EXPECTED_VERSION_ENV, "").strip()
    if not expected_text.isdigit() or int(expected_text) <= 0:
        raise ProductionAuthorityBootstrapRefused(
            "EXPECTED_SOURCE_VERSION_MISSING_OR_INVALID"
        )

    result = bootstrap_authority(
        database_url,
        expected_host_sha256=os.environ.get(EXPECTED_HOST_SHA_ENV, ""),
        expected_source_version=int(expected_text),
    )
    print(
        "FIVE_SEAT_AUTHORITY_BOOTSTRAP="
        + json.dumps(result, sort_keys=True, default=str),
        flush=True,
    )


if __name__ == "__main__":
    main()
