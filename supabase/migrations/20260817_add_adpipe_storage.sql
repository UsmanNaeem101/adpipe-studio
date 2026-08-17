-- AdPipe's storage, for running as a service rather than on a desk.
--
-- A container's filesystem is wiped on every deploy, so `projects/<name>/`
-- lasts exactly until the next push. Text goes in a table and bytes go in a
-- bucket, both keyed by the relative path the pipeline already uses:
-- `projects/montisella/research/voc.jsonl`.
--
-- Text is a table rather than more objects in the bucket because nearly
-- everything this pipeline writes is JSON or markdown someone will want to
-- read, query or diff. That is useless as an opaque blob.

CREATE TABLE IF NOT EXISTS adpipe_files (
  key text PRIMARY KEY,
  -- Denormalised from the key so a project's files can be found, counted and
  -- dropped without parsing paths in SQL.
  project text,
  content text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_adpipe_files_project ON adpipe_files(project);
-- Listing is always "everything under this prefix", which is a prefix scan.
CREATE INDEX IF NOT EXISTS idx_adpipe_files_key_prefix ON adpipe_files(key text_pattern_ops);

CREATE OR REPLACE FUNCTION adpipe_files_touch() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS adpipe_files_touch ON adpipe_files;
CREATE TRIGGER adpipe_files_touch BEFORE UPDATE ON adpipe_files
  FOR EACH ROW EXECUTE FUNCTION adpipe_files_touch();

-- AdPipe reaches Postgres with the service role key and is itself reached only
-- through Topic Atlas, which is where the session is checked. RLS is enabled so
-- that nothing else — an anon key, a stray client — can read it, and no policy
-- is granted because no other role should have access at all.
ALTER TABLE adpipe_files ENABLE ROW LEVEL SECURITY;

INSERT INTO storage.buckets (id, name, public)
VALUES ('adpipe-media', 'adpipe-media', false)
ON CONFLICT (id) DO NOTHING;

NOTIFY pgrst, 'reload schema';
