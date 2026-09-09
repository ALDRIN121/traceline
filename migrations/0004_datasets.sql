-- Immutable dataset identity plus retained import validation reports.
CREATE TABLE IF NOT EXISTS datasets (
  workspace_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, dataset_id)
);
CREATE INDEX IF NOT EXISTS datasets_project_idx ON datasets (workspace_id, project_id);
CREATE TABLE IF NOT EXISTS dataset_import_reports (
  workspace_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  upload_id TEXT NOT NULL,
  mapping_json TEXT NOT NULL,
  requirements_json TEXT NOT NULL,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, report_id)
);
CREATE INDEX IF NOT EXISTS dataset_import_reports_dataset_idx
  ON dataset_import_reports (workspace_id, dataset_id);

-- POSTGRESQL SECURITY
DO $policies$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['datasets','dataset_import_reports'] LOOP
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
