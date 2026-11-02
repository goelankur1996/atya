"""Service-level settings. Per-user settings live in the `tenants` table."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
SCHEMA_DIR = ROOT / "schema"

# Must match records.embedding in migrations/001_init.sql.
EMBEDDING_DIM = 1024


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url
