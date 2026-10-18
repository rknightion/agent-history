-- Closeout revisions made with a string-replacement edit now finish a run when a collected copy
-- postdates the edit. Drop the projection watermark so every v3 run is projected again in batches.
DELETE FROM ah.meta WHERE key = 'loops_identity_v3_projection_v1';
