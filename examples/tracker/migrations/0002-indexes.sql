-- hinoki migration 0002-indexes
-- generated from the models; review it.

-- index tracker_tasks (owner_id, created_at)
CREATE INDEX "ix_tracker_tasks_owner_id_created_at" ON "tracker_tasks" ("owner_id", "created_at");
