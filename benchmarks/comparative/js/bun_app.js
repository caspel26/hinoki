// The benchmark service on Bun.serve (no framework) with Bun's built-in PostgreSQL client (Bun.SQL).
// With more than one worker, a launcher process starts `BENCH_WORKERS` server processes sharing the
// port with SO_REUSEPORT, so the kernel spreads connections over them. Values are encoded by
// `Response.json`.
import { SQL as Client } from "bun";
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

const json = (data, status = 200) => Response.json(data, { status });

function handlers(db) {
  const listing = async (request) => {
    const query = new URL(request.url).searchParams;
    const params = pageParams(query.get("page") ?? undefined, query.get("page_size") ?? undefined);
    return params ? json(await db.articlesPage(params)) : json(INVALID, 422);
  };
  const article = async (request) => {
    const id = articleId(request.params.article_id);
    if (id === null) return json(INVALID, 422);
    const found = await db.article(id);
    return found ? json(found) : json(NOT_FOUND, 404);
  };
  // The access token is checked, and its user loaded, before the protected handler runs.
  const authenticated = (handler) => async (request) => {
    const user = await db.currentUser(cookie(request.headers.get("cookie")));
    return user ? handler(request, user) : json(UNAUTHORIZED, 401);
  };
  return {
    listing,
    article,
    users: async () => json(await db.users()),
    me: authenticated(async (_request, user) => json(user)),
    authListing: authenticated(listing),
  };
}

// Bun.SQL's `unsafe` sends a JavaScript array as "1,2,3", which PostgreSQL rejects for a
// `bigint[]` parameter, so the id list is sent as an array literal.
const parameter = (value) => (Array.isArray(value) ? `{${value.join(",")}}` : value);

function serveWorker(config) {
  const sql = new Client({ url: config.databaseUrl, max: config.pool, prepare: true });
  const db = queries((name, values) => sql.unsafe(SQL[name], values.map(parameter)));
  const routes = handlers(db);
  const payloads = loadPayloads(config.payloads);
  const payload = (name) => ({ GET: async () => json(payloads[name]) });
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: config.port,
    reusePort: config.workers > 1,
    development: false,
    routes: {
      "/health": { GET: () => json({ status: "ok" }) },
      "/json-1k": payload("json-1k"),
      "/json-10k": payload("json-10k"),
      "/db": { GET: routes.users },
      "/articles": { GET: routes.listing },
      "/articles/:article_id": { GET: routes.article },
      "/auth/me": { GET: routes.me },
      "/auth/articles": { GET: routes.authListing },
    },
    fetch: () => json({ detail: "Not Found" }, 404),
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
if (config.workers === 1 || process.env.BENCH_BUN_WORKER === "1") serveWorker(config);
else launch(config);
