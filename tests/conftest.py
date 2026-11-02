"""Test fixtures. Tests that need Postgres run only when TEST_DATABASE_URL is set.

The database is dropped and recreated per test session, then migrated.
Example: TEST_DATABASE_URL=postgresql://postgres@/atya_test?host=/tmp&port=5433
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from atya.migrate import migrate
from atya.store import Store

URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture(scope="session")
def db_url() -> str:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    info = psycopg.conninfo.conninfo_to_dict(URL)
    dbname = info.pop("dbname")
    admin = psycopg.conninfo.make_conninfo(**info, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        c.execute(f'CREATE DATABASE "{dbname}"')
    with psycopg.connect(URL) as c:
        migrate(c)
    return URL


@pytest.fixture(scope="session")
def store(db_url: str):
    s = Store(db_url)
    yield s
    s.close()


@pytest.fixture
def tenant(store: Store) -> dict:
    with store.tx(system=True) as tx:
        return tx.create_tenant(telegram_user_id=int(uuid.uuid4().int % 10**12),
                                telegram_chat_id=1, display_name="test")
