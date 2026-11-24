// The Hono application, shared by Hono on Node.js and Hono on Bun so the two differ only in
// runtime, server adapter and database client. Handlers are async; values are encoded by `c.json`.
import { Hono } from "hono";
import { getCookie } from "hono/cookie";
import { createMiddleware } from "hono/factory";
import { COOKIE, INVALID, NOT_FOUND, UNAUTHORIZED, articleId, pageParams } from "./common.js";

export function honoApp(db, payloads) {
  const app = new Hono();
  app.get("/health", (c) => c.json({ status: "ok" }));
  for (const name of Object.keys(payloads)) app.get(`/${name}`, async (c) => c.json(payloads[name]));
  app.get("/db", async (c) => c.json(await db.users()));
  const listing = async (c) => {
    const params = pageParams(c.req.query("page"), c.req.query("page_size"));
    return params ? c.json(await db.articlesPage(params)) : c.json(INVALID, 422);
  };
  app.get("/articles", listing);
  app.get("/articles/:article_id", async (c) => {
    const id = articleId(c.req.param("article_id"));
    if (id === null) return c.json(INVALID, 422);
    const found = await db.article(id);
    return found ? c.json(found) : c.json(NOT_FOUND, 404);
  });
  // The access token is checked, and its user loaded, before the protected handlers run.
  const authenticated = createMiddleware(async (c, next) => {
    const user = await db.currentUser(getCookie(c, COOKIE));
    if (!user) return c.json(UNAUTHORIZED, 401);
    c.set("user", user);
    await next();
  });
  app.get("/auth/me", authenticated, async (c) => c.json(c.get("user")));
  app.get("/auth/articles", authenticated, listing);
  return app;
}
