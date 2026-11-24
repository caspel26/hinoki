// Bun process model and database client shared by Elysia and Hono on Bun.
// With one worker the server runs in this process. With more, a launcher starts `BENCH_WORKERS`
// server processes sharing the port with SO_REUSEPORT (as in Django Bolt's comparison: "Bun 8
// processes with reusePort"), so the kernel spreads connections over them. A worker that dies
// outside a shutdown stops the whole server, so a broken stack is never measured.
import { SQL as Client } from "bun";
import { SQL } from "./common.js";

// Bun.SQL's `unsafe` sends a JavaScript array as "1,2,3", which PostgreSQL rejects for a
// `bigint[]` parameter, so the id list is sent as an array literal.
const parameter = (value) => (Array.isArray(value) ? `{${value.join(",")}}` : value);

// Bun's built-in PostgreSQL client with `config.pool` connections and prepared statements, and the
// `query(name, values)` function common.js's `queries` expects.
export function database(config) {
  const sql = new Client({ url: config.databaseUrl, max: config.pool, prepare: true });
  return { sql, query: (name, values) => sql.unsafe(SQL[name], values.map(parameter)) };
}

// Options for Bun.serve: the port is shared only when there is more than one process.
export const listenOptions = (config) => ({
  hostname: "127.0.0.1",
  port: config.port,
  reusePort: config.workers > 1,
});

// Stop the server and close the pool on SIGTERM; exit anyway after `graceMs`.
export function onTerminate(close, graceMs = 5000) {
  process.once("SIGTERM", async () => {
    setTimeout(() => process.exit(0), graceMs).unref();
    await close();
    process.exit(0);
  });
}

function launch(config, entry) {
  let stopping = false;
  const children = [];
  const stopAll = () => {
    stopping = true;
    for (const child of children) child.kill("SIGTERM");
  };
  for (let index = 0; index < config.workers; index += 1) {
    const child = Bun.spawn([process.execPath, entry], {
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

// Serve in this process, or launch `config.workers` copies of `entry` (the app's own file).
export function runBun(config, entry, serveWorker) {
  if (config.workers === 1 || process.env.BENCH_BUN_WORKER === "1") serveWorker(config);
  else launch(config, entry);
}
