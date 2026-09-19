-- Cross-process round-robin cursor for the explicitly approved worker pool.
-- The row is a system-owned coordination record, not tenant data.
CREATE TABLE IF NOT EXISTS worker_dispatch_cursors (
  workspace_id TEXT NOT NULL DEFAULT '__system__',
  pool_id TEXT NOT NULL,
  actor_digest TEXT NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, pool_id)
);

-- POSTGRESQL SECURITY
ALTER TABLE worker_dispatch_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE worker_dispatch_cursors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS worker_dispatch_service ON worker_dispatch_cursors;
CREATE POLICY worker_dispatch_service ON worker_dispatch_cursors
  USING (
    workspace_id = '__system__'
    AND current_setting('app.service_identity', true) = 'worker'
  )
  WITH CHECK (
    workspace_id = '__system__'
    AND current_setting('app.service_identity', true) = 'worker'
  );
REVOKE ALL ON worker_dispatch_cursors FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON worker_dispatch_cursors TO llm_eval_app;
GRANT ALL ON worker_dispatch_cursors TO llm_eval_maint;
