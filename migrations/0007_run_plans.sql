-- Immutable execution plans and explicit run authorization.
CREATE TABLE IF NOT EXISTS run_plans (
  workspace_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('validated','blocked')),
  content_json TEXT NOT NULL,
  blockers_json TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, plan_id),
  UNIQUE (workspace_id, content_digest)
);
CREATE TABLE IF NOT EXISTS run_authorizations (
  workspace_id TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  plan_hash TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('authorized','expired','revoked')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, authorization_id),
  FOREIGN KEY (workspace_id, plan_id) REFERENCES run_plans(workspace_id, plan_id)
);
CREATE INDEX IF NOT EXISTS run_authorizations_plan_idx
  ON run_authorizations (workspace_id, plan_id, created_at DESC);

-- POSTGRESQL SECURITY
DO $policies$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['run_plans','run_authorizations'] LOOP
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
