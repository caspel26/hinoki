"""DDL for the example. Real projects will use migrations (not implemented yet)."""

SQL = (
    """
    CREATE TABLE IF NOT EXISTS blog_users (
        id uuid PRIMARY KEY,
        name text NOT NULL,
        token text UNIQUE NOT NULL,
        is_editor bool NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blog_posts (
        id uuid PRIMARY KEY,
        author_id uuid NOT NULL REFERENCES blog_users(id) ON DELETE RESTRICT,
        title text NOT NULL,
        slug text UNIQUE NOT NULL,
        body text NOT NULL,
        published bool NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
)
DROP = ("DROP TABLE IF EXISTS blog_posts", "DROP TABLE IF EXISTS blog_users")
