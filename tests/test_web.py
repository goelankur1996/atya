"""Milestone 6: web view sign-in, browsing, search, editing, export, isolation."""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from atya import commands, web
from atya.config import Settings
from atya.schema import load_schema
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, run_all

S = load_schema("default")


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata",
                    web_base_url="https://atya.test")


@pytest.fixture
def app(store, settings):
    a = web.create_app(store, settings)
    a.testing = True
    return a


def link_for(store, tg, settings, tenant) -> str:
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=tenant["telegram_user_id"],
                       chat_id=1, name=None, tenant=tenant)
    assert commands.web_link(ctx) == "web:link-sent"
    m = re.search(r"https://atya\.test(/login/\S+)", tg.sent[-1][1])
    assert m and tg.last_preview is False
    return m.group(1)


def sign_in(app, store, settings, tenant):
    client = app.test_client()
    path = link_for(store, FakeTelegram(), settings, tenant)
    token = path.rsplit("/", 1)[1]
    assert client.get(path).status_code == 200          # preview fetch: shows a button only
    r = client.post("/login", data={"token": token})
    assert r.status_code == 302
    return client, token


def csrf(client, path="/"):
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', client.get(path).get_data(as_text=True))
    assert m
    return m.group(1)


def test_signed_out_and_bad_links(app):
    c = app.test_client()
    assert "Send <code>/web</code>" in c.get("/").get_data(as_text=True)
    assert c.get("/t/task").status_code == 302
    assert c.post("/login", data={"token": "nope"}).status_code == 400


def test_web_off_without_base_url(store, tenant, db_url):
    tg = FakeTelegram()
    s = Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                 owner_telegram_user_id=OWNER)
    ctx = commands.Ctx(store=store, tg=tg, settings=s, user_id=1, chat_id=1, name=None, tenant=tenant)
    assert commands.web_link(ctx) == "web:off"


def test_link_works_once_and_preview_does_not_burn_it(app, store, settings, tenant):
    client, token = sign_in(app, store, settings, tenant)
    assert "Download export" in client.get("/").get_data(as_text=True)
    assert app.test_client().post("/login", data={"token": token}).status_code == 400
    with store.tx(system=True) as tx:
        tokens = tx._all("SELECT token_hash FROM login_tokens WHERE tenant_id = %s", (tenant["id"],))
        assert all(token not in t["token_hash"] for t in tokens)   # only hashes stored


def test_expired_link_rejected(app, store, settings, tenant):
    path = link_for(store, FakeTelegram(), settings, tenant)
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE login_tokens SET expires_at = now() - interval '1 minute' "
                        "WHERE tenant_id = %s", (tenant["id"],))
    assert app.test_client().post("/login", data={"token": path.rsplit("/", 1)[1]}).status_code == 400


def test_browse_search_and_record_page(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        prj, _ = tx.create_record(tenant["id"], S, "project", title="VLA rig")
        task, _ = tx.create_record(tenant["id"], S, "task", title="Mount the wrist camera",
                                   fields={"Status": "next", "Due Date": "2026-11-06",
                                           "Project": [str(prj["id"])]})
    client, _ = sign_in(app, store, settings, tenant)
    home = client.get("/").get_data(as_text=True)
    assert "Tasks" in home and "Projects" in home
    lst = client.get("/t/task").get_data(as_text=True)
    assert "TSK-1" in lst and "Mount the wrist camera" in lst and "next" in lst
    found = client.get("/search?q=camera").get_data(as_text=True)
    assert "TSK-1" in found
    page = client.get("/r/TSK-1").get_data(as_text=True)
    assert "Mount the wrist camera" in page and "2026-11-06" in page
    proj_page = client.get("/r/prj-1").get_data(as_text=True)      # case-insensitive ref
    assert "Linked from" in proj_page and "TSK-1" in proj_page
    assert client.get("/r/TSK-99").status_code == 404
    assert client.get("/r/NOPE-1").status_code == 404
    assert client.get("/t/nonsense").status_code == 404


def test_edit_marks_only_changed_fields_and_bot_respects_them(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        task, _ = tx.create_record(tenant["id"], S, "task", title="Call Meera",
                                   fields={"Status": "next", "Priority": "P2", "Due Date": "2026-11-06"})
    client, _ = sign_in(app, store, settings, tenant)
    token = csrf(client, "/r/TSK-1")
    form = {"csrf": token, "title": "Call Meera", "f:Status": "waiting", "f:Priority": "P2",
            "f:Due Date": "", "f:Notes": "", "f:Completed At": "", "f:Area": "",
            "present:Project": "1", "present:People": "1"}
    r = client.post("/r/TSK-1", data=form)
    assert r.status_code == 302 and "saved=1" in r.headers["Location"]
    with store.tx(system=True) as tx:
        rec = tx.record(tenant["id"], task["id"])
        assert rec["fields"]["Status"] == "waiting"
        assert "Due Date" not in rec["fields"]                        # cleared
        assert sorted(rec["user_edited_fields"]) == ["Due Date", "Status"]
        dropped = tx.update_record(tenant["id"], S, task["id"], {"Status": "done", "Priority": "P0"})
        rec = tx.record(tenant["id"], task["id"])
        assert rec["fields"]["Status"] == "waiting" and rec["fields"]["Priority"] == "P0"
        assert any("edited by user" in d for d in dropped)
        hist = tx.audit_for(tenant["id"], task["id"])
        assert any(h["actor"] == "web" for h in hist)
    assert "you, on the web" in client.get("/r/TSK-1").get_data(as_text=True)


def test_edit_relations_and_bad_values(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        p1, _ = tx.create_record(tenant["id"], S, "person", title="Meera")
        p2, _ = tx.create_record(tenant["id"], S, "person", title="David")
        task, _ = tx.create_record(tenant["id"], S, "task", title="Plan offsite",
                                   fields={"People": [str(p1["id"])]})
    client, _ = sign_in(app, store, settings, tenant)
    token = csrf(client, "/r/TSK-1")
    r = client.post("/r/TSK-1", data={"csrf": token, "title": "Plan offsite",
                                      "present:People": "1", "f:People": [str(p2["id"])]})
    assert r.status_code == 302
    with store.tx(system=True) as tx:
        assert {l["to_id"] for l in tx.links_from(task["id"])} == {p2["id"]}
    r = client.post("/r/TSK-1", data={"csrf": token, "title": "Plan offsite", "f:Status": "someday"})
    assert r.status_code == 400 and "not an allowed option" in r.get_data(as_text=True)
    r = client.post("/r/TSK-1", data={"csrf": token, "title": "Plan offsite",
                                      "present:People": "1", "f:People": []})
    with store.tx(system=True) as tx:
        assert tx.links_from(task["id"]) == []


def test_verbatim_body_not_editable(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        th, _ = tx.create_record(tenant["id"], S, "thought", title="On focus", body="Exact words here.")
    client, _ = sign_in(app, store, settings, tenant)
    ref = f"{S.types['thought'].prefix}-1"
    token = csrf(client, f"/r/{ref}")
    client.post(f"/r/{ref}", data={"csrf": token, "title": "On focus", "body": "Rewritten!"})
    with store.tx(system=True) as tx:
        assert tx.record(tenant["id"], th["id"])["body"] == "Exact words here."


def test_csrf_required(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="X", fields={"Status": "next"})
    client, _ = sign_in(app, store, settings, tenant)
    assert client.post("/r/TSK-1", data={"title": "Y", "f:Status": "done"}).status_code == 400
    assert client.post("/export", data={}).status_code == 400
    assert client.post("/logout", data={"csrf": "0" * 64}).status_code == 400


def test_export_download_and_logout(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="Something", fields={"Status": "next"})
    client, _ = sign_in(app, store, settings, tenant)
    token = csrf(client)
    r = client.post("/export", data={"csrf": token})
    assert r.status_code == 200 and r.mimetype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert "raw_inbox.jsonl" in names and "records/task.csv" in names
    assert client.post("/logout", data={"csrf": token}).status_code == 302
    assert client.get("/t/task").status_code == 302


def test_users_cannot_see_each_other(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        other = tx.create_tenant(telegram_user_id=999_123_456, telegram_chat_id=2, display_name="other")
        tx.create_record(other["id"], S, "task", title="Other person's secret task")
        tx.create_record(tenant["id"], S, "task", title="My task")
    try:
        client, _ = sign_in(app, store, settings, tenant)
        assert "secret" not in client.get("/t/task").get_data(as_text=True)
        assert "secret task" not in client.get("/search?q=secret").get_data(as_text=True)
        # Both users have a TSK-1; the ref resolves to the signed-in user's own record.
        assert "My task" in client.get("/r/TSK-1").get_data(as_text=True)
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(other["id"])


def test_inactive_user_session_ignored(app, store, settings, tenant):
    client, _ = sign_in(app, store, settings, tenant)
    with store.tx(system=True) as tx:
        tx.set_tenant_status(tenant["id"], "suspended")
    assert client.get("/t/task").status_code == 302


def test_web_command_through_bot(store, settings):
    tg, llm = FakeTelegram(), FakeLLM()
    with store.tx(system=True) as tx:
        old = tx.tenant_by_telegram(OWNER)
        if old:
            tx.delete_tenant(old["id"])
        tx.conn.execute("DELETE FROM service_state")
    tg.push("/start")
    tg.push("/web")
    run_all(store, tg, llm, settings)
    assert "https://atya.test/login/" in tg.sent[-1][1]
    assert "/web" in commands.HELP


def test_untouched_multiline_text_is_not_marked_edited(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        task, _ = tx.create_record(tenant["id"], S, "task", title="T",
                                   fields={"Notes": "line one\nline two ", "Status": "next"})
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/r/TSK-1").get_data(as_text=True)
    assert "<textarea" in page
    r = client.post("/r/TSK-1", data={"csrf": csrf(client, "/r/TSK-1"), "title": "T",
                                      "f:Notes": "line one\r\nline two", "f:Status": "next"})
    assert r.status_code == 302
    with store.tx(system=True) as tx:
        assert tx.record(tenant["id"], task["id"])["user_edited_fields"] == []
