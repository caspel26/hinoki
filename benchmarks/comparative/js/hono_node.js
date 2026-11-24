// The benchmark service on Hono with @hono/node-server (Node.js, node:cluster above one process)
// and node-postgres (`pg`).
import { serve } from "@hono/node-server";
import pg from "pg";
import { onTerminate, runCluster } from "./cluster.js";
import { SQL, loadPayloads, queries, settings } from "./common.js";
import { honoApp } from "./hono_routes.js";

// Named queries make `pg` prepare each statement once per connection.
const runner = (pool) => async (name, values) => (await pool.query({ name, text: SQL[name], values })).rows;

async function worker(config) {
  const pool = new pg.Pool({ connectionString: config.databaseUrl, max: config.pool });
  const app = honoApp(queries(runner(pool)), loadPayloads(config.payloads));
  const server = serve({ fetch: app.fetch, hostname: "127.0.0.1", port: config.port });
  onTerminate(async () => {
    await new Promise((resolve) => server.close(resolve));
    await pool.end();
  });
}

runCluster(settings(process.env), worker);
