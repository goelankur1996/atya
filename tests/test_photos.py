"""Photos and screenshots: read once (what it shows + the text in it), then handled like a typed message."""
import io

from atya import images, processor, receiver
from tests.test_pipeline import FakeLLM, FakeTelegram, clean, owner_tenant, settings  # noqa: F401  (fixtures)


def png(size=(40, 20), color=(200, 30, 30)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


class SeeingLLM(FakeLLM):
    def read_image(self, *, image, fmt, caption):
        self.seen = (len(image), fmt, caption)
        return {"description": "A LinkedIn post: BOLD is hiring Senior Data Scientists in Noida (hybrid).",
                "text": "BOLD is hiring Data Scientists!\nShare your resume at: abhay.singh@bold.com"}


def test_photo_becomes_a_task_with_its_text(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    receiver.poll_once(store, tg, settings, timeout=0)
    t = owner_tenant(store)
    caption = "Need to apply to this position by tonight"
    with store.tx(t["id"]) as tx:
        inbox_id = tx.add_inbox(t["id"], source="telegram", raw_content=caption)
        att = tx.add_attachment(t["id"], inbox_id=inbox_id, filename="photo.jpg", mime_type="image/png",
                                data=png(), sha256="x")
    llm = SeeingLLM()
    message = images.as_message(caption, SeeingLLM().read_image(image=b"", fmt="png", caption=caption))
    assert message.startswith(caption + "\n\n[Photo] A LinkedIn post") and "abhay.singh@bold.com" in message
    llm.plans[message] = {"writes": [{"type": "task", "title": "Apply to BOLD Senior Data Scientist role",
                                      "fields": {"Status": "next"}}]}
    while processor.process_once(store, llm, settings):
        pass
    assert llm.seen[1:] == ("png", caption)
    with store.tx(t["id"]) as tx:
        (reply,) = tx._all("SELECT body FROM outbox WHERE inbox_id = %s", (inbox_id,))
        a = tx._one("SELECT extraction, text, record_id FROM attachments WHERE id = %s", (att,))
        (task,) = tx._all("SELECT id, title FROM records WHERE source_inbox_id = %s", (inbox_id,))
    assert reply["body"].startswith("\U0001F4F7 A LinkedIn post: BOLD is hiring") and "Saved:" in reply["body"]
    assert a["extraction"] == "vision" and "abhay.singh@bold.com" in a["text"] and a["record_id"] == task["id"]


def test_big_photos_are_shrunk_for_the_model():
    from PIL import Image
    raw = io.BytesIO()
    Image.effect_noise((4200, 3000), 90).convert("RGB").save(raw, format="PNG")
    data, fmt = images.for_model(raw.getvalue(), "image/png")
    assert fmt == "jpeg" and len(data) <= images.MODEL_BYTES
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) <= images.MAX_SIDE
    small = png()
    assert images.for_model(small, "image/png") == (small, "png")
    assert images.is_image("image/jpeg") and images.is_image("", "shot.PNG") and not images.is_image("application/pdf")
