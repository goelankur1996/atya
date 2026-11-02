"""Row-level security: Postgres itself keeps users apart."""

from __future__ import annotations

import psycopg
import pytest

from atya.schema import load_schema

S = load_schema("default")


@pytest.fixture
def two_users(store):
    with store.tx(system=True) as tx:
        a = tx.create_tenant(telegram_user_id=111_000_001, telegram_chat_id=1, display_name="A")
        b = tx.create_tenant(telegram_user_id=111_000_002, telegram_chat_id=2, display_name="B")
    with store.tx(a["id"]) as tx:
        ia = tx.add_inbox(a["id"], source="telegram", raw_content="A's secret plan")
        tx.create_record(a["id"], S, "task", title="A's secret plan", source_inbox_id=ia)
    with store.tx(b["id"]) as tx:
        ib = tx.add_inbox(b["id"], source="telegram", raw_content="B's diary")
        tx.create_record(b["id"], S, "journal", title="B's diary", source_inbox_id=ib)
    yield a, b
    with store.tx(system=True) as tx:
        tx.delete_tenant(a["id"])
        tx.delete_tenant(b["id"])


def test_unfiltered_queries_only_see_own_rows(store, two_users):
    a, b = two_users
    # Deliberately NO tenant filter in the SQL: the database must still hide B's rows.
    with store.tx(a["id"]) as tx:
        titles = {r["title"] for r in tx._all("SELECT title FROM records")}
        raws = {r["raw_content"] for r in tx._all("SELECT raw_content FROM inbox")}
        tenants = {r["display_name"] for r in tx._all("SELECT display_name FROM tenants")}
        audits = {r["tenant_id"] for r in tx._all("SELECT tenant_id FROM audit_log")}
    assert titles == {"A's secret plan"}
    assert raws == {"A's secret plan"}
    assert tenants == {"A"}
    assert audits == {a["id"]}


def test_cannot_write_into_another_users_data(store, two_users):
    a, b = two_users
    with pytest.raises(psycopg.errors.InsufficientPrivilege):   # RLS WITH CHECK violation
        with store.tx(a["id"]) as tx:
            tx.conn.execute("INSERT INTO inbox (tenant_id, seq, source, raw_content, captured_at) "
                            "VALUES (%s, 999, 'manual', 'injected', now())", (b["id"],))
    with store.tx(a["id"]) as tx:   # updates to invisible rows silently match nothing
        n = tx.conn.execute("UPDATE records SET title = 'hacked' WHERE tenant_id = %s", (b["id"],)).rowcount
    assert n == 0
    with store.tx(b["id"]) as tx:
        assert tx._one("SELECT title FROM records")["title"] == "B's diary"


def test_no_scope_sees_nothing(store, two_users):
    with store.tx() as tx:
        assert tx._all("SELECT id FROM records") == []
        assert tx._all("SELECT id FROM tenants") == []


def test_search_is_isolated(store, two_users):
    a, b = two_users
    with store.tx(b["id"]) as tx:
        assert tx.search_text(b["id"], "secret plan") == []
        # Even passing A's id from B's scope returns nothing.
        assert tx.search_text(a["id"], "secret plan") == []


def test_system_scope_sees_everyone(store, two_users):
    with store.tx(system=True) as tx:
        titles = {r["title"] for r in tx._all("SELECT title FROM records")}
    assert {"A's secret plan", "B's diary"} <= titles


def test_scope_does_not_leak_between_transactions(store, two_users):
    a, b = two_users
    with store.tx(a["id"]) as tx:
        tx._all("SELECT 1")
    with store.tx() as tx:   # same pooled connection, new transaction: setting must be gone
        assert tx._all("SELECT id FROM records") == []
