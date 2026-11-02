"""Embeddings (Voyage AI) and the background worker that keeps them current.

Records are embedded asynchronously so a capture never waits on, or fails
because of, the embeddings API. A record is (re-)embedded whenever its content
changed after its last embedding. Without VOYAGE_API_KEY the worker is off and
search falls back to full text only.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import requests

from .config import EMBEDDING_DIM, Settings
from .schema import load_schema
from .store import Store

log = logging.getLogger("embeddings")

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_MODEL = "voyage-3.5"
BATCH = 64
MAX_TEXT_CHARS = 6000


class EmbeddingError(Exception):
    pass


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str = VOYAGE_MODEL) -> None:
        self.model = model
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {api_key}"

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        try:
            r = self.http.post(VOYAGE_URL, timeout=60, json={
                "input": texts, "model": self.model, "input_type": input_type,
                "output_dimension": EMBEDDING_DIM})
        except requests.RequestException as e:
            raise EmbeddingError(str(e)) from e
        if r.status_code != 200:
            raise EmbeddingError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]


def record_text(rec: dict, label: str) -> str:
    """What gets embedded: type, title, field values, then the body."""
    parts = [f"{label}: {rec['title']}"]
    for k, v in (rec.get("fields") or {}).items():
        parts.append(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    if rec.get("body"):
        parts.append(rec["body"])
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def embed_pending(store: Store, embedder: Embedder, *, limit: int = BATCH) -> int:
    with store.tx() as tx:
        rows = tx.records_needing_embedding(limit=limit)
    if not rows:
        return 0
    texts = []
    for r in rows:
        with store.tx() as tx:
            tenant = tx.tenant(r["tenant_id"])
        schema = load_schema(tenant["schema_name"] if tenant else "default")
        tdef = schema.types.get(r["type"])
        texts.append(record_text(r, tdef.label if tdef else r["type"]))
    vectors = embedder.embed_documents(texts)
    with store.tx() as tx:
        for r, vec in zip(rows, vectors):
            # Only store if the record has not changed since we read it.
            tx.set_embedding(r["id"], vec, if_updated_at=r["updated_at"])
    return len(rows)


def run(store: Store, embedder: Embedder, settings: Settings) -> None:
    log.info("embedder started")
    while True:
        try:
            if embed_pending(store, embedder) == 0:
                time.sleep(settings.poll_interval)
        except EmbeddingError as e:
            log.warning("voyage: %s", e)
            time.sleep(30)
        except Exception:
            log.exception("embedder loop error")
            time.sleep(settings.poll_interval)
