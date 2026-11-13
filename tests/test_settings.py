"""The Settings page: account fields, channel linking, data controls, deleting an account."""
from __future__ import annotations

import datetime as dt
import re

import pytest

from atya import web
from atya.config import Settings
from atya.schema import load_schema
from tests.test_web import csrf, sign_in


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=1, owner_timezone="Asia/Kolkata", web_base_url="https://atya.test")


@pytest.fixture
def user(store):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=880021, telegram_chat_id=880021, display_name="Noor",
                             timezone="Asia/Kolkata")
    yield t
    with store.tx(system=True) as tx:
        if tx.tenant(t["id"]):
            tx.delete_tenant(t["id"])


@pytest.fixture
def client(store, settings, user):
    app = web.create_app(store, settings)
    app.extensions["atya_bot"]["name"] = "atya_test_bot"
    c, _ = sign_in(app, store, settings, user)
    return c


def test_page_renders_every_section(client):
    page = client.get("/settings").get_data(as_text=True)
    for heading in ("Contact ATYA", "Account", "Connectors", "Data controls", "Account management"):
        assert heading in page
    assert "Never used to train AI models" in page and "Linked" in page and "style=" not in page
    assert 'href="https://t.me/atya_test_bot"' in page
    assert 'href="/settings"' in client.get("/").get_data(as_text=True)          # nav link


def test_name_timezone_and_brief(client, store, user):
    token = csrf(client, "/settings")
    r = client.post("/settings/name", data={"csrf": token, "name": "  Noor   Shah "})
    assert r.status_code == 302 and r.location.endswith("#account")
    assert client.post("/settings/name", data={"csrf": token, "name": " "}).location.count("error=") == 1
    assert client.post("/settings/timezone", data={"csrf": token, "timezone": "Mars/Base"}).location.count("error=") == 1
    client.post("/settings/timezone", data={"csrf": token, "timezone": "Europe/London"})
    client.post("/settings/brief", data={"csrf": token, "brief": "07:30"})
    with store.tx(system=True) as tx:
        me = tx.tenant(user["id"])
    assert (me["display_name"], me["timezone"], me["brief_time"]) == ("Noor Shah", "Europe/London", dt.time(7, 30))
    assert "Every day at 7:30 am" in client.get("/settings").get_data(as_text=True)
    client.post("/settings/brief", data={"csrf": token, "brief": "07:30", "off": "1"})
    with store.tx(system=True) as tx:
        assert tx.tenant(user["id"])["brief_time"] is None


def test_posts_need_csrf(client):
    assert client.post("/settings/name", data={"name": "X"}).status_code == 400


def test_link_the_other_channel(client, store, settings):
    r = client.post("/settings/link", data={"csrf": csrf(client, "/settings")})
    assert r.status_code == 302 and "link=" in r.location
    page = client.get(r.location).get_data(as_text=True)
    # WhatsApp isn't set up in this test, and Telegram is already linked: no link to show.
    assert "this link" not in page


def test_delete_synced_events(client, store, user):
    S = load_schema("default")
    with store.tx(user["id"]) as tx:
        tx.create_record(user["id"], S, "event", title="Standup", fields={"Start": "2026-11-14T10:00:00"},
                         external_id="gcal:x1", enforce_verbatim=False, actor="google")
        tx.create_record(user["id"], S, "task", title="Mine", enforce_verbatim=False, actor="web")
    r = client.post("/settings/google/clear", data={"csrf": csrf(client, "/settings")})
    assert "Deleted+1" in r.location or "Deleted%201" in r.location
    with store.tx(user["id"]) as tx:
        left = [x["title"] for x in tx._all("SELECT title FROM records WHERE tenant_id = %s", (user["id"],))]
    assert left == ["Mine"]


def test_delete_account_needs_the_word(client, store, user):
    page = client.get("/settings/delete").get_data(as_text=True)
    assert "Type <b>delete</b> to confirm" in page and "style=" not in page
    token = csrf(client, "/settings/delete")
    assert client.post("/settings/delete", data={"csrf": token, "confirm": "yes"}).status_code == 400
    with store.tx(system=True) as tx:
        assert tx.tenant(user["id"])
    r = client.post("/settings/delete", data={"csrf": token, "confirm": " Delete "})
    assert r.status_code == 200 and "Account deleted" in r.get_data(as_text=True)
    assert re.search(r"atya_session=;|Expires=Thu, 01 Jan 1970", r.headers.get("Set-Cookie", ""))
    with store.tx(system=True) as tx:
        assert tx.tenant(user["id"]) is None
    assert client.get("/settings").status_code in (302, 401, 403)


def test_old_connect_page_redirects(client):
    r = client.get("/connect?error=x")
    assert r.status_code == 302 and "/settings" in r.location and r.location.endswith("#connectors")
