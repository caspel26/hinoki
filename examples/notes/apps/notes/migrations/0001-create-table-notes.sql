-- hinoki migration 0001-create-table-notes
-- generated from the models; review it.

-- create table notes
CREATE TABLE "notes" (
    "id" UUID PRIMARY KEY,
    "title" varchar(80) NOT NULL,
    "body" text NOT NULL
);
CREATE INDEX "ix_notes_title" ON "notes" ("title");
