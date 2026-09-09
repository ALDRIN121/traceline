-- Durable harness sessions and redacted authoring messages.
CREATE TABLE IF NOT EXISTS harness_sessions (
  workspace_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  evaluation_id TEXT NOT NULL,
  evaluation_version_id TEXT,
  knowledge_version_id TEXT,
  verification_id TEXT,
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, session_id)
);
CREATE INDEX IF NOT EXISTS harness_sessions_project_idx ON harness_sessions (workspace_id, project_id);
CREATE TABLE IF NOT EXISTS harness_messages (
  workspace_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_json TEXT NOT NULL,
  operation_id TEXT,
  job_id TEXT,
  observed_state TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, message_id)
);
CREATE INDEX IF NOT EXISTS harness_messages_session_idx ON harness_messages (workspace_id, session_id, created_at);

-- POSTGRESQL SECURITY
DO $policies$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['harness_sessions','harness_messages'] LOOP
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
