// Behaviour shared by the JavaScript stacks: settings, fixed payloads, SQL, input rules and response shape.
// Plain ECMAScript plus Buffer, so Node.js and Bun load the same file.

const CATEGORIES = ["books", "garden", "kitchen", "music", "outdoor", "toys"];
const TARGET_BYTES = { "json-1k": 1024, "json-10k": 10240 };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const INTEGER = /^[+-]?\d+$/;

export const NOT_FOUND = { detail: "not found" };
export const INVALID = { detail: "invalid input" };

// Same generator as payloads.py; verify.py compares the bytes with that file's output.
export function makeItems(count) {
  const items = [];
  for (let number = 1; number <= count; number += 1) {
    items.push({
      id: number,
      name: `Item ${String(number).padStart(4, "0")}`,
      description: `Deterministic sample item number ${number}.`,
      price_cents: 199 + ((number * 37) % 9000),
      category: CATEGORIES[number % CATEGORIES.length],
      in_stock: number % 3 !== 0,
    });
  }
  return items;
}

function itemsFor(target) {
  let count = 1;
  while (Buffer.byteLength(JSON.stringify(makeItems(count + 1))) <= target) count += 1;
  return makeItems(count);
}

export const BODIES = Object.fromEntries(
  Object.entries(TARGET_BYTES).map(([name, size]) => [name, Buffer.from(JSON.stringify(itemsFor(size)))]),
);

function positive(env, name, fallback) {
  const raw = env[name] ?? fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1) throw new Error(`${name} must be a positive integer, got ${raw}`);
  return value;
}

export function settings(env) {
  if (!env.BENCH_DATABASE_URL) throw new Error("BENCH_DATABASE_URL is required");
  return {
    databaseUrl: env.BENCH_DATABASE_URL,
    pool: positive(env, "BENCH_POOL", "10"),
    workers: positive(env, "BENCH_WORKERS", "4"),
    port: positive(env, "BENCH_PORT", "8801"),
  };
}

export const SQL = {
  count: "SELECT count(*) AS total FROM posts WHERE published",
  list:
    "SELECT p.id, p.title, p.body, p.published, p.created_at, a.id AS author_id, a.name AS author_name " +
    "FROM posts p JOIN authors a ON a.id = p.author_id WHERE p.published " +
    "ORDER BY p.created_at DESC, p.id DESC LIMIT $1 OFFSET $2",
  detail:
    "SELECT p.id, p.title, p.body, p.published, p.created_at, a.id AS author_id, a.name AS author_name " +
    "FROM posts p JOIN authors a ON a.id = p.author_id WHERE p.id = $1 AND p.published",
  insert:
    "INSERT INTO posts (id, author_id, title, body, published, created_at) VALUES ($1, $2, $3, $4, true, $5)",
  author: "SELECT id, name FROM authors WHERE id = $1",
};

export const isUuid = (value) => typeof value === "string" && UUID.test(value);

function integer(raw, fallback, low, high) {
  if (raw === undefined || raw === null) return fallback;
  if (!INTEGER.test(raw)) return null;
  const value = Number(raw);
  return value >= low && value <= high ? value : null;
}

// `page >= 1` and `1 <= page_size <= 100`; null means the request is a 422.
export function pageParams(page, pageSize) {
  const number = integer(page, 1, 1, Number.MAX_SAFE_INTEGER);
  const size = integer(pageSize, 20, 1, 100);
  return number === null || size === null ? null : { page: number, pageSize: size };
}

// Lengths count code points, as Python and PostgreSQL do, not UTF-16 units.
const length = (text) => [...text].length;

export function postInput(data) {
  if (data === null || typeof data !== "object" || Array.isArray(data)) return null;
  const { author_id: authorId, title, body } = data;
  if (!isUuid(authorId) || typeof title !== "string" || typeof body !== "string") return null;
  if (length(title) < 1 || length(title) > 120 || length(body) < 1) return null;
  return { authorId, title, body };
}

export function postOut(row) {
  return {
    id: row.id,
    title: row.title,
    body: row.body,
    published: row.published,
    created_at: row.created_at,
    author: { id: row.author_id, name: row.author_name },
  };
}

export function pageOut(rows, total, { page, pageSize }) {
  return {
    items: rows.map(postOut),
    page,
    page_size: pageSize,
    total,
    pages: Math.ceil(total / pageSize),
  };
}

export function createdOut(id, input, createdAt, author) {
  return postOut({
    id,
    title: input.title,
    body: input.body,
    published: true,
    created_at: createdAt,
    author_id: author.id,
    author_name: author.name,
  });
}
