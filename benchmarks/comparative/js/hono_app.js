// The benchmark service on Hono with @hono/node-server (Node.js, node:cluster) and node-postgres (`pg`).
// Input is checked with Hono's `validator` middleware and the shared rules in common.js.
import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { HTTPException } from "hono/http-exception";
import { validator } from "hono/validator";
import pg from "pg";
import {
  BODIES,
  INVALID,
  NOT_FOUND,
  SQL,
  createdOut,
  isUuid,
  pageOut,
  pageParams,
  postInput,
  postOut,
  settings,
} from "./common.js";
import { onTerminate, runCluster } from "./cluster.js";

// Named queries make `pg` prepare each statement once per connection.
const run = (pool, name, values) => pool.query({ name, text: SQL[name], values });
const JSON_TYPE = { "content-type": "application/json" };

const pageQuery = validator("query", (query, c) => pageParams(query.page, query.page_size) ?? c.json(INVALID, 422));
const postId = validator("param", (params, c) => (isUuid(params.post_id) ? params : c.json(INVALID, 422)));
const postBody = validator("json", (data, c) => postInput(data) ?? c.json(INVALID, 422));

function routes(app, pool) {
  app.get("/health", (c) => c.json({ status: "ok" }));
  for (const name of Object.keys(BODIES)) {
    app.get(`/${name}`, (c) => c.body(BODIES[name], 200, JSON_TYPE));
  }
  app.get("/posts", pageQuery, async (c) => {
    const params = c.req.valid("query");
    const counted = await run(pool, "count", []);
    const total = Number(counted.rows[0].total);
    const found = await run(pool, "list", [params.pageSize, (params.page - 1) * params.pageSize]);
    return c.json(pageOut(found.rows, total, params));
  });
  app.get("/posts/:post_id", postId, async (c) => {
    const found = await run(pool, "detail", [c.req.valid("param").post_id]);
    if (found.rows.length === 0) return c.json(NOT_FOUND, 404);
    return c.json(postOut(found.rows[0]));
  });
  app.post("/posts", postBody, async (c) => {
    const input = c.req.valid("json");
    const id = crypto.randomUUID();
    const createdAt = new Date();
    await run(pool, "insert", [id, input.authorId, input.title, input.body, createdAt]);
    const found = await run(pool, "author", [input.authorId]);
    return c.json(createdOut(id, input, createdAt, found.rows[0]), 201);
  });
  // Unreadable JSON is a 400 HTTPException in Hono; the other stacks answer 422.
  app.onError((error, c) => {
    if (error instanceof HTTPException && error.status === 400) return c.json(INVALID, 422);
    console.error(error);
    return c.json({ detail: "internal error" }, 500);
  });
}

async function worker(config) {
  const pool = new pg.Pool({ connectionString: config.databaseUrl, max: config.pool });
  const app = new Hono();
  routes(app, pool);
  const server = serve({ fetch: app.fetch, hostname: "127.0.0.1", port: config.port });
  onTerminate(async () => {
    await new Promise((resolve) => server.close(resolve));
    await pool.end();
  });
}

runCluster(settings(process.env), worker);
