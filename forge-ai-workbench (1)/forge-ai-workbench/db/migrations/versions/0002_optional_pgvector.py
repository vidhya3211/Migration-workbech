"""Optional semantic retrieval: pgvector extension + embeddings table.

Azure Database for PostgreSQL ships pgvector (allow-list ``VECTOR`` in ``azure.extensions``). A server without
the extension still runs everything - certified retrieval falls back to PostgreSQL full-text search.

Revision ID: 0002
Revises: 0001
"""
from alembic import context, op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _vector_available(conn) -> bool:
    return bool(conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar())


def upgrade() -> None:
    # generating a SQL script (alembic upgrade head --sql) has no server to ask: emit the statements for review
    if not context.is_offline_mode() and not _vector_available(op.get_bind()):
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_embedding (
            doc_id varchar(200) PRIMARY KEY REFERENCES retrieval_document(doc_id) ON DELETE CASCADE,
            embedding vector(1536) NOT NULL
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_embedding_hnsw ON retrieval_embedding "
               "USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS retrieval_embedding")
