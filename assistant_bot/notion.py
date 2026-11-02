"""One Notion HTTP client + property/block helpers — replaces 3 separate copies."""

from __future__ import annotations

from typing import Any, Iterable

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
RICH_TEXT_LIMIT = 2000


class NotionError(Exception):
    pass


class Notion:
    def __init__(self, token: str) -> None:
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _req(self, method: str, path: str, *, json_body: dict | None = None,
             params: dict | None = None, timeout: int = 30) -> dict:
        try:
            r = requests.request(
                method, f"{NOTION_API}{path}", headers=self.headers,
                json=json_body, params=params, timeout=timeout,
            )
        except requests.RequestException as e:
            raise NotionError(f"{method} {path}: network error: {e}") from e
        if not r.ok:
            raise NotionError(f"{method} {path}: {r.status_code} {r.text}")
        return r.json()

    def get_data_source(self, ds_id: str) -> dict:
        return self._req("GET", f"/data_sources/{ds_id}")

    def query(
        self,
        ds_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> list[dict]:
        body: dict[str, Any] = {"page_size": page_size}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        results: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            if cursor:
                body["start_cursor"] = cursor
            resp = self._req("POST", f"/data_sources/{ds_id}/query", json_body=body)
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return results

    def get_page(self, page_id: str) -> dict:
        return self._req("GET", f"/pages/{page_id}")

    def create_page(self, *, parent_ds_id: str, properties: dict,
                    children: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": parent_ds_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        return self._req("POST", "/pages", json_body=body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._req("PATCH", f"/pages/{page_id}",
                         json_body={"properties": properties})

    def get_block_children(self, block_id: str) -> list[dict]:
        results: list[dict] = []
        params: dict[str, Any] = {"page_size": 100}
        while True:
            resp = self._req("GET", f"/blocks/{block_id}/children", params=params)
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            params["start_cursor"] = resp["next_cursor"]
        return results

    def get_page_blocks_deep(self, page_id: str, *, max_depth: int = 3) -> list[dict]:
        return self._with_children(page_id, depth=0, max_depth=max_depth)

    def _with_children(self, block_id: str, *, depth: int, max_depth: int) -> list[dict]:
        blocks = self.get_block_children(block_id)
        if depth >= max_depth:
            return blocks
        for b in blocks:
            if b.get("has_children"):
                b["children"] = self._with_children(
                    b["id"], depth=depth + 1, max_depth=max_depth,
                )
        return blocks


# --- property/block helpers ----------------------------------------------

def _seg(content: str) -> dict:
    return {"type": "text", "text": {"content": content}}


def _chunks(text: str, size: int = RICH_TEXT_LIMIT) -> list[str]:
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def rich_text_value(text: str) -> list[dict]:
    return [_seg(c) for c in _chunks(text)]


def title_value(text: str, *, max_chars: int = 200) -> list[dict]:
    return [_seg(text[:max_chars])]


def paragraph_blocks(text: str) -> list[dict]:
    return [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_seg(c)]}}
        for c in _chunks(text)
    ]


def extract_plain_text(rich_text: Iterable[dict] | None) -> str:
    if not rich_text:
        return ""
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def get_property_text(page: dict, name: str) -> str:
    prop = (page.get("properties") or {}).get(name)
    if not prop:
        return ""
    ptype = prop.get("type")
    val = prop.get(ptype)
    if val is None:
        return ""
    if ptype in ("title", "rich_text"):
        return extract_plain_text(val)
    if ptype in ("select", "status"):
        return (val or {}).get("name", "")
    if ptype == "date":
        return (val or {}).get("start", "")
    if ptype == "url":
        return val or ""
    if ptype == "number":
        return "" if val is None else str(val)
    if ptype == "auto_increment_id":
        prefix = (val or {}).get("prefix") or ""
        n = (val or {}).get("number")
        return f"{prefix}-{n}" if prefix and n is not None else (str(n) if n is not None else "")
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in (val or []))
    if ptype == "people":
        return ", ".join(p.get("name", "") for p in (val or []))
    if ptype == "relation":
        return ", ".join(r.get("id", "") for r in (val or []))
    return ""
