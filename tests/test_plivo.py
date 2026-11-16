"""Real phone calls (Plivo): which line a user gets, dialling, the callbacks, the outcome,
and the audio bridge between Plivo's stream and the voice model (both faked here)."""
import array
import asyncio
import base64
import json

import pytest

from atya import callbridge, plivo, wakecall, web
from atya.config import Settings
from tests.test_wakecall import OWNER, friend, outbox, owner  # noqa: F401  (fixture)

PHONE = "919711966131"


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    plivo_auth_id="MAXXXXXXXXXXXXXXXXXX", plivo_auth_token="tok", plivo_number="918031772892",
                    web_base_url="https://atya.example.com")


def with_phone(store, tenant, phone=PHONE):
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET whatsapp_phone = %s WHERE id = %s", (phone, tenant["id"]))
        return tx.tenant(tenant["id"])


def test_line_for_each_user(settings):
    tg = Settings(**{**settings.__dict__, "plivo_auth_id": None, "tg_caller_api_id": "1",
                     "tg_caller_api_hash": "h", "tg_caller_session": "s"})
    assert plivo.line_for(settings, {"whatsapp_phone": PHONE}) == "phone"
    assert plivo.line_for(settings, {"whatsapp_phone": "14155550100", "telegram_user_id": 5}) is None
    assert plivo.line_for(tg, {"whatsapp_phone": PHONE, "telegram_user_id": 5}) == "telegram"
    assert plivo.line_for(tg, {"whatsapp_phone": PHONE}) is None
    assert plivo.display_number(settings) == "+91 80 3177 2892"
    assert settings.calls_on and not Settings(**{**settings.__dict__, "web_base_url": None}).plivo_on


def test_answer_xml_and_hangup_causes(settings):
    xml = plivo.answer_xml(settings, "s3cret")
    assert 'bidirectional="true"' in xml and 'keepCallAlive="true"' in xml
    assert "audio/x-l16;rate=16000" in xml and "wss://atya.example.com/plivo/ws/s3cret</Stream>" in xml
    assert "<Hangup/>" in plivo.answer_xml(settings, None)
    assert plivo.result_from_hangup("busy|USER_BUSY|Busy Line|17|Callee") == "declined"
    assert plivo.result_from_hangup("completed|CALL_REJECTED|Rejected||") == "declined"
    assert plivo.result_from_hangup("no-answer|NO_ANSWER|No Answer||") == "missed"
    assert plivo.result_from_hangup("timeout|||") == "missed"
    assert plivo.result_from_hangup("failed|violates_media_anchoring|||") == "failed"
    assert plivo.result_from_hangup("failed|UNALLOCATED_NUMBER|||") == "failed"


class FakeHTTP:
    def __init__(self, status=201):
        self.status = status
        self.posts = []
        self.deletes = []

    def post(self, url, auth=None, json=None, timeout=None):
        self.posts.append((url, json))
        return type("R", (), {"status_code": self.status, "content": b"x", "text": "nope",
                              "json": lambda s: {"request_uuid": "req-1", "message": "call fired"}})()

    def delete(self, url, auth=None, timeout=None):
        self.deletes.append(url)


def claim(store, tenant, kind="now"):
    import datetime as dt
    with store.tx(tenant["id"]) as tx:
        tx.add_call(tenant["id"], kind, dt.datetime.now(dt.timezone.utc))
    with store.tx(system=True) as tx:
        return tx.claim_due_call(dt.datetime.now(dt.timezone.utc))


async def place_with(store, settings, tenant, call, http, then):
    """Place the call while `then(secret)` plays Plivo's part (callbacks / bridge) in the background."""
    async def plivo_side():
        while not http.posts:
            await asyncio.sleep(0.01)
        secret = http.posts[0][1]["answer_url"].rsplit("/", 1)[-1]
        then(secret)
    side = asyncio.create_task(plivo_side())
    result = await plivo.place_call(store, settings, tenant, call, budget=600, http=http, poll=0.02)
    await side
    return result


def test_unanswered_phone_call_reports_the_hangup(store, settings, owner):
    t = with_phone(store, owner)
    call = claim(store, t)
    http = FakeHTTP()
    app = web.create_app(store, settings)

    def hangup(secret):
        assert app.test_client().post(f"/plivo/hangup/{secret}", data={
            "CallStatus": "no-answer", "HangupCause": "NO_ANSWER", "CallUUID": "c-1"}).status_code == 200

    assert asyncio.run(place_with(store, settings, t, call, http, hangup)) == "missed"
    url, body = http.posts[0]
    assert url.endswith("/Account/MAXXXXXXXXXXXXXXXXXX/Call/")
    assert body["from"] == "918031772892" and body["to"] == PHONE and body["ring_timeout"] == plivo.RING_SECONDS
    assert body["hangup_url"].startswith("https://atya.example.com/plivo/hangup/")
    with store.tx(system=True) as tx:
        row = tx.call(call["id"])
    assert row["line"] == "phone" and row["provider_uuid"] == "c-1" and "NO_ANSWER" in row["hangup_cause"]


def test_answered_phone_call_waits_for_the_bridge(store, settings, owner):
    t = with_phone(store, owner)
    call = claim(store, t)
    app = web.create_app(store, settings)

    def answer(secret):
        r = app.test_client().post(f"/plivo/answer/{secret}")
        assert r.mimetype == "application/xml" and f"/plivo/ws/{secret}" in r.get_data(as_text=True)
        with store.tx(system=True) as tx:
            c = tx.call_by_secret(secret)
            assert tx.mark_call_answered(c["id"]) and not tx.mark_call_answered(c["id"])
            tx.set_call_outcome(c["id"], "answered")

    assert asyncio.run(place_with(store, settings, t, call, FakeHTTP(), answer)) == "answered"


def test_refused_dial_and_unknown_secrets(store, settings, owner):
    t = with_phone(store, owner)
    call = claim(store, t)
    assert asyncio.run(plivo.place_call(store, settings, t, call, budget=600, http=FakeHTTP(400))) == "failed"
    c = web.create_app(store, settings).test_client()
    assert "<Hangup/>" in c.post("/plivo/answer/" + "x" * 32).get_data(as_text=True)
    assert c.post("/plivo/answer/short").status_code == 404
    off = web.create_app(store, Settings(**{**settings.__dict__, "plivo_auth_id": None})).test_client()
    assert off.post("/plivo/answer/" + "x" * 32).status_code == 404


def test_worker_uses_the_phone_and_falls_back_to_messages(store, settings, owner):
    import datetime as dt
    t = with_phone(store, owner)
    seen = []

    async def fake_phone(store_, settings_, tenant, call, *, budget, http=None, poll=1.0):
        seen.append((tenant["whatsapp_phone"], call["kind"], budget))
        return "declined"
    orig = plivo.place_call
    plivo.place_call = fake_phone
    try:
        with store.tx(t["id"]) as tx:
            tx.add_call(t["id"], "now", dt.datetime.now(dt.timezone.utc))
        assert wakecall.tick(store, settings, wakecall.dialer(store, settings)) == "now:declined:declined"
    finally:
        plivo.place_call = orig
    assert seen and seen[0][0] == PHONE
    # A reminder call for someone we can't call (a non-Indian number, no Telegram) becomes a message.
    t = with_phone(store, owner, "14155550100")
    with store.tx(t["id"]) as tx:
        tx.add_call(t["id"], "reminder", dt.datetime.now(dt.timezone.utc), about="leave for the airport")
    assert wakecall.tick(store, settings, wakecall.dialer(store, settings)) == "reminder:no-line:cancelled"
    assert any("leave for the airport" in b and "couldn't call you" in b for b in outbox(store, t["id"]))


def test_resampler_keeps_length_across_chunks():
    d = callbridge.Down24to16()
    samples = array.array("h", range(0, 3000, 10)).tobytes()       # 300 samples at 24 kHz
    out = b"".join(d.feed(samples[i:i + 77]) for i in range(0, len(samples), 77))
    assert len(out) == 200 * 2                                     # -> 200 samples at 16 kHz
    first = array.array("h")
    first.frombytes(out[:8])
    assert list(first) == [0, 15, 30, 45]


class FakeWS:
    def __init__(self, events):
        self.events = [json.dumps(e) for e in events]
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.05)
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def close(self):
        self.closed = True


class FakeSonic:
    def __init__(self, *, prompt, voice, region, tools, on_audio, on_interrupt):
        self.prompt, self.on_audio, self.on_interrupt = prompt, on_audio, on_interrupt
        self.heard = bytearray()
        self.transcript = []
        self.tool_calls = []
        self.done = asyncio.Event()

    async def open(self):
        pass

    async def say_first(self, text):
        self.on_audio(bytes(4800))                 # 100 ms of speech at 24 kHz
        self.transcript.append(("ASSISTANT", "Good morning! How are you?"))

    async def send_audio(self, pcm):
        self.heard.extend(pcm)
        if len(self.heard) >= 2048 and not self.transcript[1:]:
            self.transcript.append(("USER", "Fine, thanks. Remind me to call Ma at 6."))
            self.on_interrupt()
            self.on_audio(bytes(4800))

    async def close(self):
        self.done.set()


def test_bridge_streams_both_ways_and_saves_the_call(store, settings, owner, monkeypatch):
    t = with_phone(store, owner)
    call = claim(store, t)
    with store.tx(system=True) as tx:
        tx.start_phone_attempt(call["id"], "s" * 32)
    monkeypatch.setattr(wakecall, "prepare", lambda *a: asyncio.sleep(0, result=("prompt", [])))
    pcm = base64.b64encode(bytes(1280)).decode()     # 40 ms at 16 kHz
    ws = FakeWS([{"event": "start", "start": {"streamId": "st-1", "callId": "c-9"}},
                 *[{"event": "media", "media": {"track": "inbound", "payload": pcm}} for _ in range(3)],
                 {"event": "stop"}])
    bridge = callbridge.Bridge(store, settings, llm=None, embedder=None, sonic_factory=FakeSonic)
    asyncio.run(bridge.handle(ws, "/plivo/ws/" + "s" * 32))
    plays = [m for m in ws.sent if m["event"] == "playAudio"]
    assert plays and all(m["media"]["sampleRate"] == 16000 for m in plays)
    assert len(base64.b64decode(plays[0]["media"]["payload"])) == 3200      # 100 ms at 16 kHz
    assert {"event": "clearAudio", "streamId": "st-1"} in ws.sent
    with store.tx(system=True) as tx:
        row = tx.call(call["id"])
    assert row["outcome"] == "answered" and row["answered_at"] and row["provider_uuid"] == "c-9"
    with store.tx(t["id"]) as tx:
        saved = tx._all("SELECT raw_content AS body FROM inbox WHERE tenant_id = %s AND source = 'call'", (t["id"],))
    assert saved and "call Ma at 6" in saved[-1]["body"]
    # A second stream for the same attempt is refused.
    again = FakeWS([])
    asyncio.run(bridge.handle(again, "/plivo/ws/" + "s" * 32))
    assert again.closed and not again.sent
