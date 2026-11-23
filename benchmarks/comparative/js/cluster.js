// Node.js process model: one primary forks `workers` processes with node:cluster.
// The primary accepts connections and hands them out round-robin (Node's default on Linux).
// A worker that dies outside a shutdown stops the whole server, so a broken stack is never measured.
import cluster from "node:cluster";

function primary(workers) {
  let stopping = false;
  const stopAll = (signal) => {
    stopping = true;
    for (const worker of Object.values(cluster.workers)) worker.process.kill(signal);
  };
  cluster.on("exit", (worker, code, signal) => {
    if (stopping) return;
    console.error(`worker ${worker.process.pid} exited (${signal ?? code}); stopping`);
    process.exitCode = 1;
    stopAll("SIGTERM");
  });
  process.on("SIGTERM", () => stopAll("SIGTERM"));
  process.on("SIGINT", () => stopAll("SIGTERM"));
  for (let index = 0; index < workers; index += 1) cluster.fork();
}

export function runCluster(config, startWorker) {
  if (cluster.isPrimary) {
    primary(config.workers);
    return;
  }
  startWorker(config).catch((error) => {
    console.error(error);
    process.exit(1);
  });
}

// Close the server and the pool on SIGTERM; exit anyway after `graceMs`.
export function onTerminate(close, graceMs = 5000) {
  process.once("SIGTERM", () => {
    setTimeout(() => process.exit(0), graceMs).unref();
    close().then(
      () => process.exit(0),
      () => process.exit(1),
    );
  });
}
