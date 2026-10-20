"""Alembic environment — DATABASE_URL from env (see app.db)."""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Make `app` importable when alembic is run from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config = context.config
if config.config_file_name is not None:
    # Keep the app loggers alive when migrations run in-process at startup.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Prefer DATABASE_URL; fall back to discrete PG* vars used by docker-compose.
def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # SQLAlchemy wants postgresql:// (psycopg3: postgresql+psycopg://)
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://") and "+psycopg" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url
    user = os.environ.get("POSTGRES_USER", "terminal")
    password = os.environ.get("POSTGRES_PASSWORD", "terminal")
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "terminal")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
