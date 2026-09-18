-- Frozen export manifests. The rendered bytes are derived only from this
-- immutable manifest, so later score revisions cannot change a download.
CREATE TABLE IF NOT EXISTS export_snapshots (
  workspace_id TEXT NOT NULL,
  export_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, export_id)
);
CREATE INDEX IF NOT EXISTS export_snapshots_run_idx
  ON export_snapshots (workspace_id, run_id, created_at DESC);

-- POSTGRESQL SECURITY
ALTER TABLE export_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_snapshots FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON export_snapshots;
CREATE POLICY tenant_isolation ON export_snapshots
  USING (workspace_id = nullif(current_setting('app.workspace_id', true), ''))
  WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), ''));
REVOKE ALL ON export_snapshots FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON export_snapshots TO llm_eval_app;
GRANT ALL ON export_snapshots TO llm_eval_maint;
