"""Voice notes: receiving on both channels, transcribing, filing, replies, web view, search, export."""

from __future__ import annotations

import io
import time
import zipfile

import pytest

from atya import export, processor, receiver, sender, transcribe, web, whatsapp
from atya.config import Settings
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant
from tests.test_web import sign_in
from tests.test_whatsapp import FakeWhatsApp, PHONE_ID

OGG = b"OggS\x00\x02" + b"\x00" * 200


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    web_base_url="https://atya.test", whatsapp_token="t", whatsapp_app_secret="s",
                    whatsapp_phone_number_id=PHONE_ID, whatsapp_verify_token="v", whatsapp_number="91")


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(OWNER)
        if t:
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
        tx.conn.execute("DELETE FROM whatsapp_events")
    yield


class FakeTranscriber:
    def __init__(self, text="Remind me to call Jim at 5 pm", fail=None):
        self.text, self.fail, self.calls = text, fail, []

    def transcribe(self, data, mime_type):
        self.calls.append((len(data), mime_type))
        if self.fail:
            raise self.fail
        return {"text": self.text, "language": "en-IN"}


def push_voice(tg, data=OGG, seconds=42, mime="audio/ogg"):
    tg._next += 1
    fid = f"voice{tg._next}"
    tg.files[fid] = data
    tg.queue.append({"update_id": tg._next, "message": {
        "message_id": tg._next, "date": int(time.time()), "from": {"id": OWNER, "first_name": "Ankur"},
        "chat": {"id": 555}, "voice": {"file_id": fid, "duration": seconds, "mime_type": mime,
                                       "file_size": len(data)}}})


def mine(tg):
    """Texts sent to the owner's chat (other tests may leave other users' rows behind)."""
    return [m[1] for m in tg.sent if m[0] == 555]


def start_owner(store, settings, tg):
    tg.push("/start")
    receiver.poll_once(store, tg, settings, timeout=0)


def run(store, tg, llm, settings, tr):
    receiver.poll_once(store, tg, settings, timeout=0)
    while processor.process_once(store, llm, settings, None, tr):
        pass
    sender.send_once(store, tg)


def test_voice_note_is_kept_transcribed_and_filed(store, settings, clean):
    tg, llm, tr = FakeTelegram(), FakeLLM(), FakeTranscriber()
    start_owner(store, settings, tg)
    llm.plans["Remind me to call Jim at 5 pm"] = {"writes": [
        {"type": "task", "title": "Call Jim", "fields": {"Status": "next"}}]}
    push_voice(tg)
    run(store, tg, llm, settings, tr)

    assert tr.calls == [(len(OGG), "audio/ogg")]
    t = owner_tenant(store)
    with store.tx(t["id"]) as tx:
        inbox = tx._one("SELECT * FROM inbox WHERE raw_content LIKE '[Voice note%%'")
        att = tx.attachments_for([inbox["id"]])[0]
        task = tx._one("SELECT * FROM records WHERE type = 'task'")
    assert inbox["raw_content"] == "[Voice note 0:42]" and inbox["status"] == "processed"
    assert att["transcript"] == "Remind me to call Jim at 5 pm" and att["seconds"] == 42
    assert att["mime_type"] == "audio/ogg" and att["filename"] == "voice.ogg"
    assert task["title"] == "Call Jim" and task["source_inbox_id"] == inbox["id"]
    reply = mine(tg)[-1]
    assert reply.startswith("Heard: “Remind me to call Jim at 5 pm”") and "TSK-1 Call Jim" in reply

    # A retry uses the saved transcript instead of transcribing again.
    with store.tx(t["id"]) as tx:
        tx.conn.execute("UPDATE inbox SET status = 'pending' WHERE id = %s", (inbox["id"],))
    llm.plans["Remind me to call Jim at 5 pm"] = {"writes": []}
    while processor.process_once(store, llm, settings, None, tr):
        pass
    assert len(tr.calls) == 1


def test_voice_question_answer_starts_with_heard(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    tr = FakeTranscriber("What do I owe Rahul?")
    start_owner(store, settings, tg)
    llm.intent["What do I owe Rahul?"] = "query"
    llm.query_plans["What do I owe Rahul?"] = {"types": ["task"], "search": "Rahul"}
    push_voice(tg)
    run(store, tg, llm, settings, tr)
    assert mine(tg)[-1].startswith("Heard: “What do I owe Rahul?”\n\nAnswer:")


@pytest.mark.parametrize("tr,expect", [
    (None, "voice notes aren't switched on"),
    (FakeTranscriber(""), "couldn't make out any words"),
    (FakeTranscriber(fail=transcribe.TranscribeError("bad audio")), "couldn't turn it into text"),
])
def test_voice_note_problems_are_explained_and_audio_kept(store, settings, clean, tr, expect):
    tg, llm = FakeTelegram(), FakeLLM()
    start_owner(store, settings, tg)
    push_voice(tg)
    run(store, tg, llm, settings, tr)
    assert expect in mine(tg)[-1]
    with store.tx(owner_tenant(store)["id"]) as tx:
        assert tx._one("SELECT count(*) AS n FROM attachments WHERE mime_type = 'audio/ogg'")["n"] == 1


def test_unsupported_and_too_long_audio(store, settings, clean):
    tg = FakeTelegram()
    start_owner(store, settings, tg)
    push_voice(tg, mime="audio/aac")
    push_voice(tg, seconds=transcribe.MAX_SECONDS + 1)
    receiver.poll_once(store, tg, settings, timeout=0)
    assert "can't read that audio format" in mine(tg)[-2]
    assert "up to 15 minutes" in mine(tg)[-1]


def test_whatsapp_voice_note(store, settings, clean):
    tg, llm, tr = FakeTelegram(), FakeLLM(), FakeTranscriber("buy milk")
    start_owner(store, settings, tg)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        tx.link_whatsapp(t["id"], "919800000002")
    wa = FakeWhatsApp()
    wa.files["m1"] = OGG
    payload = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PHONE_ID},
        "messages": [{"from": "919800000002", "id": "wamid.v1", "timestamp": str(int(time.time())),
                      "type": "audio", "audio": {"id": "m1", "mime_type": "audio/ogg; codecs=opus", "voice": True}}]}}]}]}
    assert whatsapp.process_event(store, wa, settings, payload) == ["stored"]
    llm.plans["buy milk"] = {"writes": [{"type": "task", "title": "Buy milk", "fields": {"Status": "next"}}]}
    while processor.process_once(store, llm, settings, None, tr):
        pass
    whatsapp.send_once(store, wa)
    assert tr.calls == [(len(OGG), "audio/ogg")]
    assert wa.sent[-1][1].startswith("Heard: “buy milk”")


def test_web_shows_player_and_transcript_and_search_finds_it(store, settings, clean):
    tg, llm, tr = FakeTelegram(), FakeLLM(), FakeTranscriber("Pick up the sofa from Rohan's place")
    start_owner(store, settings, tg)
    llm.plans["Pick up the sofa from Rohan's place"] = {"writes": []}
    push_voice(tg)
    run(store, tg, llm, settings, tr)
    t = owner_tenant(store)
    app = web.create_app(store, settings)
    c, _ = sign_in(app, store, settings, t)
    page = c.get("/messages").get_data(as_text=True)
    assert "<audio controls" in page and "Pick up the sofa from Rohan&#39;s place" in page
    assert "style=" not in page
    assert "sofa" in c.get("/messages?q=sofa").get_data(as_text=True)
    with store.tx(t["id"]) as tx:
        aid = tx._one("SELECT id FROM attachments")["id"]
    r = c.get(f"/files/{aid}?play=1", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.data == OGG[:10] and r.mimetype == "audio/ogg"
    assert "media-src 'self'" in r.headers["Content-Security-Policy"] or \
        "media-src 'self'" in c.get("/messages").headers["Content-Security-Policy"]

    from atya.schema import load_schema
    with store.tx(t["id"]) as tx:
        _, data = export.build_export(tx, load_schema("default"), t)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert any(n.endswith("voice.ogg") for n in names)
    txt = next(n for n in names if n.endswith("voice.ogg.txt"))
    assert "sofa" in zf.read(txt).decode()


class FakeS3:
    def __init__(self):
        self.objects, self.deleted = {}, []

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)


class FakeTranscribeAPI:
    def __init__(self, statuses):
        self.statuses, self.started, self.deleted = list(statuses), None, []

    def start_transcription_job(self, **kw):
        self.started = kw

    def get_transcription_job(self, TranscriptionJobName):
        st = self.statuses.pop(0)
        return {"TranscriptionJob": {"TranscriptionJobStatus": st, "LanguageCode": "hi-IN",
                                     "FailureReason": "corrupt", "Transcript": {"TranscriptFileUri": "https://x/t.json"}}}

    def delete_transcription_job(self, TranscriptionJobName):
        self.deleted.append(TranscriptionJobName)


def test_aws_transcriber_cleans_up(monkeypatch):
    monkeypatch.setattr(transcribe, "POLL_EVERY", 0)

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": {"transcripts": [{"transcript": "नमस्ते, call Jim"}]}}
    monkeypatch.setattr(transcribe.requests, "get", lambda *a, **k: R())
    s3, api = FakeS3(), FakeTranscribeAPI(["IN_PROGRESS", "COMPLETED"])
    out = transcribe.AWSTranscriber(bucket="b", s3=s3, client=api).transcribe(OGG, "audio/ogg; codecs=opus")
    assert out == {"text": "नमस्ते, call Jim", "language": "hi-IN", "seconds": None}
    assert api.started["MediaFormat"] == "ogg" and api.started["LanguageOptions"] == ["en-IN", "hi-IN"]
    key = next(iter(s3.objects))
    assert key.startswith("transcribe-tmp/") and s3.deleted == [key] and len(api.deleted) == 1

    s3, api = FakeS3(), FakeTranscribeAPI(["FAILED"])
    with pytest.raises(transcribe.TranscribeError, match="corrupt"):
        transcribe.AWSTranscriber(bucket="b", s3=s3, client=api).transcribe(OGG, "audio/ogg")
    assert s3.deleted and api.deleted


def test_helpers():
    assert transcribe.placeholder(42) == "[Voice note 0:42]"
    assert transcribe.placeholder(None) == "[Voice note]"
    assert transcribe.supported("audio/mp4") and not transcribe.supported("audio/aac")
    assert processor.with_heard("x" * 900, "ok").endswith("…”\n\nok")
