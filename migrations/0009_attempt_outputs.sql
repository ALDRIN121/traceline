-- Redacted final-output evidence for connector/local output-only metrics.
-- Kept separate from trace_events so re-scoring never invents a trace.
CREATE TABLE IF NOT EXISTS attempt_outputs (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  result_payload TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempt_outputs_run_idx ON attempt_outputs (run_id, case_id);

-- POSTGRESQL SECURITY
ALTER TABLE attempt_outputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempt_outputs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON attempt_outputs;
CREATE POLICY tenant_isolation ON attempt_outputs USING
  (workspace_id = nullif(current_setting('app.workspace_id', true), '')) WITH CHECK
  (workspace_id = nullif(current_setting('app.workspace_id', true), ''));
REVOKE ALL ON attempt_outputs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON attempt_outputs TO llm_eval_app;
GRANT ALL ON attempt_outputs TO llm_eval_maint;
