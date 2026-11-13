"""Try other models on real past messages, without changing anything.

Replays the owner's processed messages through a candidate model pair and
compares with what the current models did (the stored outcome):

- intent: does the candidate classify log / query / chat the same way?
- filing (log messages): does its plan create the same kinds of records with
  similar titles? The records the message originally made are hidden from the
  context, so the candidate sees roughly what the original model saw.
- answers (questions): the candidate's answer next to the reply that was sent.
- files: the candidate's reading of stored PDFs next to the saved document.

Nothing is written except the report (service_state 'eval:<id>'), which the
owner reads at /eval on the web view.

    python -m atya.eval --list                       # Bedrock models this account can call
    python -m atya.eval --ping MODEL [MODEL ...]     # one tiny call each
    python -m atya.eval --fast FAST --main MAIN [--limit 150] [--workers 6]
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import logging
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from .config import load_settings
from .llm import BedrockLLM, LLMError
from .processor import (_tz, broaden_if_thin, document_excerpts, fanout_context, gather_records,
                        local_label, record_ref)
from .schema import load_schema
from .store import Store

log = logging.getLogger("eval")
TITLE_MATCH = 0.6          # difflib ratio that counts as "same record"
MAX_FILES = 10
KEEP_REPORTS = 10


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def match_writes(original: list[dict], candidate: list[dict]) -> dict:
    """Pair records by type and closest title. Returns counts and the pairs."""
    left = list(candidate)
    pairs, missed = [], []
    for o in original:
        best, score = None, 0.0
        for c in left:
            if c["type"] != o["type"]:
                continue
            r = similar(o["title"], c["title"])
            if r > score:
                best, score = c, r
        if best is not None and score >= TITLE_MATCH:
            pairs.append({"type": o["type"], "original": o["title"], "candidate": best["title"],
                          "score": round(score, 2)})
            left.remove(best)
        else:
            missed.append(o)
    return {"matched": len(pairs), "missed": missed, "extra": left, "pairs": pairs,
            "same_types": sorted(o["type"] for o in original) == sorted(c["type"] for c in candidate)}


def pick_messages(store: Store, tenant_id: uuid.UUID, limit: int) -> list[dict]:
    with store.tx(tenant_id) as tx:
        return tx._all(
            "SELECT i.* FROM inbox i WHERE i.tenant_id = %s AND i.intent IN ('log', 'query', 'chat') "
            "AND i.status IN ('processed', 'responded') AND i.forced_intent IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.inbox_id = i.id) "
            "AND length(i.raw_content) > 0 ORDER BY i.captured_at DESC LIMIT %s", (tenant_id, limit))


def eval_message(store: Store, llm: BedrockLLM, tenant: dict, row: dict) -> dict:
    schema = load_schema(tenant["schema_name"])
    tz = _tz(tenant)
    item: dict = {"id": str(row["id"]), "text": row["raw_content"][:1500], "when": local_label(row["captured_at"], tz),
                  "original_intent": row["intent"], "notes": (row.get("processor_notes") or "")[:500]}
    t0 = time.monotonic()
    try:
        cls = llm.classify(row["raw_content"])
        item["intent"] = cls["intent"]
        if row["intent"] == "chat":
            item["reply"] = cls.get("reply", "")
        elif row["intent"] == "log":
            with store.tx(tenant["id"]) as tx:
                made = tx._all("SELECT id, type, title, seq FROM records WHERE tenant_id = %s "
                               "AND source_inbox_id = %s", (tenant["id"], row["id"]))
                hidden = {r["id"] for r in made}
                context = {k: [r for r in v if r["id"] not in hidden]
                           for k, v in fanout_context(tx, tenant["id"]).items()}
            plan = llm.plan_fanout(schema=schema, raw_content=row["raw_content"],
                                   captured_local=local_label(row["captured_at"], tz), context=context)
            cand = [{"type": str(w.get("type")), "title": str(w.get("title") or "")}
                    for w in plan.get("writes", []) if isinstance(w, dict)]
            orig = [{"type": r["type"], "title": r["title"]} for r in made]
            item["original_writes"] = orig
            item["writes"] = cand
            item["updates"] = len(plan.get("updates") or [])
            item["unknown_types"] = sorted({c["type"] for c in cand if c["type"] not in schema.types})
            item["match"] = match_writes(orig, cand)
            item["plan_notes"] = str(plan.get("processor_notes") or "")[:300]
        else:  # query
            now = local_label(dt.datetime.now(dt.timezone.utc), tz)
            plan = llm.plan_query(schema=schema, question=row["raw_content"], now=now)
            plan = broaden_if_thin(store, llm, schema, tenant["id"], row["raw_content"], plan)
            with store.tx(tenant["id"]) as tx:
                records = gather_records(tx, schema, tenant["id"], plan)
                records += document_excerpts(tx, schema, tenant["id"], plan, row["raw_content"])
                sent = tx.outbox_for([row["id"]])
            ans = llm.answer(schema=schema, question=row["raw_content"], records=records, now=now)
            item["search"] = plan.get("search")
            item["answer"] = ans["text"][:3000]
            item["found"] = ans["found"]
            item["original_answer"] = (sent[0]["body"] if sent else "")[:3000]
            item["original_found"] = "nothing relevant" not in (row.get("processor_notes") or "")
    except Exception as e:  # a model that can't follow the format is a result, not a crash
        item["error"] = f"{type(e).__name__}: {str(e)[:400]}"
        log.debug("item %s failed\n%s", row["id"], traceback.format_exc())
    item["secs"] = round(time.monotonic() - t0, 1)
    return item


def eval_files(store: Store, llm: BedrockLLM, tenant: dict) -> list[dict]:
    schema = load_schema(tenant["schema_name"])
    out = []
    with store.tx(tenant["id"]) as tx:
        files = tx._all("SELECT a.id, a.filename, a.text, a.extraction, a.record_id, i.raw_content "
                        "FROM attachments a JOIN inbox i ON i.id = a.inbox_id WHERE a.tenant_id = %s "
                        "AND a.extraction IN ('text', 'vision') ORDER BY a.created_at DESC LIMIT %s",
                        (tenant["id"], MAX_FILES))
    for f in files:
        item = {"filename": f["filename"], "how": f["extraction"]}
        with store.tx(tenant["id"]) as tx:
            rec = tx.record(tenant["id"], f["record_id"]) if f["record_id"] else None
            data = None
            if f["extraction"] == "vision":
                data = (tx.attachment(tenant["id"], f["id"], with_data=True) or {}).get("data")
        if rec:
            item["original"] = {"ref": record_ref(schema, rec), "title": rec["title"],
                                "kind": rec["fields"].get("Kind"), "summary": rec["fields"].get("Summary", "")[:800]}
        caption = "" if (f["raw_content"] or "").startswith("[PDF]") else f["raw_content"]
        t0 = time.monotonic()
        try:
            if f["extraction"] == "text":
                r = llm.read_document(filename=f["filename"], caption=caption, text=f["text"] or "")
            else:
                r = llm.read_document(filename=f["filename"], caption=caption, pdf=bytes(data or b""))
            item["candidate"] = {"title": r["title"], "kind": r["kind"], "summary": r["summary"][:800],
                                 "transcribed_chars": len(r.get("text") or "")}
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {str(e)[:400]}"
        item["secs"] = round(time.monotonic() - t0, 1)
        out.append(item)
    return out


def summarise(items: list[dict], files: list[dict], usage: dict) -> dict:
    ok = [i for i in items if "error" not in i]
    logs = [i for i in ok if i["original_intent"] == "log"]
    queries = [i for i in ok if i["original_intent"] == "query"]
    orig_records = sum(len(i["original_writes"]) for i in logs)
    secs = sorted(i["secs"] for i in items)
    return {
        "messages": len(items),
        "errors": len(items) - len(ok),
        "intent_agree": sum(1 for i in ok if i.get("intent") == i["original_intent"]),
        "intent_total": len(ok),
        "confusion": {f"{a}->{b}": sum(1 for i in ok if i["original_intent"] == a and i.get("intent") == b)
                      for a in ("log", "query", "chat") for b in ("log", "query", "chat")},
        "log_messages": len(logs),
        "records_original": orig_records,
        "records_matched": sum(i["match"]["matched"] for i in logs),
        "records_extra": sum(len(i["match"]["extra"]) for i in logs),
        "same_types": sum(1 for i in logs if i["match"]["same_types"]),
        "unknown_types": sum(1 for i in logs if i["unknown_types"]),
        "queries": len(queries),
        "found_agree": sum(1 for i in queries if i["found"] == i["original_found"]),
        "files": len(files),
        "file_errors": sum(1 for f in files if "error" in f),
        "kind_agree": sum(1 for f in files if f.get("candidate") and f.get("original")
                          and f["candidate"]["kind"] == (f["original"]["kind"] or "")),
        "secs_median": secs[len(secs) // 2] if secs else 0,
        "secs_p90": secs[int(len(secs) * 0.9)] if secs else 0,
        "usage": usage,
    }


def save_report(store: Store, report: dict) -> None:
    with store.tx(system=True) as tx:
        tx.set_state(f"eval:{report['id']}", report)
        index = [r for r in (tx.get_state("eval:index") or []) if r["id"] != report["id"]]
        index.insert(0, {"id": report["id"], "fast": report["fast"], "main": report["main"],
                         "started": report["started"], "status": report["status"]})
        for old in index[KEEP_REPORTS:]:
            tx.set_state(f"eval:{old['id']}", None)
        tx.set_state("eval:index", index[:KEEP_REPORTS])


def run_eval(store: Store, llm: BedrockLLM, tenant: dict, *, limit: int, workers: int,
             with_files: bool = True) -> dict:
    # Timestamp plus a random suffix: runs started together must not share an id.
    report = {"id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], "fast": llm.fast_model,
              "main": llm.main_model, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "status": "running", "items": [], "files": [], "summary": {}}
    save_report(store, report)
    rows = pick_messages(store, tenant["id"], limit)
    print(f"evaluating {len(rows)} messages with fast={llm.fast_model} main={llm.main_model}", flush=True)
    items: list[dict] = []
    report["messages_available"] = len(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, item in enumerate(pool.map(lambda r: eval_message(store, llm, tenant, r), rows), 1):
            items.append(item)
            if n % 20 == 0:
                print(f"  {n}/{len(rows)}", flush=True)
                report["items"] = items
                report["summary"] = summarise(items, [], llm.usage)
                save_report(store, report)
    files = eval_files(store, llm, tenant) if with_files else []
    report.update(items=items, files=files, summary=summarise(items, files, llm.usage), status="done",
                  finished=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    save_report(store, report)
    return report


def print_summary(s: dict) -> None:
    pct = lambda a, b: f"{a}/{b} ({100 * a // b}%)" if b else "0/0"
    print(f"messages: {s['messages']}, errors: {s['errors']}")
    print(f"intent agrees: {pct(s['intent_agree'], s['intent_total'])}")
    print(f"records matched: {pct(s['records_matched'], s['records_original'])}, extra records: {s['records_extra']}")
    print(f"same record types: {pct(s['same_types'], s['log_messages'])}, invented types: {s['unknown_types']}")
    print(f"questions, same found/not-found: {pct(s['found_agree'], s['queries'])}")
    print(f"files: {s['files']}, errors: {s['file_errors']}, same kind: {s['kind_agree']}")
    print(f"seconds per message: median {s['secs_median']}, p90 {s['secs_p90']}")
    for m, u in s["usage"].items():
        print(f"  {m}: {u['calls']} calls, {u['in']} in / {u['out']} out tokens")


def list_models(region: str) -> None:
    import boto3
    bedrock = boto3.client("bedrock", region_name=region)
    models = bedrock.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
    profiles = bedrock.list_inference_profiles().get("inferenceProfileSummaries", [])
    via_profile = {}
    for p in profiles:
        for m in p.get("models", []):
            via_profile.setdefault(m["modelArn"].split("/")[-1], p["inferenceProfileId"])
    for m in sorted(models, key=lambda m: (m["providerName"], m["modelId"])):
        if "ON_DEMAND" not in m.get("inferenceTypesSupported", []) and m["modelId"] not in via_profile:
            continue
        use = m["modelId"] if "ON_DEMAND" in m.get("inferenceTypesSupported", []) else via_profile[m["modelId"]]
        inputs = "+".join(i.lower() for i in m.get("inputModalities", []))
        print(f"{m['providerName']:<12} {use:<55} {inputs}")


def ping(models: list[str], region: str) -> None:
    for m in models:
        llm = BedrockLLM(fast_model=m, main_model=m, region=region)
        try:
            print(f"{m}: ok, {llm.classify('Remind me to call Jim tomorrow at 2pm')}")
        except (LLMError, Exception) as e:
            print(f"{m}: FAILED {str(e)[:300]}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true")
    p.add_argument("--ping", nargs="+")
    p.add_argument("--fast")
    p.add_argument("--main")
    p.add_argument("--limit", type=int, default=150)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--no-files", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings()
    if a.list:
        return list_models(settings.aws_region)
    if a.ping:
        return ping(a.ping, settings.aws_region)
    if not (a.fast and a.main):
        p.error("--fast and --main are needed (or --list / --ping)")
    store = Store(settings.database_url)
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(settings.owner_telegram_user_id)
    if tenant is None:
        sys.exit("owner account not found")
    llm = BedrockLLM(fast_model=a.fast, main_model=a.main, region=settings.aws_region)
    report = run_eval(store, llm, tenant, limit=a.limit, workers=a.workers, with_files=not a.no_files)
    print_summary(report["summary"])
    print(f"report: {settings.web_base_url or ''}/eval/{report['id']}")


if __name__ == "__main__":
    main()
