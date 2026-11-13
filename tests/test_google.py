"""Google Calendar + birthdays: connecting (OAuth), syncing, the brief, safety rules."""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from atya import briefs, commands, connections, google, processor, web
from atya.config import Settings
from atya.schema import load_schema
from tests.test_pipeline import FakeLLM, FakeTelegram
from tests.test_web import csrf, sign_in

KEY = Fernet.generate_key().decode()
NOW = dt.datetime(2026, 11, 13, 6, 0, tzinfo=dt.timezone.utc)          # 11:30 am in Kolkata


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=1, owner_timezone="Asia/Kolkata", web_base_url="https://atya.test",
                    google_client_id="cid", google_client_secret="secret", connections_key=KEY)


@pytest.fixture
def user(store):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=880005, telegram_chat_id=880005, display_name="Ira",
                             timezone="Asia/Kolkata")
    yield t
    with store.tx(system=True) as tx:
        tx.delete_tenant(t["id"])


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.content = body, status, b"x"
        self.ok = status < 400

    def json(self):
        return self.body


class FakeGoogleHTTP:
    """Answers Calendar and People calls."""

    def __init__(self):
        self.events = {"primary": [
            {"id": "e1", "status": "confirmed", "summary": "Pricing call with Meera",
             "start": {"dateTime": "2026-11-13T09:30:00Z"}, "end": {"dateTime": "2026-11-13T10:00:00Z"},
             "location": "Zoom", "htmlLink": "https://calendar.google.com/e1",
             "attendees": [{"email": "me@x.com", "self": True}, {"displayName": "Meera", "email": "m@x.com"}]},
            {"id": "e2", "status": "confirmed", "summary": "Diwali holiday", "start": {"date": "2026-11-14"},
             "end": {"date": "2026-11-15"}},
        ]}
        self.people = [{"resourceName": "people/c1", "names": [{"displayName": "Rohan Mehta"}],
                        "birthdays": [{"date": {"month": 11, "day": 20}}]},
                       {"resourceName": "people/c2", "names": [{"displayName": "Far Away"}],
                        "birthdays": [{"date": {"month": 6, "day": 1, "year": 1990}}]}]
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        assert headers["Authorization"] == "Bearer access-1"
        if url.endswith("/calendarList"):
            return Resp({"items": [{"id": "primary", "summary": "Ira", "primary": True},
                                   {"id": "hidden", "summary": "Other", "selected": False}]})
        if "/calendars/" in url:
            cal = urllib.parse.unquote(url.split("/calendars/")[1].split("/")[0])
            return Resp({"items": self.events.get(cal, [])})
        if "/people/me/connections" in url:
            return Resp({"connections": self.people})
        raise AssertionError(url)


def connect(store, user, settings, expires=None):
    vault = google.Vault(KEY)
    with store.tx(user["id"]) as tx:
        tx.save_connection(user["id"], provider="google", account="ira@gmail.com", scopes=google.SCOPES,
                           refresh_token=vault.seal("refresh-1"), access_token=vault.seal("access-1"),
                           expires_at=expires or NOW + dt.timedelta(hours=1))
    with store.tx(system=True) as tx:
        return next(c for c in tx.connections_due(15) if c["tenant_id"] == user["id"])


def records(store, user, type_key):
    with store.tx(user["id"]) as tx:
        return tx._all("SELECT * FROM records WHERE tenant_id = %s AND type = %s ORDER BY title", (user["id"], type_key))


def test_vault_and_auth_url():
    v = google.Vault(KEY)
    blob = v.seal("secret-token")
    assert b"secret-token" not in blob and v.open(blob) == "secret-token"
    url = google.GoogleOAuth("cid", "s").auth_url(state="abc", redirect_uri="https://atya.test/cb")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["state"] == ["abc"]
    assert "calendar.readonly" in q["scope"][0] and "contacts.readonly" in q["scope"][0]
    assert "calendar " not in q["scope"][0] + " "                              # read-only scopes only


def test_sync_copies_events_and_birthdays_then_applies_changes(store, settings, user):
    conn = connect(store, user, settings)
    http = FakeGoogleHTTP()
    summary = connections.sync_one(store, None, google.Vault(KEY), conn, http=http, now=NOW)
    assert "2 new events" in summary and "1 birthday reminders" in summary
    evs = records(store, user, "event")
    call = next(e for e in evs if e["title"] == "Pricing call with Meera")
    assert call["fields"]["Start"] == "2026-11-13T15:00:00" and call["fields"]["Attendees"] == "Meera"
    assert call["fields"]["Calendar"] == "Ira" and call["fields"]["Location"] == "Zoom"
    hol = next(e for e in evs if e["title"] == "Diwali holiday")
    assert hol["fields"]["Start"] == "2026-11-14" and hol["fields"]["All Day"] is True
    assert not any("hidden" in c for c in http.calls)                          # hidden calendars skipped

    tasks = records(store, user, "task")
    assert [(t["title"], t["fields"]["Remind At"]) for t in tasks] == \
        [("Wish Rohan Mehta a happy birthday", "2026-11-20T09:00:00")]
    people = records(store, user, "person")
    assert [(p["title"], p["fields"]["Birthday"]) for p in people] == [("Rohan Mehta", "11-20")]   # Far Away: not soon
    with store.tx(user["id"]) as tx:
        inbox = tx._all("SELECT * FROM inbox WHERE tenant_id = %s", (user["id"],))
    assert len(inbox) == 1 and inbox[0]["source"] == "google" and inbox[0]["status"] == "processed"
    assert all(r["source_inbox_id"] == inbox[0]["id"] for r in evs + tasks + people)

    # Nothing changed: no new inbox row, no duplicates.
    assert connections.sync_one(store, None, google.Vault(KEY), conn, http=http, now=NOW) == "no changes"
    # The user edits the location; then the event moves and Diwali is cancelled.
    S = load_schema("default")
    with store.tx(user["id"]) as tx:
        tx.update_record(user["id"], S, call["id"], {"Location": "Office"}, by_user=True)
    http.events["primary"][0].update(summary="Pricing call (moved)", start={"dateTime": "2026-11-13T11:30:00Z"},
                                     location="Zoom 2")
    http.events["primary"][1]["status"] = "cancelled"
    summary = connections.sync_one(store, None, google.Vault(KEY), conn, http=http, now=NOW)
    assert "1 changed events" in summary and "1 cancelled events" in summary
    evs = {e["external_id"]: e for e in records(store, user, "event")}
    moved = evs["gcal:primary:e1"]
    assert moved["title"] == "Pricing call (moved)" and moved["fields"]["Start"] == "2026-11-13T17:00:00"
    assert moved["fields"]["Location"] == "Office"                              # the user's edit stands
    assert evs["gcal:primary:e2"]["fields"]["Status"] == "cancelled"


def test_expired_access_is_refreshed_and_revoked_access_asks_to_reconnect(store, settings, user):
    class OAuth:
        def __init__(self, fail=False):
            self.fail = fail

        def refresh(self, rt):
            assert rt == "refresh-1"
            if self.fail:
                raise google.GoogleError("token: invalid_grant", revoked=True)
            return {"access_token": "access-1", "expires_in": 3600}
    conn = connect(store, user, settings, expires=NOW - dt.timedelta(minutes=1))
    connections.sync_one(store, OAuth(), google.Vault(KEY), conn, http=FakeGoogleHTTP(), now=NOW)
    with store.tx(user["id"]) as tx:
        assert tx.connection(user["id"])["expires_at"] > NOW

    with store.tx(user["id"]) as tx:
        tx.update_connection(tx.connection(user["id"])["id"], last_sync_at=None,
                             expires_at=NOW - dt.timedelta(hours=1))
    connections.sync_due(store, OAuth(fail=True), google.Vault(KEY))
    with store.tx(user["id"]) as tx:
        c = tx.connection(user["id"])
        msg = tx._one("SELECT body FROM outbox WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1", (user["id"],))
    assert c["status"] == "error" and "invalid_grant" in c["error"]
    assert "send /connect" in msg["body"]


def test_brief_lists_todays_calendar(store, settings, user):
    conn = connect(store, user, settings)
    connections.sync_one(store, None, google.Vault(KEY), conn, http=FakeGoogleHTTP(), now=NOW)
    with store.tx(user["id"]) as tx:
        text = briefs.build_brief(tx, load_schema("default"), user["id"], dt.date(2026, 11, 13))
        tomorrow = briefs.build_brief(tx, load_schema("default"), user["id"], dt.date(2026, 11, 14))
    assert "Calendar today\n- 3:00 pm: Pricing call with Meera (Zoom)" in text
    assert "Calendar today\n- all day: Diwali holiday" in tomorrow


def test_messages_never_create_events(store, settings, user):
    S = load_schema("default")
    with store.tx(user["id"]) as tx:
        iid = tx.add_inbox(user["id"], source="telegram", raw_content="meeting tomorrow")
        row = tx._one("SELECT * FROM inbox WHERE id = %s", (iid,))
        notes = processor.FanoutExecutor(tx, S, tx.tenant(user["id"]), row).run(
            {"writes": [{"type": "event", "title": "Meeting", "fields": {"Start": "2026-11-14T10:00"}}]})
        assert tx._one("SELECT count(*) AS n FROM records WHERE tenant_id = %s", (user["id"],))["n"] == 0
    assert "calendar events come from the connected calendar" in notes


def test_connect_flow_on_the_web(store, settings, user, monkeypatch):
    app = web.create_app(store, settings)
    c, _ = sign_in(app, store, settings, user)
    page = c.get("/settings").get_data(as_text=True)
    assert "Google Calendar and birthdays" in page and "style=" not in page
    r = c.post("/connect/google", data={"csrf": csrf(c, "/settings")})
    assert r.status_code == 302 and r.location.startswith("https://accounts.google.com/")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.location).query)["state"][0]

    assert "expired" in c.get("/connect/google/callback?state=forged&code=x", follow_redirects=True).get_data(as_text=True)

    seen = {}

    def exchange(self, *, code, redirect_uri):
        seen["redirect"] = redirect_uri
        return {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
                "scope": google.SCOPES}
    monkeypatch.setattr(google.GoogleOAuth, "exchange", exchange)
    monkeypatch.setattr(google.GoogleOAuth, "email", lambda self, t: "ira@gmail.com")
    revoked = []
    monkeypatch.setattr(google.GoogleOAuth, "revoke", lambda self, t: revoked.append(t))
    r = c.get(f"/connect/google/callback?state={state}&code=abc")
    assert r.status_code == 302 and seen["redirect"] == "https://atya.test/connect/google/callback"
    with store.tx(user["id"]) as tx:
        conn = tx.connection(user["id"])
    assert conn["account"] == "ira@gmail.com" and b"refresh-1" not in bytes(conn["refresh_token"])
    assert google.Vault(KEY).open(conn["refresh_token"]) == "refresh-1"
    assert c.get(f"/connect/google/callback?state={state}&code=abc").location.endswith(
        "/connect?error=That+sign-in+link+expired.+Try+connecting+again.")          # state is single use
    assert "Connected as ira@gmail.com" in c.get("/settings").get_data(as_text=True)

    c.post("/connect/google/disconnect", data={"csrf": csrf(c, "/settings")})
    with store.tx(user["id"]) as tx:
        assert tx.connection(user["id"]) is None
    assert revoked == ["refresh-1"]


def test_calendar_scope_refused_connects_nothing(store, settings, user, monkeypatch):
    app = web.create_app(store, settings)
    c, _ = sign_in(app, store, settings, user)
    r = c.post("/connect/google", data={"csrf": csrf(c, "/settings")})
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.location).query)["state"][0]
    monkeypatch.setattr(google.GoogleOAuth, "exchange", lambda self, **k: {
        "access_token": "a", "refresh_token": "r", "scope": "openid email"})
    revoked = []
    monkeypatch.setattr(google.GoogleOAuth, "revoke", lambda self, t: revoked.append(t))
    page = c.get(f"/connect/google/callback?state={state}&code=abc", follow_redirects=True).get_data(as_text=True)
    assert "Calendar access wasn" in page and revoked == ["r"]
    with store.tx(user["id"]) as tx:
        assert tx.connection(user["id"]) is None


def test_connect_command_link_lands_on_connect_page(store, settings, user):
    tg = FakeTelegram()
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=880005, chat_id=880005, name=None, tenant=user)
    assert commands.handle(ctx, "/connect") == "web:link-sent"
    path = re.search(r"https://atya\.test(/login/\S+)", tg.sent[-1][1]).group(1)
    assert path.endswith("?next=/settings")
    app = web.create_app(store, settings)
    c = app.test_client()
    form = c.get(path).get_data(as_text=True)
    token = re.search(r'name="token" value="([^"]+)"', form).group(1)
    nxt = re.search(r'name="next" value="([^"]+)"', form).group(1)
    r = c.post("/login", data={"token": token, "next": nxt})
    assert r.location.endswith("/settings")
    assert c.post("/login", data={"token": "x", "next": "https://evil.example"}).status_code == 400
    off = replace(settings, google_client_id=None)
    ctx.settings = off
    assert commands.handle(ctx, "/connect") == "connect:off"


def test_csp_lets_the_connect_form_reach_google(store, settings, user):
    # Browsers apply form-action to the redirect after a form post: without Google
    # listed, pressing Connect silently does nothing.
    app = web.create_app(store, settings)
    c, _ = sign_in(app, store, settings, user)
    csp = c.get("/settings").headers["Content-Security-Policy"]
    assert "form-action 'self' https://accounts.google.com;" in csp
