"""Signing in on the web with a code sent to WhatsApp or Telegram; the public homepage."""

from __future__ import annotations

import re

import pytest

from atya import receiver, sender, web, whatsapp
from atya.config import Settings
from atya.web import parse_signin_identity
from tests.test_pipeline import FakeTelegram
from tests.test_whatsapp import FakeWhatsApp

WA = "919800000077"


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x", owner_telegram_user_id=1,
                    web_base_url="https://atya.test", whatsapp_token="t", whatsapp_app_secret="s",
                    whatsapp_phone_number_id="p", whatsapp_verify_token="v", whatsapp_number="91")


@pytest.fixture
def users(store):
    with store.tx(system=True) as tx:
        tx.conn.execute("DELETE FROM signin_codes")
        a = tx.create_tenant(telegram_user_id=880006, telegram_chat_id=880006, display_name="Tia", timezone="UTC")
        tx.conn.execute("UPDATE tenants SET telegram_username = 'tia_k' WHERE id = %s", (a["id"],))
        b = tx.create_tenant(telegram_user_id=None, telegram_chat_id=None, whatsapp_phone=WA, display_name="Wes",
                             timezone="UTC")
        tx.conn.execute("UPDATE tenants SET whatsapp_last_inbound = now() WHERE id = %s", (b["id"],))
    yield a, b
    with store.tx(system=True) as tx:
        tx.delete_tenant(a["id"])
        tx.delete_tenant(b["id"])


def code_from(text):
    return re.search(r"\b(\d{6})\b", text).group(1)


def ask(client, who):
    r = client.post("/signin", data={"who": who})
    assert r.status_code == 302
    return re.search(r"h=([0-9a-f-]+)", r.location).group(1)


def test_homepage_and_signin_pages(store, settings):
    c = web.create_app(store, settings).test_client()
    home = c.get("/").get_data(as_text=True)
    assert "ATYA is a personal assistant that lives in your chats." in home and 'href="/signin"' in home
    assert 'href="/terms"' in home and 'href="/privacy"' in home and "style=" not in home
    page = c.get("/signin").get_data(as_text=True)
    assert "Send code" in page and 'value="+91"' in page and 'href="/terms"' in page and "style=" not in page
    terms = c.get("/terms").get_data(as_text=True)
    assert "Terms of service" in terms and "train AI models" in terms and "style=" not in terms


def test_telegram_code_signs_in(store, settings, users):
    tia, _ = users
    c = web.create_app(store, settings).test_client()
    handle = ask(c, "@Tia_K")
    tg = FakeTelegram()
    sender.send_once(store, tg)
    msg = [t for chat, t, *_ in tg.sent if chat == 880006][-1]
    assert "sign-in code" in msg and "atya.test" in msg
    r = c.post("/signin/code", data={"h": handle, "code": code_from(msg)})
    assert r.status_code == 302 and r.location.endswith("/")
    assert "Tia" in c.get("/").get_data(as_text=True) or c.get("/messages").status_code == 200
    # Single use.
    assert web.create_app(store, settings).test_client().post(
        "/signin/code", data={"h": handle, "code": code_from(msg)}).status_code == 400


def test_whatsapp_code_uses_the_authentication_template(store, settings, users):
    c = web.create_app(store, settings).test_client()
    handle = ask(c, "+91 98000 00077")
    wa = FakeWhatsApp()
    whatsapp.send_once(store, wa)
    tpl = wa.templates[-1]["template"]
    assert tpl["name"] == "atya_login_code"
    code = tpl["components"][0]["parameters"][0]["text"]
    assert tpl["components"][1]["parameters"][0]["text"] == code
    assert c.post("/signin/code", data={"h": handle, "code": code}).status_code == 302


def test_unknown_account_looks_the_same_and_wrong_codes_run_out(store, settings, users):
    c = web.create_app(store, settings).test_client()
    page_known = c.get(f"/signin/code?h={ask(c, '@tia_k')}&via=telegram").get_data(as_text=True)
    unknown = ask(c, "@nobody_here")
    page_unknown = c.get(f"/signin/code?h={unknown}&via=telegram").get_data(as_text=True)
    assert page_known.replace(re.search(r'name="h" value="([^"]+)"', page_known).group(1), "") == \
        page_unknown.replace(unknown, "")                                   # nothing reveals who exists
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM outbox WHERE body LIKE '%%sign-in code%%' AND chat_id = 0")["n"] == 0
    assert c.post("/signin/code", data={"h": unknown, "code": "123456"}).status_code == 400

    handle = ask(c, "@tia_k")
    tg = FakeTelegram()
    sender.send_once(store, tg)
    real = code_from([t for chat, t, *_ in tg.sent if chat == 880006][-1])
    wrong = "000000" if real != "000000" else "111111"
    for _ in range(5):
        assert c.post("/signin/code", data={"h": handle, "code": wrong}).status_code == 400
    assert c.post("/signin/code", data={"h": handle, "code": real}).status_code == 400   # 5 tries used up


def test_rate_limits(store, settings, users):
    c = web.create_app(store, settings).test_client()
    for _ in range(7):
        ask(c, "@tia_k")
    with store.tx(system=True) as tx:
        sent = tx._one("SELECT count(*) AS n FROM signin_codes WHERE tenant_id IS NOT NULL")["n"]
    assert sent == 5                                                           # per account per hour
    for _ in range(13):
        c.post("/signin", data={"who": "@someone_else"})
    assert c.post("/signin", data={"who": "@tia_k"}).status_code == 429        # per address per hour


def test_cross_site_posts_are_refused(store, settings, users):
    c = web.create_app(store, settings).test_client()
    assert c.post("/signin", data={"who": "@tia_k"}, headers={"Origin": "https://evil.example"}).status_code == 400
    assert c.post("/signin/code", data={"h": "x", "code": "1"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 400


def test_telegram_username_is_kept_current(store, settings):
    tg = FakeTelegram()
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=880007, telegram_chat_id=880007, timezone="UTC")
    try:
        with store.tx(system=True) as tx:
            tx.set_state(receiver.OFFSET_KEY, None)
        tg.push("hi", user_id=880007, chat_id=880007)
        tg.queue[-1]["message"]["from"]["username"] = "New_Name"
        receiver.poll_once(store, tg, settings, timeout=0)
        with store.tx(system=True) as tx:
            assert tx.tenant(t["id"])["telegram_username"] == "new_name"
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(t["id"])


def test_parse_identity():
    assert parse_signin_identity("+91 97119 66131") == ("whatsapp", "919711966131")
    assert parse_signin_identity("9711966131") == ("whatsapp", "919711966131")
    assert parse_signin_identity("+1 (415) 555-0100") == ("whatsapp", "14155550100")
    assert parse_signin_identity("@Ankur_G") == ("telegram", "ankur_g")
    assert parse_signin_identity("hi") == (None, None)
    assert parse_signin_identity("12345") == (None, None)
