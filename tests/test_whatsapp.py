"""WhatsApp: webhook, sign-up by invite, linking, messages through the pipeline, sender rules."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time

import pytest

from atya import commands, processor, receiver, sender, whatsapp
from atya.config import Settings
from atya.web import create_app
from tests.pdfs import text_pdf
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram

FRIEND = "919800000001"
OWNER_WA = "919800000002"
STRANGER = "919800000003"
PHONE_ID = "1496646980190962"
SECRET = "app-secret"


class FakeWhatsApp:
    def __init__(self):
        self.sent: list[tuple[str, str, list | None]] = []
        self.files: dict[str, bytes] = {}
        self.fail_with: whatsapp.WhatsAppError | None = None
        self._n = 0

    def send_message(self, chat_id, text, reply_to=None, buttons=None, preview=True):
        if self.fail_with:
            raise self.fail_with
        self._n += 1
        self.sent.append((str(chat_id), text, buttons))
        return {"message_id": f"wamid.out{self._n}"}

    def send_document(self, *a, **k):
        raise whatsapp.WhatsAppError("no")

    def answer_callback(self, *a, **k):
        pass

    def bot_username(self):
        return ""

    def download_file(self, media_id, max_bytes):
        return self.files[media_id]

    def texts(self, phone):
        return [t for c, t, _ in self.sent if c == phone]


_n = [0]


def event(phone, *, text=None, tap=None, doc=None, name="Friend"):
    _n[0] += 1
    msg = {"from": phone, "id": f"wamid.in{_n[0]}-{time.time_ns()}", "timestamp": str(int(time.time()))}
    if text is not None:
        msg.update(type="text", text={"body": text})
    elif tap is not None:
        msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": tap, "title": "x"}})
    elif doc is not None:
        msg.update(type="document", document=doc)
    return {"object": "whatsapp_business_account", "entry": [{"id": "1425781482591751", "changes": [{
        "field": "messages", "value": {"messaging_product": "whatsapp",
                                       "metadata": {"phone_number_id": PHONE_ID},
                                       "contacts": [{"wa_id": phone, "profile": {"name": name}}],
                                       "messages": [msg]}}]}]}


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    web_base_url="https://atya.test", whatsapp_token="t", whatsapp_app_secret=SECRET,
                    whatsapp_phone_number_id=PHONE_ID, whatsapp_verify_token="verify-me",
                    whatsapp_number="918920396095")


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        for t in tx._all("SELECT id FROM tenants WHERE telegram_user_id = %s OR whatsapp_phone = ANY(%s)",
                         (OWNER, [FRIEND, OWNER_WA, STRANGER])):
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
        tx.conn.execute("DELETE FROM whatsapp_events")
    yield


def deliver(store, wa, settings, payload, tg=None):
    with store.tx(system=True) as tx:
        tx.add_whatsapp_event(payload)
    whatsapp.process_once(store, wa, settings, tg)


def by_phone(store, phone):
    with store.tx(system=True) as tx:
        return tx.tenant_by_whatsapp(phone)


def owner_invite(store, settings):
    tg = FakeTelegram()
    tg.push("/start")
    tg.push("/invite")
    receiver.poll_once(store, tg, settings, timeout=0)
    msg = tg.sent[-1][1]
    return msg.split("Invite code (one use): ")[1].split()[0], msg, tg


# --- payloads and signatures --------------------------------------------------------

def test_payloads_buttons_list_and_long_text():
    p = whatsapp.build_payloads("91", "hi", None)
    assert p == [{"messaging_product": "whatsapp", "recipient_type": "individual", "to": "91",
                  "type": "text", "text": {"body": "hi", "preview_url": False}}]
    p = whatsapp.build_payloads("91", "Reminder", [[("Done", "rem:done:1"), ("In 1 hour", "rem:1h:1"),
                                                     ("Tomorrow 9am", "rem:tmrw:1")]])
    assert len(p) == 1 and p[0]["interactive"]["type"] == "button"
    assert [b["reply"]["id"] for b in p[0]["interactive"]["action"]["buttons"]] == \
        ["rem:done:1", "rem:1h:1", "rem:tmrw:1"]
    p = whatsapp.build_payloads("91", "Pick", commands.tz_buttons())
    assert p[0]["interactive"]["type"] == "list"
    rows = p[0]["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 8 and rows[0] == {"id": "tz:Asia/Kolkata", "title": "India"}
    # Too long for a button message: the text goes first, the button after.
    p = whatsapp.build_payloads("91", "x" * 2000, [[("Save as task", "save:1")]])
    assert [m["type"] for m in p] == ["text", "interactive"]
    assert p[1]["interactive"]["body"]["text"] == "Choose:"


def test_signature():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert whatsapp.valid_signature(SECRET, body, good)
    assert not whatsapp.valid_signature(SECRET, body + b" ", good)
    assert not whatsapp.valid_signature(SECRET, body, None)
    assert not whatsapp.valid_signature(SECRET, body, "sha1=abc")


def test_webhook_verify_and_receive(store, settings, clean):
    client = create_app(store, settings).test_client()
    r = client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=12345")
    assert r.status_code == 200 and r.data == b"12345"
    assert client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1").status_code == 403
    assert client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=verify-me"
                      "&hub.challenge=<script>").status_code == 403

    body = json.dumps(event(STRANGER, text="hello")).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/whatsapp/webhook", data=body, headers={"X-Hub-Signature-256": "sha256=00"},
                       content_type="application/json").status_code == 403
    r = client.post("/whatsapp/webhook", data=body, headers={"X-Hub-Signature-256": sig},
                    content_type="application/json")
    assert r.status_code == 200
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM whatsapp_events")["n"] == 1
    # The queue is invisible outside system scope.
    with store.tx() as tx:
        assert tx._one("SELECT count(*) AS n FROM whatsapp_events")["n"] == 0


def test_webhook_off_without_settings(store, db_url):
    off = Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x", owner_telegram_user_id=OWNER)
    client = create_app(store, off).test_client()
    assert client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=1").status_code == 404
    assert client.post("/whatsapp/webhook", data=b"{}").status_code == 404


# --- people -----------------------------------------------------------------------

def test_stranger_one_reply_a_day_nothing_stored(store, settings, clean):
    wa = FakeWhatsApp()
    for t in ("hi", "/help", "anyone?"):
        deliver(store, wa, settings, event(STRANGER, text=t))
    assert wa.texts(STRANGER) == [commands.INVITE_ONLY]
    assert by_phone(store, STRANGER) is None
    with store.tx(system=True) as tx:      # processed events are deleted
        assert tx._one("SELECT count(*) AS n FROM whatsapp_events")["n"] == 0


def test_invite_signup_onboarding_and_a_message_through_the_pipeline(store, settings, clean):
    code, invite_msg, _ = owner_invite(store, settings)
    assert f"https://wa.me/918920396095?text=/start%20{code}" in invite_msg

    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(FRIEND, text=f"/start {code}", name="Ravi"))
    t = by_phone(store, FRIEND)
    assert t["status"] == "invited" and t["display_name"] == "Ravi" and t["channel"] == "whatsapp"
    assert t["telegram_user_id"] is None
    assert wa.sent[-1][2] == commands.tz_buttons()          # the timezone picker

    deliver(store, wa, settings, event(FRIEND, tap="tz:Europe/London"))
    t = by_phone(store, FRIEND)
    assert t["status"] == "active" and t["timezone"] == "Europe/London"
    assert wa.texts(FRIEND)[-1].startswith("Timezone set to Europe/London")

    deliver(store, wa, settings, event(FRIEND, text="hello there"))
    llm = FakeLLM()
    llm.intent["hello there"] = "chat"
    while processor.process_once(store, llm, settings):
        pass
    with store.tx(t["id"]) as tx:
        inbox = tx._one("SELECT * FROM inbox WHERE source = 'whatsapp' AND intent IS DISTINCT FROM 'command'")
        out = tx._all("SELECT * FROM outbox WHERE inbox_id = %s", (inbox["id"],))
    assert inbox["status"] == "responded"
    assert len(out) == 1 and out[0]["channel"] == "whatsapp" and out[0]["chat_id"] == int(FRIEND)

    # Telegram's sender leaves it alone; WhatsApp's sends it and keeps the id.
    tg = FakeTelegram()
    sender.send_once(store, tg)
    assert tg.sent == []
    assert whatsapp.send_once(store, wa) == 1
    with store.tx(t["id"]) as tx:
        row = tx._one("SELECT * FROM outbox WHERE id = %s", (out[0]["id"],))
    assert row["status"] == "sent" and row["channel_message_id"].startswith("wamid.out")

    # A later "failed" status from Meta is recorded on that reply.
    status = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PHONE_ID},
        "statuses": [{"id": row["channel_message_id"], "status": "failed",
                      "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    deliver(store, wa, settings, status)
    with store.tx(t["id"]) as tx:
        row = tx._one("SELECT * FROM outbox WHERE id = %s", (out[0]["id"],))
    assert row["status"] == "failed" and "24 hours" in row["error"]


def test_outside_24h_window_is_not_sent(store, settings, clean):
    code, _, _ = owner_invite(store, settings)
    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(FRIEND, text=f"/start {code}"))
    deliver(store, wa, settings, event(FRIEND, text="Asia/Kolkata"))
    t = by_phone(store, FRIEND)
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET whatsapp_last_inbound = now() - interval '25 hours' WHERE id = %s",
                        (t["id"],))
        oid = tx.add_outbox(t["id"], chat_id=None, body="Reminder: call Jim", priority="alert")
    before = len(wa.sent)
    whatsapp.send_once(store, wa)
    assert len(wa.sent) == before
    with store.tx(t["id"]) as tx:
        row = tx._one("SELECT * FROM outbox WHERE id = %s", (oid,))
    assert row["status"] == "failed" and "24 hours" in row["error"] and row["chat_id"] == int(FRIEND)


def test_owner_links_whatsapp_to_the_telegram_account(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    tg.push("/link")
    receiver.poll_once(store, tg, settings, timeout=0)
    msg = tg.sent[-1][1]
    code = msg.split("text=/start%20")[1].split()[0]
    with store.tx(system=True) as tx:
        logged = tx._one("SELECT body FROM outbox WHERE body LIKE 'To use WhatsApp%%' ORDER BY created_at DESC")
    assert code not in logged["body"] and "[link hidden]" in logged["body"]

    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(OWNER_WA, text=f"/start {code}"), tg)
    t = by_phone(store, OWNER_WA)
    assert t is not None and t["telegram_user_id"] == OWNER        # one account, two channels
    assert wa.texts(OWNER_WA)[-1].startswith("Connected.")

    # The code works once.
    deliver(store, wa, settings, event(STRANGER, text=f"/start {code}"))
    assert by_phone(store, STRANGER) is None

    # Owner powers carry over, and unprompted messages follow the last channel used.
    deliver(store, wa, settings, event(OWNER_WA, text="/invite"), tg)
    assert "Invite code (one use)" in wa.texts(OWNER_WA)[-1]
    assert "https://t.me/atya_test_bot?start=" in wa.texts(OWNER_WA)[-1]
    with store.tx(system=True) as tx:
        oid = tx.add_outbox(t["id"], chat_id=None, body="brief")
        assert tx._one("SELECT channel, chat_id FROM outbox WHERE id = %s", (oid,)) == \
            {"channel": "whatsapp", "chat_id": int(OWNER_WA)}
    tg.push("hello from telegram")
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
        oid = tx.add_outbox(t["id"], chat_id=None, body="brief")
        assert tx._one("SELECT channel, chat_id FROM outbox WHERE id = %s", (oid,)) == \
            {"channel": "telegram", "chat_id": 555}


def test_expired_link_code(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    tg.push("/link")
    receiver.poll_once(store, tg, settings, timeout=0)
    code = tg.sent[-1][1].split("text=/start%20")[1].split()[0]
    with store.tx(system=True) as tx:
        st = tx.get_state(f"link:{code}")
        st["expires"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
        tx.set_state(f"link:{code}", st)
    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(OWNER_WA, text=f"/start {code}"))
    assert by_phone(store, OWNER_WA) is None
    assert "expired" in wa.texts(OWNER_WA)[-1]


def test_pdf_media_and_export_on_whatsapp(store, settings, clean):
    code, _, _ = owner_invite(store, settings)
    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(FRIEND, text=f"/start {code}"))
    deliver(store, wa, settings, event(FRIEND, text="Asia/Kolkata"))
    t = by_phone(store, FRIEND)

    wa.files["media1"] = text_pdf(["Lease agreement between A and B. Rent 50,000 a month."])
    deliver(store, wa, settings, event(FRIEND, doc={"id": "media1", "filename": "lease.pdf",
                                                    "mime_type": "application/pdf"}))
    with store.tx(t["id"]) as tx:
        row = tx._one("SELECT i.raw_content, a.filename FROM inbox i JOIN attachments a ON a.inbox_id = i.id")
    assert row == {"raw_content": "[PDF] lease.pdf", "filename": "lease.pdf"}

    deliver(store, wa, settings, event(FRIEND, doc={"id": "m2", "filename": "x.docx",
                                                    "mime_type": "application/msword"}))
    assert "only save PDF" in wa.texts(FRIEND)[-1]
    photo = event(FRIEND)
    photo["entry"][0]["changes"][0]["value"]["messages"][0].update(type="image", image={"id": "i"})
    deliver(store, wa, settings, photo)
    assert "Photos and voice notes" in wa.texts(FRIEND)[-1]

    deliver(store, wa, settings, event(FRIEND, text="/export"))
    assert "/web" in wa.texts(FRIEND)[-1]
    deliver(store, wa, settings, event(FRIEND, text="/web"))
    assert "https://atya.test/login/" in wa.texts(FRIEND)[-1]


def test_duplicate_webhook_delivery_is_stored_once(store, settings, clean):
    code, _, _ = owner_invite(store, settings)
    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(FRIEND, text=f"/start {code}"))
    deliver(store, wa, settings, event(FRIEND, text="Asia/Kolkata"))
    t = by_phone(store, FRIEND)
    ev = event(FRIEND, text="buy milk")
    deliver(store, wa, settings, ev)
    deliver(store, wa, settings, ev)
    with store.tx(t["id"]) as tx:
        assert tx._one("SELECT count(*) AS n FROM inbox WHERE raw_content = 'buy milk'")["n"] == 1


def test_failed_event_is_kept_and_retried(store, settings, clean, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("db down")
    monkeypatch.setattr(whatsapp, "process_event", boom)
    wa = FakeWhatsApp()
    deliver(store, wa, settings, event(STRANGER, text="hi"))
    with store.tx(system=True) as tx:
        ev = tx._one("SELECT * FROM whatsapp_events")
    assert ev["attempts"] == 1 and "db down" in ev["error"]
    for _ in range(6):
        whatsapp.process_once(store, wa, settings)
    assert len(calls) == 5                        # gives up after 5 attempts; the row stays


def test_privacy_page_is_public(store, settings):
    client = create_app(store, settings).test_client()
    r = client.get("/privacy")
    assert r.status_code == 200 and b"Privacy policy" in r.data and b"Anthropic" in r.data
    assert b"style=" not in r.data
    from dataclasses import replace
    r = create_app(store, replace(settings, llm_provider="bedrock")).test_client().get("/privacy")
    assert b"Amazon Bedrock" in r.data and b"Anthropic" not in r.data
