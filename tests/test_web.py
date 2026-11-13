"""Milestone 6: web view sign-in, browsing, search, editing, export, isolation."""

from __future__ import annotations

import html
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


def page_form(client, path):
    """csrf + orig hidden fields of a record page, as a browser would send them."""
    page = client.get(path).get_data(as_text=True)
    orig = re.search(r'name="orig" value="([^"]*)"', page).group(1)
    return {"csrf": csrf(client, path), "orig": html.unescape(orig)}


def test_signed_out_and_bad_links(app):
    c = app.test_client()
    assert "A memory that lives in your chats." in c.get("/").get_data(as_text=True)
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
    form = {**page_form(client, "/r/TSK-1"), "title": "Call Meera", "f:Status": "waiting", "f:Priority": "P2",
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
    base = page_form(client, "/r/TSK-1")
    r = client.post("/r/TSK-1", data={**base, "title": "Plan offsite",
                                      "present:People": "1", "f:People": [str(p2["id"])]})
    assert r.status_code == 302
    with store.tx(system=True) as tx:
        assert {l["to_id"] for l in tx.links_from(task["id"])} == {p2["id"]}
    base = page_form(client, "/r/TSK-1")
    r = client.post("/r/TSK-1", data={**base, "title": "Plan offsite", "f:Status": "someday"})
    assert r.status_code == 400 and "not an allowed option" in r.get_data(as_text=True)
    base = page_form(client, "/r/TSK-1")
    r = client.post("/r/TSK-1", data={**base, "title": "Plan offsite",
                                      "present:People": "1", "f:People": []})
    with store.tx(system=True) as tx:
        assert tx.links_from(task["id"]) == []


def test_verbatim_body_not_editable(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        th, _ = tx.create_record(tenant["id"], S, "thought", title="On focus", body="Exact words here.")
    client, _ = sign_in(app, store, settings, tenant)
    ref = f"{S.types['thought'].prefix}-1"
    client.post(f"/r/{ref}", data={**page_form(client, f"/r/{ref}"), "title": "On focus", "body": "Rewritten!"})
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
    r = client.post("/r/TSK-1", data={**page_form(client, "/r/TSK-1"), "title": "T",
                                      "f:Notes": "line one\r\nline two", "f:Status": "next"})
    assert r.status_code == 302
    with store.tx(system=True) as tx:
        assert tx.record(tenant["id"], task["id"])["user_edited_fields"] == []


def test_untouched_fields_dont_revert_bot_changes(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        task, _ = tx.create_record(tenant["id"], S, "task", title="Ship it",
                                   fields={"Status": "next", "Priority": "P2"})
    client, _ = sign_in(app, store, settings, tenant)
    base = page_form(client, "/r/TSK-1")                          # page opened: Status next
    with store.tx(system=True) as tx:                             # bot closes it meanwhile
        tx.update_record(tenant["id"], S, task["id"], {"Status": "done"})
    r = client.post("/r/TSK-1", data={**base, "title": "Ship it", "f:Status": "next", "f:Priority": "P1"})
    assert r.status_code == 302
    with store.tx(system=True) as tx:
        rec = tx.record(tenant["id"], task["id"])
    assert rec["fields"]["Status"] == "done" and rec["fields"]["Priority"] == "P1"
    assert rec["user_edited_fields"] == ["Priority"]


def test_odd_input_does_not_crash(app, store, settings, tenant):
    S = load_schema("ankur")                                     # a schema with a tracker (number fields)
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET schema_name = 'ankur' WHERE id = %s", (tenant["id"],))
        tx.create_record(tenant["id"], S, "task", title="T", fields={"Status": "next"})
        tx.create_record(tenant["id"], S, "fitness", title="Run", fields={})
    client, _ = sign_in(app, store, settings, tenant)
    assert client.get("/r/TSK-\u00b2").status_code == 404
    assert client.get("/r/TSK-99999999999999").status_code == 404
    assert client.get("/search?q=a%00b").status_code == 200
    assert client.get("/t/task?page=99999999999999999999").status_code == 200
    base = page_form(client, "/r/TSK-1")
    r = client.post("/r/TSK-1", data={**base, "title": "T\x00x", "f:Notes": "a\x00b"})
    assert r.status_code == 302
    r = client.post("/r/TSK-1", data={**page_form(client, "/r/TSK-1"), "title": "Tx"})
    assert client.post("/r/TSK-1", data={"csrf": base["csrf"], "title": "x"}).status_code == 400  # no orig
    num_field = next(f.name for f in S.types["fitness"].fields.values() if f.type == "number")
    ref = f"{S.types['fitness'].prefix}-1"
    r = client.post(f"/r/{ref}", data={**page_form(client, f"/r/{ref}"), "title": "Run",
                                       f"f:{num_field}": "nan"})
    assert r.status_code == 400 and "finite" in r.get_data(as_text=True)
    r = client.post(f"/r/{ref}", data={**page_form(client, f"/r/{ref}"), "title": "Run",
                                       f"f:{num_field}": "1234567"})
    assert "1234567" in client.get(f"/r/{ref}").get_data(as_text=True)


def test_cross_site_login_post_rejected(app, store, settings, tenant):
    path = link_for(store, FakeTelegram(), settings, tenant)
    token = path.rsplit("/", 1)[1]
    c = app.test_client()
    assert c.post("/login", data={"token": token}, headers={"Origin": "https://evil.example"}).status_code == 400
    assert c.post("/login", data={"token": token}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 400
    assert c.post("/login", data={"token": token}, headers={"Origin": "https://atya.test"}).status_code == 302


def test_messages_page_shows_conversation(app, store, settings, tenant):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    with store.tx(system=True) as tx:
        cap = tx.add_inbox(tenant["id"], source="telegram", raw_content="Met Meera, call her Friday",
                           captured_at=now - dt.timedelta(minutes=5))
        tx.finish_inbox(cap, status="processed", intent="log", notes="created PER-1, TSK-1")
        tx.create_record(tenant["id"], S, "task", title="Call Meera", source_inbox_id=cap)
        q = tx.add_inbox(tenant["id"], source="telegram", raw_content="what do I owe Meera?",
                         captured_at=now - dt.timedelta(minutes=4))
        tx.finish_inbox(q, status="responded", intent="query", notes="searched task for 'Meera'; 1 records")
        out = tx.add_outbox(tenant["id"], chat_id=1, body="A call by Friday (TSK-1).", inbox_id=q)
        tx.finish_outbox(out, ok=True)
        bad = tx.add_inbox(tenant["id"], source="telegram", raw_content="<script>x</script> broken one",
                           captured_at=now - dt.timedelta(minutes=3))
        tx.finish_inbox(bad, status="failed", notes="gave up after 3 attempts: boom")
        orphan = tx.add_outbox(tenant["id"], chat_id=1, body="Old reminder from v1")
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/messages").get_data(as_text=True)
    for text in ("Met Meera, call her Friday", "filed", "TSK-1", "what do I owe Meera?", "answered",
                 "A call by Friday", "failed", "gave up after 3 attempts", "Old reminder from v1"):
        assert text in page, text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert page.index("Met Meera") < page.index("what do I owe") < page.index("broken one")  # chat order
    assert page.index("broken one") < page.index('id="latest"')                              # opens at the end
    failed = client.get("/messages?kind=failed").get_data(as_text=True)
    assert "broken one" in failed and "Met Meera" not in failed and "Old reminder" not in failed
    questions = client.get("/messages?kind=query").get_data(as_text=True)
    assert "what do I owe" in questions and "broken one" not in questions
    found = client.get("/messages?q=owe").get_data(as_text=True)
    assert "what do I owe" in found and "Met Meera, call" not in found
    assert client.get("/messages?q=100%25_").status_code == 200
    assert client.get("/messages?kind=bogus&page=-3").status_code == 200


def test_messages_pagination_keeps_unlinked_bot_messages_in_order(app, store, settings, tenant):
    import datetime as dt
    from atya import web as webmod
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    n = webmod.PAGE_SIZE + 5
    with store.tx(system=True) as tx:
        for i in range(n):
            tx.add_inbox(tenant["id"], source="telegram", raw_content=f"note number {i:03d}",
                         captured_at=base + dt.timedelta(hours=i), status="processed")
        # A bot message between note 3 and note 4 (both on page 2).
        o = tx.add_outbox(tenant["id"], chat_id=1, body="between three and four")
        tx.conn.execute("UPDATE outbox SET created_at = %s WHERE id = %s", (base + dt.timedelta(hours=3, minutes=30), o))
    client, _ = sign_in(app, store, settings, tenant)
    p1 = client.get("/messages").get_data(as_text=True)
    p2 = client.get("/messages?page=2").get_data(as_text=True)
    assert "between three and four" not in p1
    assert p2.count("between three and four") == 1
    assert p2.index("note number 003") < p2.index("between three and four") < p2.index("note number 004")
    assert p1.index("Older messages") < p1.index("note number 005") < p1.index("note number 054")
    assert "Older messages" not in p2 and p2.index("note number 004") < p2.index("Newer messages")


def test_raw_database_view(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        other = tx.create_tenant(telegram_user_id=999_222_333, telegram_chat_id=3, display_name="other")
        tx.add_inbox(other["id"], source="telegram", raw_content="other user's raw message")
        tx.add_inbox(tenant["id"], source="telegram", raw_content="my raw message " + "x" * 700)
        tx.create_record(tenant["id"], S, "task", title="Raw task", fields={"Status": "next"})
    try:
        client, _ = sign_in(app, store, settings, tenant)
        idx = client.get("/db").get_data(as_text=True)
        assert "inbox" in idx and "audit_log" in idx and "service_state" not in idx
        inbox = client.get("/db/inbox").get_data(as_text=True)
        assert "my raw message" in inbox and "other user" not in inbox and "chars)" in inbox
        recs = client.get("/db/records").get_data(as_text=True)
        assert "Raw task" in recs and "has_embedding" in recs and "&#34;Status&#34;" in recs
        sess = client.get("/db/sessions").get_data(as_text=True)
        assert "…" in sess
        for name in ("tenants", "outbox", "record_links", "audit_log", "invites", "seq_counters",
                     "login_tokens", "attachments"):
            assert client.get(f"/db/{name}").status_code == 200, name
        assert client.get("/db/service_state").status_code == 404
        assert client.get("/db/inbox;drop").status_code == 404
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(other["id"])


def test_home_due_this_week_and_lately(app, store, settings, tenant):
    import datetime as dt
    import zoneinfo
    today = dt.datetime.now(zoneinfo.ZoneInfo(tenant["timezone"] or "UTC")).date()
    with store.tx(system=True) as tx:
        prj, _ = tx.create_record(tenant["id"], S, "project", title="VLA rig")
        tx.create_record(tenant["id"], S, "task", title="Overdue thing",
                         fields={"Status": "next", "Due Date": (today - dt.timedelta(days=2)).isoformat()})
        tx.create_record(tenant["id"], S, "task", title="Due today thing",
                         fields={"Status": "next", "Due Date": today.isoformat(), "Project": [str(prj["id"])]})
        tx.create_record(tenant["id"], S, "task", title="Done thing",
                         fields={"Status": "done", "Due Date": today.isoformat()})
        tx.create_record(tenant["id"], S, "task", title="Far future thing",
                         fields={"Status": "next", "Due Date": (today + dt.timedelta(days=30)).isoformat()})
        msg = tx.add_inbox(tenant["id"], source="telegram", raw_content="remember the rig calibration")
        tx.finish_inbox(msg, status="processed", intent="log")
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/").get_data(as_text=True)
    assert "Due this week" in page
    assert "Overdue thing" in page and "Overdue · " in page
    assert "Due today thing" in page and "VLA rig" in page and ">Today<" in page
    assert "Done thing" not in page and "Far future thing" not in page
    assert page.index("Overdue thing") < page.index("Due today thing")
    assert "remember the rig calibration" in page and "Lately" in page
    assert "Good " in page


def test_retry_failed_message(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        bad = tx.add_inbox(tenant["id"], source="telegram", raw_content="this one broke")
        tx.conn.execute("UPDATE inbox SET status = 'failed', attempts = 3 WHERE id = %s", (bad,))
        ok = tx.add_inbox(tenant["id"], source="telegram", raw_content="this one is fine")
        tx.finish_inbox(ok, status="processed", intent="log")
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/messages").get_data(as_text=True)
    assert "Retry" in page
    token = csrf(client, "/messages")
    assert client.post(f"/messages/{bad}/retry", data={"csrf": token}).status_code == 302
    assert client.post(f"/messages/{ok}/retry", data={"csrf": token}).status_code == 302
    assert client.post(f"/messages/{bad}/retry", data={}).status_code == 400          # csrf
    assert client.post("/messages/not-a-uuid/retry", data={"csrf": token}).status_code == 404
    with store.tx(system=True) as tx:
        assert tx.inbox_row(bad)["status"] == "pending" and tx.inbox_row(bad)["attempts"] == 0
        assert tx.inbox_row(ok)["status"] == "processed"


def test_pages_use_no_inline_styles_and_fonts_are_self_hosted(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="T", fields={"Status": "next"})
    client, _ = sign_in(app, store, settings, tenant)
    for path in ("/", "/t/task", "/r/TSK-1", "/messages", "/search?q=t", "/db", "/db/inbox"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'style="' not in r.get_data(as_text=True), path       # CSP blocks inline styles
        assert "font-src 'self'" in r.headers["Content-Security-Policy"]
    assert client.get("/static/fonts/inter-latin-wght.woff2").status_code == 200


def test_quick_mark_done_and_status_from_list(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        task, _ = tx.create_record(tenant["id"], S, "task", title="Ship it", fields={"Status": "next"})
    client, _ = sign_in(app, store, settings, tenant)
    lst = client.get("/t/task").get_data(as_text=True)
    assert "Mark done: Ship it" in lst and "data-autosubmit" in lst
    token = csrf(client, "/t/task")
    r = client.post("/r/TSK-1/quick", data={"csrf": token, "field": "Status", "value": "done", "back": "/t/task?show=open"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/t/task?show=open")
    with store.tx(system=True) as tx:
        rec = tx.record(tenant["id"], task["id"])
    assert rec["fields"]["Status"] == "done" and rec["fields"]["Completed At"]
    assert set(rec["user_edited_fields"]) == {"Status", "Completed At"}
    assert "Ship it" not in client.get("/t/task?show=open").get_data(as_text=True)
    assert "Ship it" in client.get("/t/task?show=done").get_data(as_text=True)
    # Off-site redirects are ignored; bad values and relation fields are refused.
    r = client.post("/r/TSK-1/quick", data={"csrf": token, "field": "Status", "value": "next", "back": "//evil.example"})
    assert r.headers["Location"].endswith("/r/TSK-1")
    assert client.post("/r/TSK-1/quick", data={"csrf": token, "field": "Status", "value": "bogus"}).status_code == 400
    assert client.post("/r/TSK-1/quick", data={"csrf": token, "field": "Project", "value": "x"}).status_code == 400
    assert client.post("/r/TSK-1/quick", data={"field": "Status", "value": "done"}).status_code == 400


def test_create_record_from_web(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        prj, _ = tx.create_record(tenant["id"], S, "project", title="VLA rig")
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/t/task/new").get_data(as_text=True)
    assert "Create" in page
    token = csrf(client, "/t/task/new")
    r = client.post("/t/task/new", data={"csrf": token, "title": "Order spare servos", "f:Status": "next",
                                         "f:Priority": "P1", "f:Due Date": "2026-12-01",
                                         "f:Project": str(prj["id"]), "body": "Two of them"})
    assert r.status_code == 302 and "/r/TSK-1" in r.headers["Location"]
    with store.tx(system=True) as tx:
        rec = tx.record_by_ref(tenant["id"], "task", 1)
        links = tx.links_from(rec["id"])
    assert rec["title"] == "Order spare servos" and rec["fields"]["Priority"] == "P1" and rec["body"] == "Two of them"
    assert {"Status", "Priority", "Due Date", "Project", "Task"} <= set(rec["user_edited_fields"]) | {"Task"}
    assert [l["to_id"] for l in links] == [prj["id"]]
    assert client.post("/t/task/new", data={"csrf": token, "title": ""}).status_code == 400
    assert client.get("/t/nonsense/new").status_code == 404


def test_delete_record_keeps_copy_in_audit(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        prj, _ = tx.create_record(tenant["id"], S, "project", title="Old project")
        task, _ = tx.create_record(tenant["id"], S, "task", title="Doomed", fields={"Project": [str(prj["id"])]})
    client, _ = sign_in(app, store, settings, tenant)
    assert "Delete TSK-1?" in client.get("/r/TSK-1/delete").get_data(as_text=True)
    token = csrf(client, "/r/TSK-1/delete")
    r = client.post("/r/TSK-1/delete", data={"csrf": token})
    assert r.status_code == 302 and "deleted=TSK-1" in r.headers["Location"]
    assert "TSK-1 was deleted" in client.get(r.headers["Location"]).get_data(as_text=True)
    assert client.get("/r/TSK-1").status_code == 404
    with store.tx(system=True) as tx:
        audit = tx._one("SELECT * FROM audit_log WHERE target_id = %s AND action = 'delete'", (task["id"],))
        assert audit["source_data"]["title"] == "Doomed" and audit["source_data"]["links"]
        assert tx.links_to(prj["id"]) == []
    assert client.post("/r/PRJ-1/delete", data={}).status_code == 400   # csrf


def test_notes_editable_but_verbatim_text_is_not(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="T", body="old details")
        msg = tx.add_inbox(tenant["id"], source="telegram", raw_content="My exact words.")
        th, _ = tx.create_record(tenant["id"], S, "thought", title="On words", source_inbox_id=msg)
    client, _ = sign_in(app, store, settings, tenant)
    r = client.post("/r/TSK-1", data={**page_form(client, "/r/TSK-1"), "title": "T", "body": "new details"})
    assert r.status_code == 302
    ref = f"{S.types['thought'].prefix}-1"
    page = client.get(f"/r/{ref}").get_data(as_text=True)
    assert 'name="body"' not in page and "My exact words." in page
    client.post(f"/r/{ref}", data={**page_form(client, f"/r/{ref}"), "title": "On words", "body": "changed!"})
    with store.tx(system=True) as tx:
        assert tx.record_by_ref(tenant["id"], "task", 1)["body"] == "new details"
        assert tx.record(tenant["id"], th["id"])["body"] == "My exact words."
        import pytest as _p
        from atya.store import StoreError
        with _p.raises(StoreError):
            tx.update_body(tenant["id"], S, th["id"], "nope")


def test_search_partial_words_fields_and_filters(app, store, settings, tenant):
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="Recalibrate the wrist camera", fields={"Status": "next"})
        tx.create_record(tenant["id"], S, "task", title="Old calibration notes", fields={"Status": "done"})
        tx.create_record(tenant["id"], S, "person", title="Meera", fields={"Notes": "works on calibration rigs"})
        m = tx.add_inbox(tenant["id"], source="telegram", raw_content="calibration went fine")
    client, _ = sign_in(app, store, settings, tenant)
    all_ = client.get("/search?q=calib").get_data(as_text=True)
    assert "Recalibrate the wrist camera" in all_ and "Old calibration notes" in all_ and "Meera" in all_
    assert "1 message mention" in all_
    tasks = client.get("/search?q=calib&type=task").get_data(as_text=True)
    assert "Meera" not in tasks and "Old calibration" in tasks
    open_ = client.get("/search?q=calib&type=task&open=1").get_data(as_text=True)
    assert "Recalibrate" in open_ and "Old calibration" not in open_
    assert client.get("/search?q=%25_%5C").status_code == 200
    assert 'id="topq"' in client.get("/").get_data(as_text=True)          # search on every page
    lst = client.get("/t/task?q=wrist").get_data(as_text=True)
    assert "Recalibrate" in lst and "Old calibration" not in lst


def test_chat_from_web_goes_through_pipeline_and_stays_on_web(app, store, settings, tenant):
    from atya import processor, sender
    from tests.test_pipeline import FakeLLM, FakeTelegram as FT
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET telegram_chat_id = 777 WHERE id = %s", (tenant["id"],))
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/messages").get_data(as_text=True)
    assert 'id="composer"' in page and 'name="text"' in page
    token = csrf(client, "/messages")
    q = "what's due this week?"
    r = client.post("/messages/send", data={"csrf": token, "text": q})
    assert r.status_code == 302 and r.headers["Location"].endswith("/messages#latest")
    with store.tx(system=True) as tx:
        row = tx._one("SELECT * FROM inbox WHERE tenant_id = %s AND raw_content = %s", (tenant["id"], q))
    assert row["source"] == "web" and row["status"] == "pending"
    # While waiting: a typing bubble, and the page knows what to poll for.
    page = client.get("/messages").get_data(as_text=True)
    assert "typing" in page and str(row["id"]) in page
    assert client.get(f"/messages/pending?ids={row['id']}").get_json() == {"pending": [str(row["id"])]}

    llm = FakeLLM()
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["task"], "search": "due"}
    llm.found[q] = False
    while processor.process_once(store, llm, settings):
        pass
    assert client.get(f"/messages/pending?ids={row['id']}").get_json() == {"pending": []}
    with store.tx(system=True) as tx:
        out = tx._one("SELECT * FROM outbox WHERE inbox_id = %s", (row["id"],))
    assert out["channel"] == "web" and out["status"] == "sent"
    tg = FT()
    sender.send_once(store, tg)
    assert all(chat != 777 for chat, *_ in tg.sent)        # nothing echoed to Telegram
    page = client.get("/messages").get_data(as_text=True)
    assert "from the web" in page and "Answer: " in page and "Save as task" in page

    # Save as task from the web.
    llm.plans[q] = {"writes": [{"type": "task", "title": "Due this week"}]}
    r = client.post(f"/messages/{row['id']}/save", data={"csrf": token})
    assert r.status_code == 302
    while processor.process_once(store, llm, settings):
        pass
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM records WHERE tenant_id = %s AND type = 'task'",
                       (tenant["id"],))["n"] == 1


def test_chat_send_edge_cases(app, store, settings, tenant):
    client, _ = sign_in(app, store, settings, tenant)
    token = csrf(client, "/messages")
    assert client.post("/messages/send", data={"text": "hi"}).status_code == 400            # csrf
    r = client.post("/messages/send", data={"csrf": token, "text": "   "})
    assert r.status_code == 302
    r = client.post("/messages/send", data={"csrf": token, "text": "/export"})
    assert "note=commands" in r.headers["Location"]
    assert "work in Telegram" in client.get("/messages?note=commands").get_data(as_text=True)
    client.post("/messages/send", data={"csrf": token, "text": "line one\r\nline two\x00"})
    with store.tx(system=True) as tx:
        rows = tx._all("SELECT raw_content FROM inbox WHERE tenant_id = %s", (tenant["id"],))
    assert [r["raw_content"] for r in rows] == ["line one\nline two"]
    assert client.get("/messages/pending?ids=nope,").get_json() == {"pending": []}


def test_pdf_upload_download_record_and_export(app, store, settings, tenant):
    from atya import processor
    from tests.pdfs import text_pdf
    from tests.test_pipeline import FakeLLM
    pdf = text_pdf(["Invoice 2213 from AWS for October usage, total 312.40 USD, due 15 November 2026."])
    client, _ = sign_in(app, store, settings, tenant)
    token = csrf(client, "/messages")
    assert 'enctype="multipart/form-data"' in client.get("/messages").get_data(as_text=True)
    r = client.post("/messages/send", data={"csrf": token, "text": "",
                                            "file": (io.BytesIO(pdf), "aws-oct.pdf", "application/pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    bad = client.post("/messages/send", data={"csrf": token, "text": "",
                                              "file": (io.BytesIO(b"not a pdf"), "x.txt", "text/plain")},
                      content_type="multipart/form-data")
    assert "note=file" in bad.headers["Location"]
    llm = FakeLLM()
    llm.plan_fanout = lambda **kw: {"writes": [{"type": "document", "title": "AWS invoice October"}]}
    while processor.process_once(store, llm, settings):
        pass
    with store.tx(system=True) as tx:
        att = tx._one("SELECT id, record_id FROM attachments WHERE tenant_id = %s", (tenant["id"],))
        doc = tx.record(tenant["id"], att["record_id"])
        reply = tx._one("SELECT body, channel FROM outbox WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
                        (tenant["id"],))
    assert reply["channel"] == "web" and reply["body"].startswith("Saved DOC-1")
    msgs = client.get("/messages").get_data(as_text=True)
    assert "aws-oct.pdf" in msgs and f"/files/{att['id']}" in msgs and "[PDF] aws-oct.pdf" in msgs
    page = client.get("/r/DOC-1").get_data(as_text=True)
    assert "aws-oct.pdf" in page and "1 page" in page and "Download" in page
    dl = client.get(f"/files/{att['id']}")
    assert dl.status_code == 200 and dl.data == pdf and "attachment" in dl.headers["Content-Disposition"]
    assert client.get("/files/not-a-uuid").status_code == 404
    raw = client.get("/db/attachments").get_data(as_text=True)
    assert "aws-oct.pdf" in raw and "312.40" in raw
    assert client.get("/db/attachment_chunks").status_code == 200
    z = zipfile.ZipFile(io.BytesIO(client.post("/export", data={"csrf": token}).data))
    names = z.namelist()
    assert any(n.startswith("files/INB-") and n.endswith("aws-oct.pdf") for n in names)
    assert any(n.endswith("aws-oct.pdf.txt") for n in names)

    # Someone else can't download it.
    with store.tx(system=True) as tx:
        other = tx.create_tenant(telegram_user_id=999_444_555, telegram_chat_id=9, display_name="other")
    try:
        other_client, _ = sign_in(app, store, settings, other)
        assert other_client.get(f"/files/{att['id']}").status_code == 404
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(other["id"])
