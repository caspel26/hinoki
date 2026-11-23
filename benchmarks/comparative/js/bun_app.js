// The benchmark service on Bun.serve (no framework) with Bun's built-in PostgreSQL client (Bun.SQL).
// A launcher process starts `BENCH_WORKERS` server processes sharing the port with SO_REUSEPORT,
// so the kernel spreads connections over them. Tagged-template queries are prepared per connection.
import { SQL as Client } from "bun";
import { BODIES, INVALID, NOT_FOUND, createdOut, isUuid, pageOut, pageParams, postInput, postOut, settings } from "./common.js";

const JSON_HEADERS = { "content-type": "application/json" };

function json(data, status = 200) {
  return Response.json(data, { status });
}

function handlers(sql) {
  const listing = async (request) => {
    const query = new URL(request.url).searchParams;
    const params = pageParams(query.get("page") ?? undefined, query.get("page_size") ?? undefined);
    if (!params) return json(INVALID, 422);
    const [counted] = await sql`SELECT count(*) AS total FROM posts WHERE published`;
    const total = Number(counted.total);
    const rows = await sql`SELECT p.id, p.title, p.body, p.published, p.created_at, a.id AS author_id, a.name AS author_name FROM posts p JOIN authors a ON a.id = p.author_id WHERE p.published ORDER BY p.created_at DESC, p.id DESC LIMIT ${params.pageSize} OFFSET ${(params.page - 1) * params.pageSize}`;
    return json(pageOut(rows, total, params));
  };
  const detail = async (request) => {
    const id = request.params.post_id;
    if (!isUuid(id)) return json(INVALID, 422);
    const rows = await sql`SELECT p.id, p.title, p.body, p.published, p.created_at, a.id AS author_id, a.name AS author_name FROM posts p JOIN authors a ON a.id = p.author_id WHERE p.id = ${id} AND p.published`;
    return rows.length === 0 ? json(NOT_FOUND, 404) : json(postOut(rows[0]));
  };
  const create = async (request) => {
    let data;
    try {
      data = await request.json();
    } catch {
      return json(INVALID, 422);
    }
    const input = postInput(data);
    if (!input) return json(INVALID, 422);
    const id = crypto.randomUUID();
    const createdAt = new Date();
    await sql`INSERT INTO posts (id, author_id, title, body, published, created_at) VALUES (${id}, ${input.authorId}, ${input.title}, ${input.body}, true, ${createdAt})`;
    const [author] = await sql`SELECT id, name FROM authors WHERE id = ${input.authorId}`;
    return json(createdOut(id, input, createdAt, author), 201);
  };
  return { listing, detail, create };
}

function serveWorker(config) {
  const sql = new Client({ url: config.databaseUrl, max: config.pool, prepare: true });
  const { listing, detail, create } = handlers(sql);
  const payload = (name) => ({ GET: () => new Response(BODIES[name], { headers: JSON_HEADERS }) });
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: config.port,
    reusePort: true,
    development: false,
    routes: {
      "/health": { GET: () => json({ status: "ok" }) },
      "/json-1k": payload("json-1k"),
      "/json-10k": payload("json-10k"),
      "/posts": { GET: listing, POST: create },
      "/posts/:post_id": { GET: detail },
    },
    fetch: () => json(NOT_FOUND, 404),
    error(error) {
      console.error(error);
      return json({ detail: "internal error" }, 500);
    },
  });
  process.once("SIGTERM", async () => {
    setTimeout(() => process.exit(0), 5000).unref();
    await server.stop();
    await sql.close();
    process.exit(0);
  });
}

// The launcher: a worker that dies outside a shutdown stops the whole server.
function launch(config) {
  let stopping = false;
  const children = [];
  const stopAll = () => {
    stopping = true;
    for (const child of children) child.kill("SIGTERM");
  };
  for (let index = 0; index < config.workers; index += 1) {
    const child = Bun.spawn([process.execPath, import.meta.path], {
      env: { ...process.env, BENCH_BUN_WORKER: "1" },
      stdio: ["ignore", "inherit", "inherit"],
      onExit(_process, code, signal) {
        if (stopping) return;
        console.error(`worker exited (${signal ?? code}); stopping`);
        process.exitCode = 1;
        stopAll();
      },
    });
    children.push(child);
  }
  process.on("SIGTERM", stopAll);
  process.on("SIGINT", stopAll);
}

const config = settings(process.env);
if (process.env.BENCH_BUN_WORKER === "1") serveWorker(config);
else launch(config);
