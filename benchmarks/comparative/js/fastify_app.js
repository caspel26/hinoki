// The benchmark service on Fastify (Node.js, node:cluster) with node-postgres (`pg`).
// Handlers are async; values are encoded by Fastify's default JSON serializer.
import Fastify from "fastify";
import pg from "pg";
import { onTerminate, runCluster } from "./cluster.js";
import {
  INVALID,
  NOT_FOUND,
  SQL,
  UNAUTHORIZED,
  articleId,
  cookie,
  loadPayloads,
  pageParams,
  queries,
  settings,
} from "./common.js";

// Named queries make `pg` prepare each statement once per connection.
const runner = (pool) => async (name, values) => (await pool.query({ name, text: SQL[name], values })).rows;

function routes(app, db, payloads) {
  app.get("/health", async () => ({ status: "ok" }));
  for (const name of Object.keys(payloads)) app.get(`/${name}`, async () => payloads[name]);
  app.get("/db", async () => db.users());
  const listing = async (request, reply) => {
    const params = pageParams(request.query.page, request.query.page_size);
    if (!params) return reply.code(422).send(INVALID);
    return db.articlesPage(params);
  };
  app.get("/articles", listing);
  app.get("/articles/:article_id", async (request, reply) => {
    const id = articleId(request.params.article_id);
    if (id === null) return reply.code(422).send(INVALID);
    return (await db.article(id)) ?? reply.code(404).send(NOT_FOUND);
  });
  // The access token is checked, and its user loaded, before the protected handlers run.
  const authenticated = async (request, reply) => {
    request.user = await db.currentUser(cookie(request.headers.cookie));
    if (!request.user) return reply.code(401).send(UNAUTHORIZED);
  };
  app.get("/auth/me", { preHandler: authenticated }, async (request) => request.user);
  app.get("/auth/articles", { preHandler: authenticated }, listing);
}

async function worker(config) {
  const pool = new pg.Pool({ connectionString: config.databaseUrl, max: config.pool });
  const app = Fastify({ logger: false });
  routes(app, queries(runner(pool)), loadPayloads(config.payloads));
  app.addHook("onClose", () => pool.end());
  await app.listen({ host: "127.0.0.1", port: config.port });
  onTerminate(() => app.close());
}

runCluster(settings(process.env), worker);
