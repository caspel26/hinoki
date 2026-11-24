// The benchmark service on Hono served by Bun.serve (Bun, processes sharing the port above one)
// with Bun's built-in PostgreSQL client (Bun.SQL).
import { database, listenOptions, onTerminate, runBun } from "./bun_runtime.js";
import { loadPayloads, queries, settings } from "./common.js";
import { honoApp } from "./hono_routes.js";

function serveWorker(config) {
  const { sql, query } = database(config);
  const app = honoApp(queries(query), loadPayloads(config.payloads));
  const server = Bun.serve({ ...listenOptions(config), development: false, fetch: app.fetch });
  onTerminate(async () => {
    await server.stop();
    await sql.close();
  });
}

runBun(settings(process.env), import.meta.path, serveWorker);
