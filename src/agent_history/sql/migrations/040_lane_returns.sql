-- Every lane-return of a lane, in time order, beside the latest one that return_status and lane_return keep.
-- Each element: {"at": <timestamp>, "source": "report" | "hand_back", "event_uid": ..., "status": ..., "return": ...}.
ALTER TABLE ah.lane ADD COLUMN IF NOT EXISTS returns jsonb;
