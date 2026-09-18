-- Remote async job identity is stored before polling so a worker restart can
-- resume observation instead of submitting the agent request twice.
CREATE TABLE IF NOT EXISTS remote_jobs (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  remote_job_id TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS remote_jobs_run_idx ON remote_jobs (run_id, case_id);

-- POSTGRESQL SECURITY
ALTER TABLE remote_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE remote_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON remote_jobs;
CREATE POLICY tenant_isolation ON remote_jobs USING
  (workspace_id = nullif(current_setting('app.workspace_id', true), '')) WITH CHECK
  (workspace_id = nullif(current_setting('app.workspace_id', true), ''));
REVOKE ALL ON remote_jobs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON remote_jobs TO llm_eval_app;
GRANT ALL ON remote_jobs TO llm_eval_maint;
