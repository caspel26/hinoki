-- hinoki migration 0002-accounts
-- generated from the models; review it.

-- add blog_users.username
ALTER TABLE "blog_users" ADD COLUMN "username" varchar(40);
CREATE UNIQUE INDEX blog_users_username_unique ON blog_users (username);

-- add blog_users.password_hash
ALTER TABLE "blog_users" ADD COLUMN "password_hash" text;
