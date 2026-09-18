-- Durable recurring synthetic-evaluation schedules and per-slot idempotency.
CREATE TABLE IF NOT EXISTS schedules (
  workspace_id TEXT NOT NULL,
  schedule_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  timezone TEXT NOT NULL,
  local_time TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active','paused')),
  dst_policy TEXT NOT NULL CHECK (dst_policy IN ('first','skip')),
  version_policy TEXT NOT NULL CHECK (version_policy IN ('frozen','new_versions')),
  daily_request_limit INTEGER NOT NULL CHECK (daily_request_limit > 0),
  daily_budget_usd_micros INTEGER NOT NULL CHECK (daily_budget_usd_micros >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, schedule_id)
);
CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules (workspace_id, state, timezone, local_time);

CREATE TABLE IF NOT EXISTS schedule_slots (
  workspace_id TEXT NOT NULL,
  schedule_id TEXT NOT NULL,
  slot_key TEXT NOT NULL,
  slot_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('claimed','queued','failed')),
  owner TEXT NOT NULL,
  job_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, schedule_id, slot_key),
  FOREIGN KEY (workspace_id, schedule_id) REFERENCES schedules(workspace_id, schedule_id)
);
CREATE INDEX IF NOT EXISTS schedule_slots_job_idx ON schedule_slots (workspace_id, job_id);

-- POSTGRESQL SECURITY
DO $policies$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['schedules','schedule_slots'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING '
      || '(workspace_id = nullif(current_setting(''app.workspace_id'', true), '''')) WITH CHECK '
      || '(workspace_id = nullif(current_setting(''app.workspace_id'', true), ''''))', t);
    EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO llm_eval_app', t);
    EXECUTE format('GRANT ALL ON %I TO llm_eval_maint', t);
  END LOOP;
END
$policies$;
