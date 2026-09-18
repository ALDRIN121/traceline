-- SQLite installs the expanded CHECK with a table rebuild in migrations.py.
-- POSTGRESQL SECURITY
ALTER TABLE object_versions DROP CONSTRAINT IF EXISTS object_versions_kind_check;
ALTER TABLE object_versions ADD CONSTRAINT object_versions_kind_check
  CHECK (kind IN ('source','knowledge','evaluation','dataset','target','connection','dashboard',
                  'model_profile','model_selection','judge_rubric'));
