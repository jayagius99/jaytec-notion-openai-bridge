-- JAYTEC Session Resilience v1.3 durable job runtime.
-- Additive only. Shared House remains semantic authority; this schema owns
-- operational execution state, leases, fences, steps, operations and events.

CREATE TABLE IF NOT EXISTS jaytec_jobs (
  job_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  subtask_id TEXT,
  assignment_type TEXT NOT NULL,
  objective TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('QUEUED','RUNNING','PAUSED','BLOCKED','SUCCEEDED','FAILED_SAFE','CANCELED')),
  stage TEXT,
  priority INTEGER NOT NULL DEFAULT 100,
  source_shared_state_version BIGINT NOT NULL,
  ownership_epoch BIGINT NOT NULL DEFAULT 1,
  fence_token BIGINT NOT NULL DEFAULT 1,
  execution_room_id TEXT,
  concurrency_class TEXT CHECK (concurrency_class IN ('A','B','C','D','E')),
  mutation_scope JSONB NOT NULL DEFAULT '[]'::jsonb,
  read_scope JSONB NOT NULL DEFAULT '[]'::jsonb,
  dependencies JSONB NOT NULL DEFAULT '[]'::jsonb,
  resource_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  checkpoint_ref TEXT,
  latest_verified_result JSONB,
  blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
  health TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (health IN ('UNKNOWN','HEALTHY','DEGRADED','BLOCKED','FAILED_SAFE')),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  version BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_jobs_status_idx ON jaytec_jobs(status, priority, updated_at);
CREATE INDEX IF NOT EXISTS jaytec_jobs_task_idx ON jaytec_jobs(project_id, task_id);

CREATE TABLE IF NOT EXISTS jaytec_job_steps (
  job_id TEXT NOT NULL REFERENCES jaytec_jobs(job_id) ON DELETE CASCADE,
  step_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  step_type TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','READY','RUNNING','WAITING','SUCCEEDED','FAILED_SAFE','CANCELED')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 1,
  input_digest TEXT,
  idempotency_key TEXT,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  result JSONB,
  error JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, step_id),
  UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS jaytec_job_steps_ready_idx ON jaytec_job_steps(status, ordinal, updated_at);

CREATE TABLE IF NOT EXISTS jaytec_operations (
  operation_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jaytec_jobs(job_id) ON DELETE CASCADE,
  step_id TEXT,
  operation_type TEXT NOT NULL,
  target TEXT NOT NULL,
  intended_effect TEXT NOT NULL,
  source_shared_state_version BIGINT NOT NULL,
  execution_room_id TEXT,
  ownership_epoch BIGINT NOT NULL,
  fence_token BIGINT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('PREPARED','IN_FLIGHT','VERIFIED_COMPLETE','VERIFIED_NOT_DONE','UNCERTAIN_PARTIAL','FAILED_SAFE')),
  preconditions JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  retry_decision TEXT,
  started_at TIMESTAMPTZ,
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_operations_job_status_idx ON jaytec_operations(job_id, status, updated_at);
CREATE INDEX IF NOT EXISTS jaytec_operations_step_idx ON jaytec_operations(job_id, step_id);

CREATE TABLE IF NOT EXISTS jaytec_job_events (
  event_id BIGSERIAL PRIMARY KEY,
  job_id TEXT REFERENCES jaytec_jobs(job_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  source_version BIGINT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jaytec_job_events_job_idx ON jaytec_job_events(job_id, event_id);

CREATE TABLE IF NOT EXISTS jaytec_guardian_findings (
  finding_id TEXT PRIMARY KEY,
  job_id TEXT REFERENCES jaytec_jobs(job_id) ON DELETE SET NULL,
  severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL')),
  finding_type TEXT NOT NULL,
  symptom TEXT NOT NULL,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  containment TEXT,
  proposed_repair TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CONTAINED','REPAIRED','ESCALATED','CLOSED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
