-- Forward-only durable job, secret-reference, and trusted-redaction support.
CREATE TABLE IF NOT EXISTS jobs (
  workspace_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  command_json TEXT NOT NULL,
  command_digest TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','leased','completed','failed','cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
  fence INTEGER NOT NULL DEFAULT 0 CHECK (fence >= 0),
  lease_worker_id TEXT,
  lease_expires_at TEXT,
  cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0,1)),
  result_json TEXT,
  error_json TEXT,
  command_redaction_json TEXT NOT NULL,
  result_redaction_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, job_id),
  UNIQUE (workspace_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (workspace_id, status, created_at);
CREATE TABLE IF NOT EXISTS job_outbox (
  workspace_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  published_at TEXT,
  PRIMARY KEY (workspace_id, event_id)
);
CREATE TABLE IF NOT EXISTS secret_refs (
  workspace_id TEXT NOT NULL,
  secret_id TEXT NOT NULL,
  secret_type TEXT NOT NULL,
  ciphertext TEXT NOT NULL,
  allowed_services_json TEXT NOT NULL,
  key_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state = 'active'),
  created_at TEXT NOT NULL,
  rotated_at TEXT,
  PRIMARY KEY (workspace_id, secret_id)
);
CREATE TABLE IF NOT EXISTS secret_audit (
  workspace_id TEXT NOT NULL,
  audit_id TEXT NOT NULL,
  secret_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('created','resolved','rotated')),
  service TEXT,
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, audit_id)
);

-- POSTGRESQL SECURITY
DO $policies$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs','job_outbox','secret_refs','secret_audit'] LOOP
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
