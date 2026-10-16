-- Historical commits remain unknown until their Task trailers have been collected.
-- An empty array means the observed commit has no exact tracker IDs in Task trailers.
ALTER TABLE ah.git_commit ADD COLUMN IF NOT EXISTS task_ids text[];

-- Match the projection's exact owner/repo normalisation and bounded default-branch window.
CREATE INDEX IF NOT EXISTS git_commit_task_window_idx
    ON ah.git_commit (lower(regexp_replace(repo_slug, '^[^/]+/', '')), committed_at)
    WHERE on_default;

COMMENT ON COLUMN ah.git_commit.task_ids IS
    'Distinct exact tracker IDs in git Task trailers; NULL until observed, empty array when observed without IDs.';
