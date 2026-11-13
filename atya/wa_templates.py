"""WhatsApp message templates: what they say, building a send, creating them at Meta.

Outside the 24-hour window WhatsApp only delivers approved templates:
- atya_reminder: the reminder itself, with Done / In 1 hour / Tomorrow 9am buttons.
- atya_brief:    "your morning brief is ready", with a Show button.
- atya_update:   "you have N messages waiting", with a Show button.
Tapping Show (or writing anything) opens the window and the waiting messages go out.

    python -m atya.wa_templates create     # submit all three for approval
    python -m atya.wa_templates status     # see whether they're approved
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import requests

from .config import load_settings

GRAPH = "https://graph.facebook.com/v25.0"
LANG = "en"
SHOW = "show"                 # the payload of the Show button

TEMPLATES = [
    {"name": "atya_reminder", "category": "UTILITY", "language": LANG, "components": [
        {"type": "BODY", "text": "Reminder: {{1}}. Tap a button below to mark it done or snooze it.",
         "example": {"body_text": [["Call Jim about the shipment (TSK-12)"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Done"},
                                         {"type": "QUICK_REPLY", "text": "In 1 hour"},
                                         {"type": "QUICK_REPLY", "text": "Tomorrow 9am"}]}]},
    {"name": "atya_brief", "category": "UTILITY", "language": LANG, "components": [
        {"type": "BODY", "text": "Good morning! Your daily brief for {{1}} is ready. Tap Show to read it.",
         "example": {"body_text": [["Friday 14 November"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Show"}]}]},
    {"name": "atya_update", "category": "UTILITY", "language": LANG, "components": [
        {"type": "BODY", "text": "You have {{1}} waiting for you in ATYA. Tap Show to read it.",
         "example": {"body_text": [["2 new messages"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Show"}]}]},
    # v2: worded as replies to what the user asked for, with their own details, so Meta
    # keeps them in the cheaper, reliably delivered Utility category (v1 became Marketing).
    {"name": "atya_brief_v2", "category": "UTILITY", "language": LANG, "components": [
        {"type": "BODY", "text": "Your daily task summary for {{1}}: {{2}} due today, {{3}} overdue. "
                                 "Tap Show for the full list.",
         "example": {"body_text": [["Friday 14 November", "3", "1"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Show"}]}]},
    {"name": "atya_reply_v2", "category": "UTILITY", "language": LANG, "components": [
        {"type": "BODY", "text": "There's a reply to your message from {{1}} waiting. Tap Show to read it.",
         "example": {"body_text": [["today at 6:40 pm"]]}},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Show"}]}]},
]

# Not approved (yet) or missing: send the older template instead.
NOT_USABLE_CODES = {132000, 132001, 132005, 132007, 132012, 132015, 132016}


def param(text: str, limit: int = 200) -> str:
    """Template parameters can't have line breaks, tabs or long runs of spaces."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return (t[: limit - 1] + "…") if len(t) > limit else (t or "-")


def payload(name: str, to: str, params: list[str], button_payloads: list[str]) -> dict:
    components: list[dict] = [{"type": "body", "parameters": [{"type": "text", "text": param(p)} for p in params]}]
    for i, p in enumerate(button_payloads):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": p}]})
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "template",
            "template": {"name": name, "language": {"code": LANG}, "components": components}}


def reminder(to: str, body: str, buttons: list[list[list[str]]] | None) -> dict:
    title = body.splitlines()[0].lstrip("⏰ ").strip() if body else "a task"
    datas = [b[1] for row in (buttons or []) for b in row][:3]
    return payload("atya_reminder", to, [title], datas)


def brief_ready(to: str, day_label: str) -> dict:
    return payload("atya_brief", to, [day_label], [SHOW])


def update_waiting(to: str, count: int) -> dict:
    return payload("atya_update", to, [f"{count} new message{'s' if count != 1 else ''}"], [SHOW])


def brief_summary(to: str, day_label: str, due_today: int, overdue: int) -> dict:
    return payload("atya_brief_v2", to, [day_label, str(due_today), str(overdue)], [SHOW])


def reply_waiting(to: str, when: str) -> dict:
    return payload("atya_reply_v2", to, [when], [SHOW])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["create", "status"])
    p.add_argument("names", nargs="*", help="only these templates (default: all)")
    a = p.parse_args()
    st = load_settings()
    if not (st.whatsapp_token and st.whatsapp_waba_id):
        sys.exit("needs WHATSAPP_TOKEN and WHATSAPP_WABA_ID")
    http = requests.Session()
    http.headers["Authorization"] = f"Bearer {st.whatsapp_token}"
    url = f"{GRAPH}/{st.whatsapp_waba_id}/message_templates"
    if a.action == "create":
        for t in [t for t in TEMPLATES if not a.names or t["name"] in a.names]:
            r = http.post(url, json=t, timeout=30)
            body = r.json()
            if r.ok:
                print(f"{t['name']}: submitted ({body.get('status', '?')}, category {body.get('category', '?')})")
            else:
                print(f"{t['name']}: {body.get('error', {}).get('error_user_msg') or body.get('error', {}).get('message') or r.text[:300]}")
    else:
        r = http.get(url, params={"fields": "name,status,category,rejected_reason", "limit": 50}, timeout=30)
        rows = [t for t in r.json().get("data", []) if t["name"].startswith("atya_")]
        for t in rows:
            extra = f" ({t['rejected_reason']})" if t.get("rejected_reason") not in (None, "NONE") else ""
            print(f"{t['name']}: {t['status']}, {t['category']}{extra}")
        if not rows:
            print(json.dumps(r.json())[:500])


if __name__ == "__main__":
    main()
