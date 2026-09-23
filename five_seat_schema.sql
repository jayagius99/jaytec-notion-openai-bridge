-- JAYTEC Five-Seat Continuous Worker Fabric V1
-- FS02-FS04 additive migration only. Extends the existing durable job runtime.
-- Runtime startup MUST NOT apply this file automatically.

ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS seat_id TEXT;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS fabric_state TEXT;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS collision_key TEXT;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS task_packet_hash TEXT;

ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS fabric_attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS fabric_max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS fabric_rework_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS fabric_max_reworks INTEGER NOT NULL DEFAULT 2;
ALTER TABLE jaytec_jobs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='jaytec_jobs_fabric_state_check'
  ) THEN
    ALTER TABLE jaytec_jobs
      ADD CONSTRAINT jaytec_jobs_fabric_state_check
      CHECK (
        fabric_state IS NULL OR fabric_state IN (
          'INTAKE','QUEUED','CLAIMED','RUNNING','HANDOFF_PENDING_REVIEW','REVIEWING',
          'SUCCEEDED','BLOCKED_OWNER','BLOCKED_DEPENDENCY','FAILED_SAFE','REWORK_QUEUED',
          'QUARANTINED','CANCEL_REQUESTED','CANCELLED','STALE','SUPERSEDED','ESCALATED'
        )
      );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS jaytec_worker_seats (
  seat_id TEXT PRIMARY KEY CHECK (
    seat_id IN (
      'WORKER-SEAT-1','WORKER-SEAT-2','WORKER-SEAT-3','WORKER-SEAT-4','WORKER-SEAT-5'
    )
  ),
  state TEXT NOT NULL DEFAULT 'FREE' CHECK (
    state IN (
      'FREE','CLAIMED','RUNNING','HANDOFF_WRITTEN','RELEASING',
      'SUSPECT','RECONCILING','QUARANTINED'
    )
  ),
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
  ON jaytec_worker_seats(state,seat_id);

CREATE INDEX IF NOT EXISTS jaytec_worker_seats_lease_idx
  ON jaytec_worker_seats(lease_expires_at)
  WHERE state <> 'FREE';

-- Admission freshness snapshot. This is a guard projection, not a second source of truth.
CREATE TABLE IF NOT EXISTS jaytec_fabric_authority_state (
  authority_id TEXT PRIMARY KEY CHECK (authority_id='FABRIC'),
  current_shared_state_version BIGINT NOT NULL DEFAULT 0
    CHECK (current_shared_state_version >= 0),
  authority_epoch BIGINT NOT NULL DEFAULT 0,
  fence_token BIGINT NOT NULL DEFAULT 0,
  updated_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO jaytec_fabric_authority_state(authority_id)
VALUES ('FABRIC')
ON CONFLICT (authority_id) DO NOTHING;

-- Immutable generic assignment envelope. jaytec_jobs remains state authority.
CREATE TABLE IF NOT EXISTS jaytec_fabric_envelopes (
  job_id TEXT PRIMARY KEY REFERENCES jaytec_jobs(job_id) ON DELETE RESTRICT,
  envelope_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  worker_kind TEXT NOT NULL,
  required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  approval_required BOOLEAN NOT NULL DEFAULT FALSE,
  authority_class TEXT NOT NULL CHECK (
    authority_class IN (
      'READ_ONLY','SCOPED_MUTATION','CANONICAL_SHARED',
      'EXTERNAL_SIDE_EFFECT','GLOBAL_EXCLUSIVE','OWNER_GATED'
    )
  ),
  cost_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_standard JSONB NOT NULL DEFAULT '{}'::jsonb,
  stop_conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_destination JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_fabric_envelopes_worker_idx
  ON jaytec_fabric_envelopes(worker_kind,job_id);

ALTER TABLE jaytec_fabric_envelopes
  ADD COLUMN IF NOT EXISTS approval_required BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE jaytec_fabric_envelopes
SET approval_required=TRUE
WHERE authority_class IN (
  'CANONICAL_SHARED','EXTERNAL_SIDE_EFFECT','GLOBAL_EXCLUSIVE','OWNER_GATED'
);

CREATE TABLE IF NOT EXISTS jaytec_fabric_approvals (
  approval_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jaytec_jobs(job_id) ON DELETE RESTRICT,
  envelope_hash TEXT NOT NULL,
  source_shared_state_version BIGINT NOT NULL,
  authority_class TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approval_ref TEXT NOT NULL,
  max_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (max_cost_usd >= 0),
  state TEXT NOT NULL CHECK (state IN ('APPROVED','REVOKED')),
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS jaytec_fabric_approvals_one_active_idx
  ON jaytec_fabric_approvals(job_id)
  WHERE state='APPROVED';

CREATE INDEX IF NOT EXISTS jaytec_fabric_approvals_state_idx
  ON jaytec_fabric_approvals(state,expires_at,job_id);

-- Durable worker-kind circuit state prevents provider/adapter retry storms.
CREATE TABLE IF NOT EXISTS jaytec_fabric_circuits (
  worker_kind TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'CLOSED'
    CHECK (state IN ('CLOSED','OPEN','HALF_OPEN')),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  failure_threshold INTEGER NOT NULL DEFAULT 3
    CHECK (failure_threshold BETWEEN 1 AND 20),
  open_until TIMESTAMPTZ,
  probe_job_id TEXT REFERENCES jaytec_jobs(job_id) ON DELETE SET NULL,
  probe_started_at TIMESTAMPTZ,
  last_failure JSONB,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_fabric_circuits_open_idx
  ON jaytec_fabric_circuits(state,open_until);

ALTER TABLE jaytec_fabric_circuits
  ADD COLUMN IF NOT EXISTS probe_job_id TEXT REFERENCES jaytec_jobs(job_id) ON DELETE SET NULL;
ALTER TABLE jaytec_fabric_circuits
  ADD COLUMN IF NOT EXISTS probe_started_at TIMESTAMPTZ;

-- Worker output becomes immutable candidate evidence before the seat is freed.
CREATE TABLE IF NOT EXISTS jaytec_worker_handoffs (
  handoff_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jaytec_jobs(job_id) ON DELETE RESTRICT,
  seat_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  ownership_epoch BIGINT NOT NULL,
  job_fence_token BIGINT NOT NULL,
  seat_epoch BIGINT NOT NULL,
  seat_fence_token BIGINT NOT NULL,
  task_packet_hash TEXT,
  handoff_digest TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(job_id,ownership_epoch,job_fence_token)
);

CREATE INDEX IF NOT EXISTS jaytec_worker_handoffs_job_idx
  ON jaytec_worker_handoffs(job_id,created_at);

-- WATCH is out-of-band and has exactly one durable leader row.
CREATE TABLE IF NOT EXISTS jaytec_watch_leader (
  controller_id TEXT PRIMARY KEY CHECK (controller_id='WATCH'),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  leader_epoch BIGINT NOT NULL DEFAULT 0,
  fence_token BIGINT NOT NULL DEFAULT 0,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO jaytec_watch_leader(controller_id)
VALUES ('WATCH')
ON CONFLICT (controller_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS jaytec_watch_reviews (
  review_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jaytec_jobs(job_id) ON DELETE RESTRICT,
  handoff_id TEXT NOT NULL UNIQUE REFERENCES jaytec_worker_handoffs(handoff_id) ON DELETE RESTRICT,
  controller_id TEXT NOT NULL DEFAULT 'WATCH' CHECK (controller_id='WATCH'),
  controller_owner TEXT NOT NULL,
  leader_epoch BIGINT NOT NULL,
  fence_token BIGINT NOT NULL,
  decision TEXT NOT NULL CHECK (
    decision IN ('ACCEPT','REWORK','BLOCK','ESCALATE')
  ),
  reason TEXT NOT NULL,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_watch_reviews_job_idx
  ON jaytec_watch_reviews(job_id,created_at);


-- JAYTEC Single Canonical Writer V1.
-- Additive control plane only. WATCH/workers cannot perform canonical writes.
CREATE TABLE IF NOT EXISTS jaytec_canonical_writer_state (
  writer_id TEXT PRIMARY KEY CHECK (writer_id='CANONICAL'),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  writer_epoch BIGINT NOT NULL DEFAULT 0,
  fence_token BIGINT NOT NULL DEFAULT 0,
  active_write_id TEXT,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO jaytec_canonical_writer_state(writer_id)
VALUES ('CANONICAL')
ON CONFLICT (writer_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS jaytec_canonical_write_queue (
  write_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jaytec_jobs(job_id) ON DELETE RESTRICT,
  handoff_id TEXT NOT NULL REFERENCES jaytec_worker_handoffs(handoff_id) ON DELETE RESTRICT,
  review_id TEXT NOT NULL UNIQUE REFERENCES jaytec_watch_reviews(review_id) ON DELETE RESTRICT,
  idempotency_key TEXT NOT NULL UNIQUE,
  repository TEXT NOT NULL,
  pull_request_number BIGINT NOT NULL CHECK (pull_request_number > 0),
  candidate_head_sha TEXT NOT NULL CHECK (candidate_head_sha ~ '^[0-9a-f]{40}$'),
  expected_base_sha TEXT NOT NULL CHECK (expected_base_sha ~ '^[0-9a-f]{40}$'),
  candidate_digest TEXT NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
  source_shared_state_version BIGINT NOT NULL CHECK (source_shared_state_version > 0),
  candidate_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
  deployment_target JSONB NOT NULL DEFAULT '{}'::jsonb,
  requested_by TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100 CHECK (priority BETWEEN 0 AND 1000),
  state TEXT NOT NULL CHECK (
    state IN (
      'PENDING_CHATGPT_APPROVAL','APPROVED','IN_FLIGHT',
      'VERIFIED_COMPLETE','FAILED_SAFE','QUARANTINED',
      'SUPERSEDED','CANCELLED'
    )
  ),
  approved_by TEXT,
  approval_ref TEXT,
  approval_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  approved_at TIMESTAMPTZ,
  writer_owner TEXT,
  writer_epoch BIGINT,
  writer_fence_token BIGINT,
  writer_lease_expires_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  canonical_commit_sha TEXT CHECK (
    canonical_commit_sha IS NULL OR canonical_commit_sha ~ '^[0-9a-f]{40}$'
  ),
  verification_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  last_error TEXT,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(job_id,handoff_id,review_id)
);

ALTER TABLE jaytec_canonical_write_queue
  ADD COLUMN IF NOT EXISTS candidate_manifest JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE jaytec_canonical_write_queue
  ADD COLUMN IF NOT EXISTS approval_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS jaytec_canonical_write_one_inflight_idx
  ON jaytec_canonical_write_queue((1))
  WHERE state='IN_FLIGHT';

CREATE INDEX IF NOT EXISTS jaytec_canonical_write_queue_state_idx
  ON jaytec_canonical_write_queue(state,priority,created_at);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='jaytec_canonical_writer_active_write_fk'
  ) THEN
    ALTER TABLE jaytec_canonical_writer_state
      ADD CONSTRAINT jaytec_canonical_writer_active_write_fk
      FOREIGN KEY (active_write_id)
      REFERENCES jaytec_canonical_write_queue(write_id)
      ON DELETE RESTRICT;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS jaytec_canonical_write_ledger (
  event_id BIGSERIAL PRIMARY KEY,
  write_id TEXT NOT NULL REFERENCES jaytec_canonical_write_queue(write_id) ON DELETE RESTRICT,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE CHECK (event_hash ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_canonical_write_ledger_write_idx
  ON jaytec_canonical_write_ledger(write_id,event_id);

CREATE OR REPLACE FUNCTION jaytec_canonical_ledger_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'jaytec_canonical_write_ledger is append-only';
END;
$$;

DROP TRIGGER IF EXISTS jaytec_canonical_ledger_no_update
  ON jaytec_canonical_write_ledger;
CREATE TRIGGER jaytec_canonical_ledger_no_update
BEFORE UPDATE ON jaytec_canonical_write_ledger
FOR EACH ROW EXECUTE FUNCTION jaytec_canonical_ledger_immutable();

DROP TRIGGER IF EXISTS jaytec_canonical_ledger_no_delete
  ON jaytec_canonical_write_ledger;
CREATE TRIGGER jaytec_canonical_ledger_no_delete
BEFORE DELETE ON jaytec_canonical_write_ledger
FOR EACH ROW EXECUTE FUNCTION jaytec_canonical_ledger_immutable();
