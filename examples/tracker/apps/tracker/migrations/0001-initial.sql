-- hinoki migration 0001-initial
-- generated from the models; review it.

-- create table tracker_members
CREATE TABLE "tracker_members" (
    "id" uuid PRIMARY KEY,
    "name" varchar(80) NOT NULL,
    "token" text NOT NULL UNIQUE
);

-- create table tracker_tasks
CREATE TABLE "tracker_tasks" (
    "id" uuid PRIMARY KEY,
    "owner_id" uuid NOT NULL REFERENCES "tracker_members" ("id") ON DELETE RESTRICT,
    "title" varchar(120) NOT NULL,
    "done" boolean NOT NULL,
    "created_at" timestamptz NOT NULL,
    "completed_at" timestamptz
);
