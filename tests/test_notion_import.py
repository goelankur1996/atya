"""Notion importer against a fake Notion (canned pages in the API's shape)."""

from __future__ import annotations

import uuid

import pytest

from atya import notion_import as ni
from atya.schema import load_schema

S = load_schema("default")


def rt(text):  return {"type": "rich_text", "rich_text": [{"plain_text": text}] if text else []}
def title(text): return {"type": "title", "title": [{"plain_text": text}]}
def sel(name): return {"type": "select", "select": {"name": name} if name else None}
def date(d):   return {"type": "date", "date": {"start": d} if d else None}
def num(n):    return {"type": "number", "number": n}
def uid(n):    return {"type": "unique_id", "unique_id": {"number": n, "prefix": None}}
def rel(*ids): return {"type": "relation", "id": "r", "relation": [{"id": i} for i in ids], "has_more": False}


def page(props, created="2026-07-01T10:00:00.000Z", pid=None):
    return {"id": pid or str(uuid.uuid4()), "created_time": created,
            "last_edited_time": "2026-07-02T10:00:00.000Z", "properties": props}


class FakeNotion:
    def __init__(self):
        self.dbs: dict[str, list[dict]] = {ds: [] for ds in
                                           [ni.INBOX_DS, ni.OUTBOX_DS, ni.AUDIT_DS, *ni.TYPE_SOURCES.values()]}
        self.bodies: dict[str, str] = {}
        self.body_calls = 0

    def query_all(self, ds_id):
        return self.dbs[ds_id]

    def page_text(self, page_id):
        self.body_calls += 1
        return self.bodies.get(page_id, "")

    def relation_ids(self, page, prop):
        return [r["id"] for r in page["properties"][prop].get("relation", [])]


@pytest.fixture
def notion():
    n = FakeNotion()
    long_text = "Long capture. " * 200   # > 2000 chars: v1 kept only 2000 in the property
    inb1 = page({"Title": title("Met Rahul"), "Raw Content": rt("Met Rahul from Addverb, send audit"),
                 "Source": sel("telegram"), "Status": sel("processed"),
                 "Source Message ID": rt("telegram:555:42"), "Captured At": date("2026-07-01T10:00:00+00:00"),
                 "Inbox ID": uid(7)})
    inb2 = page({"Title": title("Long"), "Raw Content": rt(long_text[:2000]), "Source": sel("telegram"),
                 "Status": sel("processed"), "Inbox ID": uid(8)})
    n.bodies[inb2["id"]] = long_text.strip()
    n.dbs[ni.INBOX_DS] = [inb1, inb2]

    proj = page({"Title": title("10-Pullup Goal"), "Status": sel("active"), "Area": sel("health"),
                 "Project ID": uid(3)})
    person = page({"Name": title("Rahul"), "Company": rt("Addverb"), "Relationship": sel("contact"),
                   "Source": rt(inb1["id"]), "Person ID": uid(12)})
    task = page({"Title": title("Send Rahul the audit"), "Status": sel("next"), "Area": sel("julep"),
                 "Priority": sel("P1"), "Due Date": date("2026-07-04"), "Task ID": uid(41),
                 "People": rel(person["id"]), "Project": rel(proj["id"]), "Source": rt(inb1["id"])})
    thought = page({"Title": title("Scattered"), "Content": rt("Felt scattered today."), "Mood": sel("scattered"),
                    "Thought ID": uid(5)})
    fit = page({"Entry": title("Session"), "Pullups (max)": num(2), "Date": date("2026-07-01"),
                "Project": rel(proj["id"])})
    n.bodies[proj["id"]] = "Goal: 10 clean pullups by December."
    n.dbs[ni.TYPE_SOURCES["project"]] = [proj]
    n.dbs[ni.TYPE_SOURCES["person"]] = [person]
    n.dbs[ni.TYPE_SOURCES["task"]] = [task]
    n.dbs[ni.TYPE_SOURCES["thought"]] = [thought]
    n.dbs[ni.TYPE_SOURCES["fitness"]] = [fit]
    n.dbs[ni.OUTBOX_DS] = [page({"Title": title("reply"), "Body": rt("You owe Rahul the audit."),
                                 "Status": sel("sent"), "Chat ID": rt("555"), "Channel": sel("telegram")}),
                           page({"Title": title("stuck"), "Body": rt("never sent"), "Status": sel("pending"),
                                 "Chat ID": rt("C0SLACK")})]
    n.dbs[ni.AUDIT_DS] = [page({"Title": title("x"), "Action": sel("create"), "Actor": rt("processor"),
                                "Target DB": sel("tasks"), "Summary": rt("created task"),
                                "Target ID": rt(task["id"])})]
    n.ids = {"inb1": inb1["id"], "task": task["id"], "person": person["id"], "proj": proj["id"]}
    return n


def run_import(store, tenant, notion, **kw):
    return ni.Importer(store, notion, S, tenant, **kw).run()


def test_full_import(store, tenant, notion):
    tid = tenant["id"]
    report = run_import(store, tenant, notion)
    with store.tx(system=True) as tx:
        inbox = {r["seq"]: r for r in tx._all("SELECT * FROM inbox WHERE tenant_id = %s", (tid,))}
        assert set(inbox) == {7, 8}                                     # Inbox IDs kept
        assert inbox[7]["source_message_id"] == "telegram:555:42"       # future Telegram dedup works
        assert inbox[8]["raw_content"] == ("Long capture. " * 200).strip()   # full text from page body
        task = tx._one("SELECT * FROM records WHERE tenant_id = %s AND type = 'task'", (tid,))
        assert task["seq"] == 41                                        # TSK-41 stays TSK-41
        assert task["fields"]["Area"] == "julep"                        # deprecated kept for history
        assert "Status" in task["fields"] and task["source_inbox_id"] == inbox[7]["id"]
        assert task["created_at"].year == 2026 and task["created_at"].month == 7
        linked = {l["field"] for l in tx.links_from(task["id"])}
        assert linked == {"People", "Project"}
        thought = tx._one("SELECT body FROM records WHERE tenant_id = %s AND type = 'thought'", (tid,))
        assert thought["body"] == "Felt scattered today."
        proj = tx._one("SELECT body FROM records WHERE tenant_id = %s AND type = 'project'", (tid,))
        assert proj["body"] == "Goal: 10 clean pullups by December."
        fit = tx._one("SELECT id FROM records WHERE tenant_id = %s AND type = 'fitness'", (tid,))
        assert [l["field"] for l in tx.links_from(fit["id"])] == ["Project"]
        out = {r["body"]: r["status"] for r in tx._all("SELECT * FROM outbox WHERE tenant_id = %s", (tid,))}
        assert out == {"You owe Rahul the audit.": "sent", "never sent": "cancelled"}
        assert tx._one("SELECT count(*) AS n FROM audit_log WHERE tenant_id = %s AND actor = 'v1:processor'",
                       (tid,))["n"] == 1
        # New captures continue after the imported numbers.
        assert tx.next_seq(tid, "record:task") == 42
        assert tx.next_seq(tid, "inbox") == 9
    assert report.imported["task"] == 1 and report.links == 3   # task->person, task->project, fitness->project


def test_rerun_only_adds_new(store, tenant, notion):
    run_import(store, tenant, notion)
    notion.dbs[ni.TYPE_SOURCES["task"]].append(page({"Title": title("New one"), "Task ID": uid(42)}))
    report = run_import(store, tenant, notion)
    assert report.imported["task"] == 1 and report.skipped["task"] == 1
    assert report.imported["inbox"] == 0 and report.skipped["inbox"] == 2
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM records WHERE tenant_id = %s AND type = 'task'",
                       (tenant["id"],))["n"] == 2


def test_dry_run_writes_nothing(store, tenant, notion):
    report = run_import(store, tenant, notion, dry_run=True)
    assert report.imported["task"] == 1
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM records WHERE tenant_id = %s", (tenant["id"],))["n"] == 0


def test_blocks_to_text():
    blocks = [
        {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Plan"}]}},
        {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"plain_text": "one"}]}},
        {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"plain_text": "two"}]}},
        {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "ship"}], "checked": True}},
        {"type": "paragraph", "paragraph": {"rich_text": []}},
    ]
    assert ni.blocks_to_text(blocks) == "## Plan\n1. one\n2. two\n- [x] ship"


def test_imported_records_are_searchable_and_skip_defaults(store, tenant, notion):
    run_import(store, tenant, notion)
    notion.dbs[ni.TYPE_SOURCES["task"]].append(page({"Title": title("No status task"), "Task ID": uid(50)}))
    run_import(store, tenant, notion)
    with store.tx(system=True) as tx:
        t = tx._one("SELECT fields FROM records WHERE tenant_id = %s AND seq = 50 AND type = 'task'",
                    (tenant["id"],))
        assert "Status" not in t["fields"]          # no invented 'inbox' default on history
        assert tx.search_text(tenant["id"], "Rahul audit")
