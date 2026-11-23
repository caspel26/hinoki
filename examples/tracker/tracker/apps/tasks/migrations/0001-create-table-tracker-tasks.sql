-- hinoki migration 0001-create-table-tracker-tasks
-- generated from the models; review it.

-- create table tracker_tasks
CREATE TABLE "tracker_tasks" (
    "id" UUID PRIMARY KEY,
    "owner_id" UUID NOT NULL REFERENCES "tracker_members" ("id") ON DELETE RESTRICT,
    "title" varchar(120) NOT NULL,
    "priority" integer NOT NULL,
    "done" boolean NOT NULL,
    "created_at" timestamptz NOT NULL,
    "completed_at" timestamptz
);
CREATE INDEX "ix_tracker_tasks_owner_id_created_at_e46b5006" ON "tracker_tasks" ("owner_id", "created_at");
