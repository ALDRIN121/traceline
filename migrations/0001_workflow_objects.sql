-- Forward-only, additive migration. Existing runs and custom_evals are retained.
-- The shared DDL above the PostgreSQL marker also supports the SQLite prototype.
CREATE TABLE IF NOT EXISTS object_versions (
  workspace_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('source','knowledge','evaluation','dataset','target','connection','dashboard','evaluator','model_profile','model_selection','judge_rubric')),
  parent_id TEXT NOT NULL,
  previous_version_id TEXT,
  content_digest TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  content_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  PRIMARY KEY (workspace_id, version_id),
  UNIQUE (workspace_id, kind, parent_id, revision),
  FOREIGN KEY (workspace_id, previous_version_id) REFERENCES object_versions(workspace_id, version_id)
);
CREATE TABLE IF NOT EXISTS version_heads (
  workspace_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  parent_id TEXT NOT NULL,
  active_version_id TEXT,
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  PRIMARY KEY (workspace_id, kind, parent_id),
  FOREIGN KEY (workspace_id, active_version_id) REFERENCES object_versions(workspace_id, version_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
  workspace_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  checksum TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
  media_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state = 'ready'),
  PRIMARY KEY (workspace_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS version_artifacts (
  workspace_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  PRIMARY KEY (workspace_id, version_id, artifact_id),
  FOREIGN KEY (workspace_id, version_id) REFERENCES object_versions(workspace_id, version_id),
  FOREIGN KEY (workspace_id, artifact_id) REFERENCES artifacts(workspace_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS legacy_definition_migrations (
  workspace_id TEXT NOT NULL,
  eval_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  source_digest TEXT NOT NULL,
  version_id TEXT,
  state TEXT NOT NULL CHECK (state IN ('migrated','quarantined')),
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, eval_id, kind, source_digest),
  FOREIGN KEY (workspace_id, version_id) REFERENCES object_versions(workspace_id, version_id)
);

-- POSTGRESQL SECURITY
-- Run as a migration identity, never the API identity. Membership and LOGIN
-- credentials are provisioned by the operator, never embedded here.
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'llm_eval_app') THEN
    CREATE ROLE llm_eval_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'llm_eval_maint') THEN
    CREATE ROLE llm_eval_maint NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
  END IF;
END
$roles$;

DO $policies$
DECLARE t text;
BEGIN
  -- Explicit inventory prevents an unrelated table from receiving app grants.
  FOREACH t IN ARRAY ARRAY[
    'projects','smoke_attempts','runs','run_cases','run_case_attempts','trace_events',
    'cost_summaries','run_case_metric_results','run_metric_results','score_revisions','custom_evals',
    'object_versions','version_heads','artifacts','version_artifacts','legacy_definition_migrations'
  ] LOOP
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
  EXECUTE format('GRANT USAGE ON SCHEMA %I TO llm_eval_app, llm_eval_maint', current_schema());
END
$policies$;
REVOKE UPDATE, DELETE ON object_versions, version_artifacts, legacy_definition_migrations FROM llm_eval_app;

CREATE OR REPLACE FUNCTION reject_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $immutable$
BEGIN
  RAISE EXCEPTION 'Object versions are immutable';
END
$immutable$;
DROP TRIGGER IF EXISTS immutable_object_versions ON object_versions;
CREATE TRIGGER immutable_object_versions BEFORE UPDATE OR DELETE ON object_versions
  FOR EACH ROW EXECUTE FUNCTION reject_version_mutation();
