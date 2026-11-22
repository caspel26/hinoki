-- hinoki migration 0001-create-table-tracker-activity
-- generated from the models; review it.

-- create table tracker_activity
CREATE TABLE "tracker_activity" (
    "id" UUID PRIMARY KEY,
    "member_id" UUID NOT NULL REFERENCES "tracker_members" ("id") ON DELETE CASCADE,
    "task_id" UUID NOT NULL,
    "kind" varchar(20) NOT NULL,
    "created_at" timestamptz NOT NULL
);
CREATE INDEX "ix_tracker_activity_member_id_created_at_58e96532" ON "tracker_activity" ("member_id", "created_at");
