// Behaviour shared by the JavaScript stacks: settings, payloads, SQL, the access token and the response shapes.
// Plain ECMAScript plus node:crypto and node:fs, so Node.js and Bun load the same file.
import { createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

export const COOKIE = "access_token";
const SECRET = "hinoki-bolt-parity-benchmark-secret-not-for-production";
const INTEGER = /^[+-]?\d+$/;
const SUBJECT = /^\d+$/;

export const NOT_FOUND = { detail: "Article not found" };
export const UNAUTHORIZED = { detail: "Authentication required" };
export const INVALID = { detail: "invalid input" };

function positive(env, name, fallback) {
  const raw = env[name] ?? fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1) throw new Error(`${name} must be a positive integer, got ${raw}`);
  return value;
}

export function settings(env) {
  if (!env.BENCH_DATABASE_URL) throw new Error("BENCH_DATABASE_URL is required");
  if (!env.BENCH_PAYLOADS) throw new Error("BENCH_PAYLOADS is required");
  return {
    databaseUrl: env.BENCH_DATABASE_URL,
    payloads: env.BENCH_PAYLOADS,
    pool: positive(env, "BENCH_POOL", "8"),
    workers: positive(env, "BENCH_WORKERS", "1"),
    port: positive(env, "BENCH_PORT", "8801"),
  };
}

// The two `json-*` values, written by run.py from parity_data.py; each framework encodes them itself.
export const loadPayloads = (path) => JSON.parse(readFileSync(path, "utf8"));

// The statements of Django Bolt's handlers: COUNT, the page joined with its author, then the tags
// of every article on the page in one query; the detail adds the comments.
const ARTICLE =
  "SELECT a.id, a.title, a.content, a.published, a.created_at, a.updated_at, " +
  "u.id AS author_id, u.name AS author_name, u.email AS author_email, u.bio AS author_bio, " +
  "u.created_at AS author_created_at FROM articles_article a JOIN articles_author u ON u.id = a.author_id";
export const SQL = {
  users: "SELECT id, username, email, first_name, last_name, is_active FROM benchmark_users LIMIT 10",
  count: "SELECT count(*) AS total FROM articles_article",
  page: `${ARTICLE} ORDER BY a.created_at DESC LIMIT $1 OFFSET $2`,
  article: `${ARTICLE} WHERE a.id = $1`,
  tags:
    "SELECT l.article_id, t.id, t.name, t.slug FROM articles_tag t " +
    "JOIN articles_article_tags l ON l.tag_id = t.id WHERE l.article_id = ANY($1::bigint[])",
  comments:
    "SELECT id, article_id, author_name, content, created_at FROM articles_comment " +
    "WHERE article_id = $1 ORDER BY created_at DESC",
  user: "SELECT id, username, email, first_name, last_name, is_active FROM auth_user WHERE id = $1",
};

function integer(raw, fallback) {
  if (raw === undefined || raw === null) return fallback;
  return INTEGER.test(raw) ? Number(raw) : null;
}

// Out-of-range values are clamped as Django Bolt's handlers do; a non-integer is a 422.
export function pageParams(page, pageSize) {
  const number = integer(page, 1);
  const size = integer(pageSize, 20);
  if (number === null || size === null) return null;
  return { page: Math.max(1, number), pageSize: Math.max(1, Math.min(100, size)) };
}

export const articleId = (raw) => (INTEGER.test(raw) ? Number(raw) : null);

const decode = (part) => JSON.parse(Buffer.from(part, "base64url").toString("utf8"));

// The user id of a valid HS256 access token (signature, `exp` and `sub` required), else null.
// Each framework reads the cookie itself (Hono's `getCookie`, Elysia's `cookie`).
export function tokenSubject(token, now = Date.now() / 1000) {
  const parts = typeof token === "string" ? token.split(".") : [];
  if (parts.length !== 3) return null;
  try {
    if (decode(parts[0]).alg !== "HS256") return null;
    const expected = createHmac("sha256", SECRET).update(`${parts[0]}.${parts[1]}`).digest();
    const signature = Buffer.from(parts[2], "base64url");
    if (signature.length !== expected.length || !timingSafeEqual(signature, expected)) return null;
    const claims = decode(parts[1]);
    if (typeof claims.exp !== "number" || claims.exp <= now) return null;
    return typeof claims.sub === "string" && SUBJECT.test(claims.sub) ? Number(claims.sub) : null;
  } catch {
    return null;
  }
}

export const userOut = (row) => ({
  id: Number(row.id),
  username: row.username,
  email: row.email,
  first_name: row.first_name,
  last_name: row.last_name,
  is_active: row.is_active,
});

export function articleOut(row, tags) {
  return {
    id: Number(row.id),
    title: row.title,
    content: row.content,
    published: row.published,
    author: {
      id: Number(row.author_id),
      name: row.author_name,
      email: row.author_email,
      bio: row.author_bio,
      created_at: row.author_created_at,
    },
    tags: tags.get(Number(row.id)) ?? [],
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

// Tag rows grouped by article id, in the order the database returned them.
export function tagsByArticle(rows) {
  const grouped = new Map();
  for (const row of rows) {
    const id = Number(row.article_id);
    if (!grouped.has(id)) grouped.set(id, []);
    grouped.get(id).push({ id: Number(row.id), name: row.name, slug: row.slug });
  }
  return grouped;
}

export const commentOut = (row) => ({
  id: Number(row.id),
  article_id: Number(row.article_id),
  author_name: row.author_name,
  content: row.content,
  created_at: row.created_at,
});

export function pageOut(rows, tags, total, { page, pageSize }) {
  return {
    items: rows.map((row) => articleOut(row, tags)),
    total,
    page,
    page_size: pageSize,
    pages: Math.ceil(total / pageSize),
  };
}

// The handlers' database work over a `query(name, values)` function that returns the rows.
export function queries(query) {
  const articlesPage = async (params) => {
    const [counted] = await query("count", []);
    const rows = await query("page", [params.pageSize, (params.page - 1) * params.pageSize]);
    const tags = await query("tags", [rows.map((row) => row.id)]);
    return pageOut(rows, tagsByArticle(tags), Number(counted.total), params);
  };
  const article = async (id) => {
    const [row] = await query("article", [id]);
    if (!row) return null;
    const tags = await query("tags", [[row.id]]);
    const comments = await query("comments", [id]);
    return { ...articleOut(row, tagsByArticle(tags)), comments: comments.map(commentOut) };
  };
  const users = async () => (await query("users", [])).map(userOut);
  // The user of a request's access token, or null; the row is read only for a valid token.
  const currentUser = async (token) => {
    const subject = tokenSubject(token);
    if (subject === null) return null;
    const [row] = await query("user", [subject]);
    return row ? userOut(row) : null;
  };
  return { articlesPage, article, users, currentUser };
}
