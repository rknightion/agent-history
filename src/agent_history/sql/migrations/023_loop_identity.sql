-- Exact receiver join identity. Unknown values remain NULL; existing rows and grants survive.
ALTER TABLE ah.loops ADD COLUMN repo text;
ALTER TABLE ah.loops ADD COLUMN loop text;
ALTER TABLE ah.loops ADD COLUMN goal_sha256 text;
