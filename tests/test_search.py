"""Milestone 3: embeddings worker and hybrid (full-text + vector) retrieval."""

from __future__ import annotations

import hashlib
import math
import re

import pytest

from atya import embeddings, processor
from atya.config import EMBEDDING_DIM
from atya.processor import fuse
from atya.schema import load_schema

S = load_schema("default")

# Words the fake treats as meaning the same thing, to stand in for semantics.
SYNONYMS = {"provider": "vendor", "vendor": "vendor", "voyage": "vendor", "embeddings": "vectors",
            "embedding": "vectors", "vectors": "vectors", "vector": "vectors"}


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * EMBEDDING_DIM
        for w in re.findall(r"[a-z]+", text.lower()):
            w = SYNONYMS.get(w, w)
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % EMBEDDING_DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        self.calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def drained(store):
    """Embed whatever earlier tests left behind so counts below are exact."""
    emb = FakeEmbedder()
    while embeddings.embed_pending(store, emb, limit=500):
        pass
    return emb


def test_worker_embeds_and_reembeds_after_change(store, tenant, drained):
    emb = drained
    with store.tx() as tx:
        rec, _ = tx.create_record(tenant["id"], S, "task", title="Book flights to Bengaluru")
    assert embeddings.embed_pending(store, emb) == 1
    assert embeddings.embed_pending(store, emb) == 0          # storing a vector is not a change
    with store.tx() as tx:
        tx.update_record(tenant["id"], S, rec["id"], {"Notes": "window seat"})
    assert embeddings.embed_pending(store, emb) == 1          # content changed -> re-embed


def test_stale_vector_is_not_stored(store, tenant, drained):
    with store.tx() as tx:
        rec, _ = tx.create_record(tenant["id"], S, "task", title="old title")
        stale_ts = tx.record(tenant["id"], rec["id"])["updated_at"]
    with store.tx() as tx:   # a later transaction, so updated_at really moves
        tx.update_record(tenant["id"], S, rec["id"], {"Notes": "changed meanwhile"})
        assert tx.set_embedding(rec["id"], [0.1] * EMBEDDING_DIM, if_updated_at=stale_ts) is False
        assert tx.record(tenant["id"], rec["id"])["embedding"] is None


def test_vector_search_finds_by_meaning(store, tenant, drained):
    emb = drained
    with store.tx() as tx:
        tx.create_record(tenant["id"], S, "decision", title="Picked Voyage for vector search")
        tx.create_record(tenant["id"], S, "task", title="Buy groceries")
    embeddings.embed_pending(store, emb)
    with store.tx() as tx:
        hits = tx.search_vector(tenant["id"], emb.embed_query("which embeddings provider?"), limit=1)
        assert hits[0]["title"] == "Picked Voyage for vector search"
        # Full text alone misses it: no shared words.
        assert tx.search_text(tenant["id"], "which embeddings provider") == []


def test_fuse_prefers_records_found_by_both():
    a, b, c = ({"id": i} for i in ("a", "b", "c"))
    assert [r["id"] for r in fuse([a, b], [c, b])] == ["b", "a", "c"]


def test_answer_reaches_records_older_than_the_latest_100(store, tenant, drained):
    emb = drained
    tid = tenant["id"]
    with store.tx() as tx:
        old, _ = tx.create_record(tid, S, "decision", title="Picked Voyage for vector search",
                                  fields={"Reasoning": "good retrieval quality"})
        # Make it genuinely old: 120 newer decisions follow.
        tx.conn.execute("UPDATE records SET updated_at = now() - interval '60 days', "
                        "created_at = now() - interval '60 days' WHERE id = %s", (old["id"],))
        for i in range(120):
            tx.create_record(tid, S, "decision", title=f"Routine choice number {i}")
    while embeddings.embed_pending(store, emb, limit=200):
        pass
    plan = {"types": ["decision"], "search": "provider"}
    with store.tx() as tx:
        without = processor.gather_records(tx, S, tid, plan)
        with_vec = processor.gather_records(tx, S, tid, plan, emb.embed_query("which embeddings provider did I choose"))
    assert "DEC-1" not in [r["id"] for r in without]
    assert with_vec[0]["id"] == "DEC-1"


def test_record_text_includes_fields_and_body():
    text = embeddings.record_text({"title": "Rahul", "fields": {"Company": "Addverb", "Tags": ["a", "b"]},
                                   "body": "met at IITD"}, "People")
    assert text == "People: Rahul\nCompany: Addverb\nTags: a, b\nmet at IITD"
