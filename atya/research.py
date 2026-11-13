"""Web research on request ("research X", "find me 3 good physios in Noida").

Amazon Nova 2 Lite with its built-in web search (Nova Web Grounding) on
Bedrock: it searches, reads and writes an answer with sources. The result is
sent to the user and saved as a document record (Kind: report), so it can be
found again later ("what did you find about physios?").
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

log = logging.getLogger("research")

MODEL = "us.amazon.nova-2-lite-v1:0"      # web grounding needs a US inference profile
MAX_SOURCES = 6

SYSTEM = """You are a research assistant for one person. Search the web and answer their request with current, specific, checkable information.

- Lead with the answer. Then the specifics: names, prices, dates, addresses, phone numbers or links where they help.
- When they ask for options, give 3 to 5, each in one or two lines with why it fits.
- Prefer sources from the last year. Say plainly when something could not be found or sources disagree.
- Location and dates: the person is in the timezone {timezone}; today is {today}. Use that for "near me", "this weekend", prices and currency, unless they say otherwise.
- Plain text for a chat app: short paragraphs or "- " bullets, no markdown headings, no tables, under 250 words.
- Don't list sources in the text; they are added separately."""

PREFIX = re.compile(r"^\s*(?:/?research|look\s+up|find\s+out)\b[\s:,-]*", re.IGNORECASE)


class Researcher(Protocol):
    def research(self, topic: str, *, timezone: str, today: str) -> dict: ...   # {"text", "sources"}


def explicit(text: str) -> str | None:
    """'research visa rules for Japan' -> 'visa rules for Japan'; None if not an explicit request."""
    if not re.match(r"^\s*/?research\b", text, re.IGNORECASE):
        return None
    topic = PREFIX.sub("", text, count=1).strip()
    return topic or None


def sources_from(obj: Any) -> list[str]:
    """Every web URL cited anywhere in a Converse response, in order, without repeats."""
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            web = o.get("web")
            if isinstance(web, dict) and isinstance(web.get("url"), str) and web["url"] not in found:
                found.append(web["url"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return found


def with_sources(text: str, sources: list[str]) -> str:
    if not sources:
        return text
    return text.rstrip() + "\n\nSources:\n" + "\n".join(f"- {u}" for u in sources[:MAX_SOURCES])


class NovaResearcher:
    def __init__(self, *, model: str = MODEL, region: str = "us-east-1", client: Any = None) -> None:
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(read_timeout=180, retries={"max_attempts": 3, "mode": "adaptive"}))
        self.client, self.model = client, model
        self.usage: dict[str, dict[str, float]] = {}

    def research(self, topic: str, *, timezone: str, today: str) -> dict:
        t0 = time.monotonic()
        resp = self.client.converse(
            modelId=self.model,
            system=[{"text": SYSTEM.format(timezone=timezone, today=today)}],
            messages=[{"role": "user", "content": [{"text": topic[:2000]}]}],
            toolConfig={"tools": [{"systemTool": {"name": "nova_grounding"}}]},
            inferenceConfig={"maxTokens": 3000, "temperature": 0.3})
        parts = (resp.get("output") or {}).get("message", {}).get("content", [])
        text = "".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
        for p in parts:                                  # citations may carry their own text spans
            cc = p.get("citationsContent") if isinstance(p, dict) else None
            if cc and not text:
                text += "".join(c.get("text", "") for c in cc.get("content", []) if isinstance(c, dict))
        u = resp.get("usage") or {}
        m = self.usage.setdefault(self.model, {"calls": 0, "in": 0, "out": 0, "secs": 0.0})
        m["calls"] += 1
        m["in"] += u.get("inputTokens", 0)
        m["out"] += u.get("outputTokens", 0)
        m["secs"] += time.monotonic() - t0
        return {"text": re.sub(r"\n{3,}", "\n\n", text).strip(), "sources": sources_from(parts)}
