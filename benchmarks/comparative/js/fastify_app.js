// The benchmark service on Fastify (Node.js, node:cluster) with node-postgres (`pg`).
// Input is checked by Fastify's JSON Schema validation and output serialized from response schemas.
import Fastify from "fastify";
import pg from "pg";
import { BODIES, INVALID, NOT_FOUND, SQL, createdOut, pageOut, postOut, settings } from "./common.js";
import { onTerminate, runCluster } from "./cluster.js";

const uuid = { type: "string", format: "uuid" };
const author = { type: "object", properties: { id: uuid, name: { type: "string" } } };
const post = {
  type: "object",
  properties: {
    id: uuid,
    title: { type: "string" },
    body: { type: "string" },
    published: { type: "boolean" },
    created_at: { type: "string", format: "date-time" },
    author,
  },
};
const page = {
  type: "object",
  properties: {
    items: { type: "array", items: post },
    page: { type: "integer" },
    page_size: { type: "integer" },
    total: { type: "integer" },
    pages: { type: "integer" },
  },
};
const listSchema = {
  querystring: {
    type: "object",
    properties: {
      page: { type: "integer", minimum: 1, default: 1 },
      page_size: { type: "integer", minimum: 1, maximum: 100, default: 20 },
    },
  },
  response: { 200: page },
};
const detailSchema = {
  params: { type: "object", properties: { post_id: uuid }, required: ["post_id"] },
  response: { 200: post },
};
const createSchema = {
  body: {
    type: "object",
    required: ["author_id", "title", "body"],
    properties: {
      author_id: uuid,
      title: { type: "string", minLength: 1, maxLength: 120 },
      body: { type: "string", minLength: 1 },
    },
  },
  response: { 201: post },
};

// Named queries make `pg` prepare each statement once per connection.
const run = (pool, name, values) => pool.query({ name, text: SQL[name], values });

function routes(app, pool) {
  app.get("/health", async () => ({ status: "ok" }));
  for (const name of Object.keys(BODIES)) {
    app.get(`/${name}`, (_request, reply) => {
      reply.type("application/json").send(BODIES[name]);
    });
  }
  app.get("/posts", { schema: listSchema }, async (request) => {
    const params = { page: request.query.page, pageSize: request.query.page_size };
    const counted = await run(pool, "count", []);
    const total = Number(counted.rows[0].total);
    const found = await run(pool, "list", [params.pageSize, (params.page - 1) * params.pageSize]);
    return pageOut(found.rows, total, params);
  });
  app.get("/posts/:post_id", { schema: detailSchema }, async (request, reply) => {
    const found = await run(pool, "detail", [request.params.post_id]);
    if (found.rows.length === 0) return reply.code(404).send(NOT_FOUND);
    return postOut(found.rows[0]);
  });
  app.post("/posts", { schema: createSchema }, async (request, reply) => {
    const input = { authorId: request.body.author_id, title: request.body.title, body: request.body.body };
    const id = crypto.randomUUID();
    const createdAt = new Date();
    await run(pool, "insert", [id, input.authorId, input.title, input.body, createdAt]);
    const found = await run(pool, "author", [input.authorId]);
    return reply.code(201).send(createdOut(id, input, createdAt, found.rows[0]));
  });
}

async function worker(config) {
  const pool = new pg.Pool({ connectionString: config.databaseUrl, max: config.pool });
  const app = Fastify({ logger: false });
  // Schema failures and unreadable JSON are 422, as in the other stacks.
  app.setErrorHandler((error, _request, reply) => {
    if (error.validation || error.statusCode === 400) return reply.code(422).send(INVALID);
    reply.log.error(error);
    return reply.code(error.statusCode ?? 500).send({ detail: "internal error" });
  });
  routes(app, pool);
  app.addHook("onClose", () => pool.end());
  await app.listen({ host: "127.0.0.1", port: config.port });
  onTerminate(() => app.close());
}

runCluster(settings(process.env), worker);
