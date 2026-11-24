// The benchmark service on Elysia (Bun, processes sharing the port above one) with Bun's built-in
// PostgreSQL client (Bun.SQL). Handlers are async and return plain objects, which Elysia encodes.
import { Elysia } from "elysia";
import { database, listenOptions, onTerminate, runBun } from "./bun_runtime.js";
import {
  COOKIE,
  INVALID,
  NOT_FOUND,
  UNAUTHORIZED,
  articleId,
  loadPayloads,
  pageParams,
  queries,
  settings,
} from "./common.js";

function elysiaApp(db, payloads) {
  const listing = async ({ query, status }) => {
    const params = pageParams(query.page, query.page_size);
    return params ? db.articlesPage(params) : status(422, INVALID);
  };
  // The access token is checked, and its user loaded, before the protected handlers run.
  const protectedRoutes = new Elysia()
    .resolve(async ({ cookie }) => ({ user: await db.currentUser(cookie[COOKIE]?.value) }))
    .onBeforeHandle(({ user, status }) => (user ? undefined : status(401, UNAUTHORIZED)))
    .get("/auth/me", async ({ user }) => user)
    .get("/auth/articles", listing);
  const app = new Elysia()
    .get("/health", () => ({ status: "ok" }))
    .get("/db", async () => db.users())
    .get("/articles", listing)
    .get("/articles/:article_id", async ({ params, status }) => {
      const id = articleId(params.article_id);
      if (id === null) return status(422, INVALID);
      return (await db.article(id)) ?? status(404, NOT_FOUND);
    })
    .use(protectedRoutes);
  for (const name of Object.keys(payloads)) app.get(`/${name}`, async () => payloads[name]);
  return app;
}

function serveWorker(config) {
  const { sql, query } = database(config);
  const app = elysiaApp(queries(query), loadPayloads(config.payloads));
  app.listen({ ...listenOptions(config), development: false });
  onTerminate(async () => {
    await app.stop();
    await sql.close();
  });
}

runBun(settings(process.env), import.meta.path, serveWorker);
