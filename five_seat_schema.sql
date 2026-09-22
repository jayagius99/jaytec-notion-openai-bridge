-- JAYTEC Five-Seat Continuous Worker Fabric V1
-- FS02 additive migration only. This migration extends the existing durable
-- job runtime; it does not create a second queue or replace jaytec_jobs.
--
-- IMPORTANT: runtime startup must not apply this file automatically.
-- Apply only through the tracked database migration path after review.

ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS seat_id TEXT;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS fabric_state TEXT;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS collision_key TEXT;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS task_packet_hash TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'jaytec_jobs_fabric_state_check'
  ) THEN
    ALTER TABLE jaytec_jobs
      ADD CONSTRAINT jaytec_jobs_fabric_state_check
      CHECK (
        fabric_state IS NULL OR fabric_state IN (
          'INTAKE',
          'QUEUED',
          'CLAIMED',
          'RUNNING',
          'HANDOFF_PENDING_REVIEW',
          'REVIEWING',
          'SUCCEEDED',
          'BLOCKED_OWNER',
          'BLOCKED_DEPENDENCY',
          'FAILED_SAFE',
          'REWORK_QUEUED',
          'QUARANTINED',
          'CANCEL_REQUESTED',
          'CANCELLED',
          'STALE',
          'SUPERSEDED',
          'ESCALATED'
        )
      );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS jaytec_worker_seats (
  seat_id TEXT PRIMARY KEY
    CHECK (seat_id IN (
      'WORKER-SEAT-1',
      'WORKER-SEAT-2',
      'WORKER-SEAT-3',
      'WORKER-SEAT-4',
      'WORKER-SEAT-5'
    )),
  state TEXT NOT NULL DEFAULT 'FREE'
    CHECK (state IN (
      'FREE',
      'CLAIMED',
      'RUNNING',
      'HANDOFF_WRITTEN',
      'RELEASING',
      'SUSPECT',
      'RECONCILING',
      'QUARANTINED'
    )),
  current_job_id TEXT UNIQUE REFERENCES jaytec_jobs(job_id) ON DELETE SET NULL,
  worker_id TEXT,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  seat_epoch BIGINT NOT NULL DEFAULT 0,
  fence_token BIGINT NOT NULL DEFAULT 0,
  last_handoff_ref TEXT,
  version BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO jaytec_worker_seats(seat_id)
VALUES
  ('WORKER-SEAT-1'),
  ('WORKER-SEAT-2'),
  ('WORKER-SEAT-3'),
  ('WORKER-SEAT-4'),
  ('WORKER-SEAT-5')
ON CONFLICT (seat_id) DO NOTHING;

CREATE UNIQUE INDEX IF NOT EXISTS jaytec_jobs_one_running_job_per_seat_idx
  ON jaytec_jobs(seat_id)
  WHERE seat_id IS NOT NULL AND status='RUNNING';

CREATE INDEX IF NOT EXISTS jaytec_worker_seats_state_idx
  ON jaytec_worker_seats(state, seat_id);

CREATE INDEX IF NOT EXISTS jaytec_worker_seats_lease_idx
  ON jaytec_worker_seats(lease_expires_at)
  WHERE state <> 'FREE';
