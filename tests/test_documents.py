"""PDFs: receiving, reading, filing, searching, answering, downloading, exporting."""

from __future__ import annotations

import io
import zipfile

import pytest

from atya import documents, processor
from tests.pdfs import scanned_pdf, text_pdf
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant, run_all

CONTRACT = text_pdf([
    "Services agreement between VVPM Logistics and Ankur Goel\nEffective 1 November 2026\n"
    "Scope: shipment planning for the Philippines route",
    "Payment terms: 1,240 USD due by 30 November 2026\nLate fee 2 percent per month\n"
    "Termination: either party with 30 days written notice",
])


@pytest.fixture
def settings(db_url):
    from atya.config import Settings
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2)


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(OWNER)
        if t:
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
    yield


def test_check_and_extract():
    assert documents.is_pdf(CONTRACT)
    with pytest.raises(documents.DocumentError, match="Only PDF"):
        documents.check(b"hello")
    with pytest.raises(documents.DocumentError, match="20 MB"):
        documents.check(b"%PDF-" + b"0" * (documents.MAX_BYTES + 1))
    pages = documents.extract(CONTRACT)
    assert len(pages) == 2 and "1,240 USD" in pages[1] and not documents.looks_scanned(pages)
    assert documents.looks_scanned(documents.extract(scanned_pdf(2)))
    with pytest.raises(documents.DocumentError, match="damaged"):
        documents.extract(b"%PDF-1.4 garbage")
    chunks = documents.chunk(["a" * 4000])
    assert len(chunks) == 3 and all(len(c) <= documents.CHUNK_CHARS for _, c in chunks)


def test_pdf_on_telegram_is_read_filed_and_answered(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("hi")                                         # creates the owner account
    llm.intent["hi"] = "chat"
    run_all(store, tg, llm, settings)
    caption = "contract from VVPM, check the payment terms"
    planned = {}

    def plan_fanout(*, schema, raw_content, captured_local, context, hint=None):
        planned["raw"], planned["hint"] = raw_content, hint
        return {"writes": [
            {"type": "document", "title": "VVPM services agreement",
             "fields": {"Kind": "contract", "From": "VVPM", "Summary": "Agreement for Philippines shipments."}},
            {"type": "task", "title": "Pay VVPM 1,240 USD", "fields": {"Due Date": "2026-11-30"}},
        ]}
    llm.plan_fanout = plan_fanout
    tg.push_document(CONTRACT, name="vvpm.pdf", caption=caption)
    run_all(store, tg, llm, settings)

    t = owner_tenant(store)
    assert llm.read_calls[-1]["text"] and "1,240 USD" in llm.read_calls[-1]["text"] and not llm.read_calls[-1]["pdf"]
    assert planned["raw"].startswith(caption) and "[Attached file: vvpm.pdf, 2 pages]" in planned["raw"]
    assert "PDF" in planned["hint"]
    with store.tx(system=True) as tx:
        inbox = tx._one("SELECT * FROM inbox WHERE tenant_id = %s AND raw_content = %s", (t["id"], caption))
        att = tx._one("SELECT * FROM attachments WHERE inbox_id = %s", (inbox["id"],))
        doc = tx._one("SELECT * FROM records WHERE tenant_id = %s AND type = 'document'", (t["id"],))
        task = tx._one("SELECT * FROM records WHERE tenant_id = %s AND type = 'task'", (t["id"],))
        n_chunks = tx._one("SELECT count(*) AS n FROM attachment_chunks WHERE attachment_id = %s", (att["id"],))["n"]
    assert inbox["raw_content"] == caption and inbox["status"] == "processed" and inbox["intent"] == "log"
    assert bytes(att["data"]) == CONTRACT and att["pages"] == 2 and att["extraction"] == "text"
    assert att["record_id"] == doc["id"] and n_chunks == 2
    assert task["fields"]["Due Date"] == "2026-11-30"
    reply = tg.sent[-1][1]
    assert reply.startswith("Saved DOC-1 “VVPM services agreement” (2 pages).")
    assert "TSK-1 Pay VVPM 1,240 USD" in reply

    # A question finds the passage inside the PDF.
    q = "what are the late fees in the VVPM contract?"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["document"], "search": "late fee VVPM"}
    tg.push(q)
    run_all(store, tg, llm, settings)
    excerpts = [r for r in llm.seen_records if r.get("document_excerpt")]
    assert excerpts and "Late fee 2 percent" in excerpts[0]["text"] and excerpts[0]["page"] == 2
    assert excerpts[0]["document"] == "[DOC-1] VVPM services agreement"


def test_pdf_without_caption_and_planner_skipping_document(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    llm.plan_fanout = lambda **kw: {"writes": []}
    tg.push_document(CONTRACT, name="scan-0042.pdf")
    run_all(store, tg, llm, settings)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        inbox = tx._one("SELECT * FROM inbox WHERE tenant_id = %s", (t["id"],))
        doc = tx._one("SELECT * FROM records WHERE tenant_id = %s AND type = 'document'", (t["id"],))
    assert inbox["raw_content"] == "[PDF] scan-0042.pdf"
    assert llm.read_calls[-1]["caption"] == ""
    assert doc["title"] == "Services agreement" and doc["fields"]["Kind"] == "contract"   # fallback record


def test_scanned_pdf_read_by_model_and_too_long_scan_kept(store, settings, clean, monkeypatch):
    tg, llm = FakeTelegram(), FakeLLM()
    llm.plan_fanout = lambda **kw: {"writes": [{"type": "document", "title": "Old receipt"}]}
    llm.reading = {"title": "Old receipt", "kind": "receipt", "date": "", "from": "", "summary": "A receipt.",
                   "key_points": [], "actions": [], "text": "[page 1]\nTotal paid 99 EUR to Hotel Lisboa"}
    tg.push_document(scanned_pdf(2), name="receipt.pdf", caption="hotel receipt")
    run_all(store, tg, llm, settings)
    assert llm.read_calls[-1]["pdf"] is True and llm.read_calls[-1]["text"] is None
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        att = tx._one("SELECT * FROM attachments WHERE tenant_id = %s", (t["id"],))
        hits = tx.search_chunks(t["id"], "Lisboa")
    assert att["extraction"] == "vision" and "Hotel Lisboa" in att["text"] and hits

    monkeypatch.setattr(documents, "MAX_SCAN_PAGES", 2)
    calls = len(llm.read_calls)
    tg.push_document(scanned_pdf(3), name="big-scan.pdf", caption="big scan")
    run_all(store, tg, llm, settings)
    assert len(llm.read_calls) == calls                   # not sent to the model
    assert "scan of 3 pages" in tg.sent[-1][1] and "kept the file" in tg.sent[-1][1]


def test_non_pdf_and_bad_pdf_rejected_politely(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("hi")
    llm.intent["hi"] = "chat"
    tg.push_document(b"just text", name="notes.txt", mime="text/plain")
    tg.push_document(b"%PDF-1.4 broken", name="broken.pdf")
    run_all(store, tg, llm, settings)
    texts = [m[1] for m in tg.sent]
    assert any("only save PDF" in m for m in texts)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        att = tx._one("SELECT * FROM attachments WHERE tenant_id = %s", (t["id"],))
    assert att["filename"] == "broken.pdf" and att["extraction"] == "none"
    assert any("damaged" in m for m in texts)
