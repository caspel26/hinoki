// The benchmark service on Hono with @hono/node-server (Node.js, node:cluster) and node-postgres (`pg`).
// Handlers are async; values are encoded by Hono's `c.json`.
import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { getCookie } from "hono/cookie";
import { createMiddleware } from "hono/factory";
import pg from "pg";
import { onTerminate, runCluster } from "./cluster.js";
import {
  COOKIE,
  INVALID,
  NOT_FOUND,
  SQL,
  UNAUTHORIZED,
  articleId,
  loadPayloads,
  pageParams,
  queries,
  settings,
} from "./common.js";

// Named queries make `pg` prepare each statement once per connection.
const runner = (pool) => async (name, values) => (await pool.query({ name, text: SQL[name], values })).rows;

function routes(app, db, payloads) {
  app.get("/health", (c) => c.json({ status: "ok" }));
  for (const name of Object.keys(payloads)) app.get(`/${name}`, async (c) => c.json(payloads[name]));
  app.get("/db", async (c) => c.json(await db.users()));
  const listing = async (c) => {
    const params = pageParams(c.req.query("page"), c.req.query("page_size"));
    return params ? c.json(await db.articlesPage(params)) : c.json(INVALID, 422);
  };
  app.get("/articles", listing);
  app.get("/articles/:article_id", async (c) => {
    const id = articleId(c.req.param("article_id"));
    if (id === null) return c.json(INVALID, 422);
    const found = await db.article(id);
    return found ? c.json(found) : c.json(NOT_FOUND, 404);
  });
  // The access token is checked, and its user loaded, before the protected handlers run.
  const authenticated = createMiddleware(async (c, next) => {
    const user = await db.currentUser(getCookie(c, COOKIE));
    if (!user) return c.json(UNAUTHORIZED, 401);
    c.set("user", user);
    await next();
  });
  app.get("/auth/me", authenticated, async (c) => c.json(c.get("user")));
  app.get("/auth/articles", authenticated, listing);
}

async function worker(config) {
  const pool = new pg.Pool({ connectionString: config.databaseUrl, max: config.pool });
  const app = new Hono();
  routes(app, queries(runner(pool)), loadPayloads(config.payloads));
  const server = serve({ fetch: app.fetch, hostname: "127.0.0.1", port: config.port });
  onTerminate(async () => {
    await new Promise((resolve) => server.close(resolve));
    await pool.end();
  });
}

runCluster(settings(process.env), worker);
