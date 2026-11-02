"""Storage layer against a real Postgres (needs TEST_DATABASE_URL)."""

from __future__ import annotations

import psycopg
import pytest

from atya.schema import load_schema
from atya.store import StoreError

S = load_schema("default")


def test_inbox_dedup_and_seq(store, tenant):
    with store.tx(system=True) as tx:
        a = tx.add_inbox(tenant["id"], source="telegram", raw_content="hello",
                         source_message_id="telegram:1:1")
        b = tx.add_inbox(tenant["id"], source="telegram", raw_content="hello again",
                         source_message_id="telegram:1:1")
        c = tx.add_inbox(tenant["id"], source="telegram", raw_content="second",
                         source_message_id="telegram:1:2")
        assert a and c and b is None
        assert tx.inbox_row(c)["seq"] == 2


def test_inbox_raw_is_immutable(store, tenant):
    with store.tx(system=True) as tx:
        iid = tx.add_inbox(tenant["id"], source="telegram", raw_content="original")
    with pytest.raises(psycopg.errors.RaiseException):
        with store.tx(system=True) as tx:
            tx.conn.execute("UPDATE inbox SET raw_content = 'changed' WHERE id = %s", (iid,))
    with store.tx(system=True) as tx:  # status changes are fine
        tx.finish_inbox(iid, status="processed", notes="ok", intent="log")
        assert tx.inbox_row(iid)["raw_content"] == "original"


def test_audit_log_append_only(store, tenant):
    with pytest.raises(psycopg.errors.RaiseException):
        with store.tx(system=True) as tx:
            tx.conn.execute("DELETE FROM audit_log WHERE tenant_id = %s", (tenant["id"],))
    # The app role has no UPDATE grant at all; the trigger is the second line of defence.
    with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.InsufficientPrivilege)):
        with store.tx(system=True) as tx:
            tx.conn.execute("UPDATE audit_log SET summary = 'x' WHERE tenant_id = %s", (tenant["id"],))


def test_claim_is_exclusive_and_reclaims(store, tenant):
    with store.tx(system=True) as tx:
        tx.add_inbox(tenant["id"], source="manual", raw_content="claim me")
    with store.tx(system=True) as t1:
        first = [r for r in t1.claim_inbox(limit=50) if r["tenant_id"] == tenant["id"]]
    with store.tx(system=True) as t2:
        second = [r for r in t2.claim_inbox(limit=50) if r["tenant_id"] == tenant["id"]]
        assert len(first) == 1 and second == []
        # Simulate a worker that died 10 minutes ago.
        t2.conn.execute("UPDATE inbox SET claimed_at = now() - interval '10 minutes' WHERE id = %s",
                        (first[0]["id"],))
    with store.tx(system=True) as t3:
        again = [r for r in t3.claim_inbox(limit=50) if r["tenant_id"] == tenant["id"]]
        assert [r["id"] for r in again] == [first[0]["id"]] and again[0]["attempts"] == 2


def test_record_with_links_defaults_and_audit(store, tenant):
    tid = tenant["id"]
    with store.tx(system=True) as tx:
        iid = tx.add_inbox(tid, source="telegram", raw_content="Call Rahul about the rig next week")
        person, _ = tx.create_record(tid, S, "person", title="Rahul",
                                     fields={"Relationship": "friend"}, source_inbox_id=iid)
        task, dropped = tx.create_record(
            tid, S, "task", title="Call Rahul about the rig",
            fields={"Priority": "P1", "Area": "julep", "People": [str(person["id"]), "not-an-id"]},
            source_inbox_id=iid)
        assert task["fields"] == {"Priority": "P1", "Status": "inbox"}   # default applied, julep dropped
        assert any("julep" in d for d in dropped) and any("not-an-id" in d for d in dropped)
        assert [l["to_id"] for l in tx.links_from(task["id"])] == [person["id"]]
        assert task["seq"] == 1
        audits = tx._all("SELECT action, target_table FROM audit_log WHERE target_id = %s",
                         (task["id"],))
        assert audits == [{"action": "create", "target_table": "records"}]


def test_verbatim_body_enforced(store, tenant):
    tid = tenant["id"]
    with store.tx(system=True) as tx:
        iid = tx.add_inbox(tid, source="telegram", raw_content="Felt scattered today, too many tabs.")
        thought, _ = tx.create_record(tid, S, "thought", title="Scattered", source_inbox_id=iid)
        assert thought["body"] == "Felt scattered today, too many tabs."
    with pytest.raises(StoreError):
        with store.tx(system=True) as tx:
            tx.create_record(tid, S, "thought", title="Scattered", body="Felt scattered.",
                             source_inbox_id=iid)


def test_link_to_wrong_type_dropped(store, tenant):
    tid = tenant["id"]
    with store.tx(system=True) as tx:
        person, _ = tx.create_record(tid, S, "person", title="Asha")
        task, dropped = tx.create_record(tid, S, "task", title="x", fields={"Project": [str(person["id"])]})
        assert tx.links_from(task["id"]) == [] and "not a project record" in dropped[0]


def test_user_edits_are_protected(store, tenant):
    tid = tenant["id"]
    with store.tx(system=True) as tx:
        p, _ = tx.create_record(tid, S, "person", title="Meera", fields={"Role": "PM"})
        tx.update_record(tid, S, p["id"], {"Role": "Head of Product"}, actor="web", by_user=True)
        skipped = tx.update_record(tid, S, p["id"], {"Role": "Engineer", "Company": "Acme"})
        rec = tx.record(tid, p["id"])
        assert rec["fields"]["Role"] == "Head of Product" and rec["fields"]["Company"] == "Acme"
        assert rec["user_edited_fields"] == ["Role"]
        assert any("edited by user" in s for s in skipped)


def test_full_text_search(store, tenant):
    tid = tenant["id"]
    with store.tx(system=True) as tx:
        tx.create_record(tid, S, "decision", title="Use Voyage for embeddings",
                         fields={"Reasoning": "Anthropic points to it; good retrieval quality"})
        tx.create_record(tid, S, "task", title="Buy groceries")
        hits = tx.search_text(tid, "embeddings retrieval")
        assert [h["title"] for h in hits] == ["Use Voyage for embeddings"]
        assert tx.search_text(tid, "groceries", types=["decision"]) == []


def test_tenants_are_isolated(store, tenant):
    with store.tx(system=True) as tx:
        other = tx.create_tenant(telegram_user_id=999_000_111, telegram_chat_id=2)
        tx.create_record(other["id"], S, "task", title="secret plan")
        assert tx.search_text(tenant["id"], "secret plan") == []
        assert tx.list_records(tenant["id"], "task") == [] or all(
            r["tenant_id"] == tenant["id"] for r in tx.list_records(tenant["id"], "task"))


def test_outbox_cycle(store, tenant):
    with store.tx(system=True) as tx:
        oid = tx.add_outbox(tenant["id"], chat_id=42, body="You promised Rahul a call.")
    with store.tx(system=True) as tx:
        mine = [r for r in tx.claim_outbox(limit=50) if r["id"] == oid]
        assert mine and mine[0]["status"] == "sending"
        tx.finish_outbox(oid, ok=True)
        assert tx._one("SELECT status FROM outbox WHERE id = %s", (oid,))["status"] == "sent"


def test_embedding_roundtrip(store, tenant):
    with store.tx(system=True) as tx:
        r, _ = tx.create_record(tenant["id"], S, "task", title="embed me")
        tx.set_embedding(r["id"], [0.1] * 1024)
        assert tx.record(tenant["id"], r["id"])["embedded_at"] is not None


def test_delete_tenant_removes_everything(store):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=555_000_222, telegram_chat_id=3)
        iid = tx.add_inbox(t["id"], source="telegram", raw_content="bye")
        tx.create_record(t["id"], S, "thought", title="bye", source_inbox_id=iid)
    with store.tx(system=True) as tx:
        counts = tx.delete_tenant(t["id"])
        assert counts["inbox"] == 1 and counts["records"] == 1 and counts["audit_log"] >= 3
        for table in ("inbox", "records", "audit_log"):
            n = tx._one(f"SELECT count(*) AS n FROM {table} WHERE tenant_id = %s", (t["id"],))["n"]
            assert n == 0
        assert tx.tenant(t["id"]) is None


def test_search_handles_natural_questions(store, tenant):
    tid = tenant["id"]
    with store.tx(system=True) as tx:
        tx.create_record(tid, S, "decision", title="Use Voyage for embeddings",
                         fields={"Decision": "Voyage voyage-3.5"})
        hits = tx.search_text(tid, "what did I decide about the embeddings provider?")
        assert hits and hits[0]["title"] == "Use Voyage for embeddings"
        assert tx.search_text(tid, "the and of") == []   # stop words only: no query, no crash
