-- Persisted calibration state is keyed by the complete judge instrument
-- identity; labels from another provider/model/schema/rubric never mix.
CREATE TABLE IF NOT EXISTS judge_calibrations (
  workspace_id TEXT NOT NULL,
  binding_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  rubric_version TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('UNCALIBRATED','CALIBRATING','CALIBRATED')),
  label_count INTEGER NOT NULL DEFAULT 0 CHECK (label_count >= 0),
  kappa REAL,
  min_labels INTEGER NOT NULL CHECK (min_labels > 0),
  kappa_ready REAL NOT NULL,
  kappa_reset REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, binding_key)
);

CREATE TABLE IF NOT EXISTS judge_calibration_labels (
  workspace_id TEXT NOT NULL,
  binding_key TEXT NOT NULL,
  generation INTEGER NOT NULL,
  label_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  human_label TEXT NOT NULL,
  judge_label TEXT NOT NULL,
  author_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, binding_key, generation, label_id)
);
CREATE INDEX IF NOT EXISTS judge_calibration_labels_case_idx
  ON judge_calibration_labels (workspace_id, binding_key, generation, case_id);

-- POSTGRESQL SECURITY
DO $policies$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['judge_calibrations','judge_calibration_labels'] LOOP
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
