-- hinoki migration 0001-create-table-tracker-members
-- generated from the models; review it.

-- create table tracker_members
CREATE TABLE "tracker_members" (
    "id" UUID PRIMARY KEY,
    "name" varchar(80) NOT NULL,
    "token_hash" text NOT NULL UNIQUE
);
