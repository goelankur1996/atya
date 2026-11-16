"""Circle: people who let each other ask ATYA to call them, with a short report back.

- `/circle add Dad [hinglish]` gives an invite link (WhatsApp and Telegram). The person taps it
  and sends the message it fills in (`/start c-XXXXXXXX`): someone new gets an account (no
  waitlist), someone already on ATYA is simply asked.
- The person being called agrees, with buttons: that creates a link caller -> callee. Then the
  inviter is asked whether the other person may ask ATYA to call *them* (the reverse link).
- A `checkin` record (filed like anything else: "call dad every day at 5:30 to check his
  situps") is turned into a call in the callee's call queue at its time. After the call (or
  after the retries run out) the person who asked gets a short report. Only that: nothing
  else of the callee's is shared.
- `/circle` lists links, `/circle remove <name>` ends them (both directions).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import secrets

from . import briefs
from .store import Store

log = logging.getLogger(__name__)

CODE_PREFIX = "c-"
INVITE_DAYS = 7
DAILY_CHECKINS = 5                 # check-in calls per link per day
LANGUAGES = ("english", "hinglish", "hindi")
LATE_LIMIT = dt.timedelta(hours=2)


def first(tenant: dict | None, fallback: str = "someone") -> str:
    name = ((tenant or {}).get("display_name") or "").strip()
    return name.split()[0] if name else fallback


def hindi(tenant: dict | None) -> bool:
    return (tenant or {}).get("call_language") in ("hinglish", "hindi")


def tell(store: Store, tenant: dict, body: str, buttons=None) -> None:
    with store.tx(tenant["id"]) as tx:
        tx.add_outbox(tenant["id"], chat_id=tenant.get("telegram_chat_id") or 0, body=body, buttons=buttons,
                      actor="circle")


# --- links -----------------------------------------------------------------------

def links(tx, tenant_id) -> tuple[list[dict], list[dict]]:
    """(people this user can ask ATYA to call, people who can ask ATYA to call this user)."""
    out = tx._all("SELECT l.*, t.display_name FROM circle_links l JOIN tenants t ON t.id = l.callee "
                  "WHERE l.caller = %s AND l.status = 'active' ORDER BY l.name", (tenant_id,))
    inn = tx._all("SELECT l.*, t.display_name FROM circle_links l JOIN tenants t ON t.id = l.caller "
                  "WHERE l.callee = %s AND l.status = 'active' ORDER BY t.display_name", (tenant_id,))
    return out, inn


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def find_link(tx, caller_id, name: str) -> dict | None:
    """The active link from this caller to the person they call `name` ("Dad", "dad", "papa"...)."""
    want = _norm(name)
    if not want:
        return None
    rows, _ = links(tx, caller_id)
    for r in rows:
        if _norm(r["name"]) == want:
            return r
    for r in rows:                     # their own name works too ("call Raj")
        if want in (_norm(r["display_name"] or ""), _norm(first(r))):
            return r
    return None


def link_between(tx, caller_id, callee_id) -> dict | None:
    return tx._one("SELECT * FROM circle_links WHERE caller = %s AND callee = %s AND status = 'active'",
                   (caller_id, callee_id))


def make_link(tx, caller_id, callee_id, name: str) -> dict:
    tx.conn.execute("UPDATE circle_links SET status = 'ended', ended_at = now() "
                    "WHERE caller = %s AND callee = %s AND status <> 'ended'", (caller_id, callee_id))
    return tx._one("INSERT INTO circle_links (caller, callee, name, status, accepted_at) "
                   "VALUES (%s, %s, %s, 'active', now()) RETURNING *", (caller_id, callee_id, name[:60]))


# --- /circle -----------------------------------------------------------------------

USAGE = ("Circle: people who let you ask ATYA to call them (to check in on something), with a short report back "
         "to you.\n\nAdd someone: /circle add <name>, e.g. /circle add Dad hinglish (the language their calls "
         "should be in: english, hinglish or hindi).\nRemove: /circle remove <name>.\n\nThen just ask, e.g. "
         "\"call Dad every day at 5:30 pm to check if he did his situps; motivate him if not\".")


def command(ctx, arg: str) -> str:
    """/circle, /circle add <name> [language], /circle remove <name>."""
    words = arg.split()
    if words and words[0].lower() == "add":
        return add(ctx, words[1:])
    if words and words[0].lower() in ("remove", "end", "stop"):
        return remove(ctx, " ".join(words[1:]))
    with ctx.store.tx(system=True) as tx:
        out, inn = links(tx, ctx.tenant["id"])
    lines = []
    if out:
        lines.append("You can ask ATYA to call: " + ", ".join(r["name"] for r in out) + ".")
    if inn:
        lines.append("Can ask ATYA to call you: " + ", ".join(first(r) for r in inn) + ".")
    ctx.reply(("\n".join(lines) + "\n\n" if lines else "Your circle is empty.\n\n") + USAGE)
    return "circle:list"


def add(ctx, words: list[str]) -> str:
    language = None
    if words and words[-1].lower() in LANGUAGES:
        language = words.pop().lower()
    name = " ".join(words).strip()
    if not name:
        ctx.reply("Who should it be? For example: /circle add Dad hinglish")
        return "circle:no-name"
    code = CODE_PREFIX + secrets.token_hex(4).upper()
    with ctx.store.tx(system=True) as tx:
        tx.conn.execute("INSERT INTO circle_invites (code, from_tenant, name, language, expires_at) "
                        "VALUES (%s, %s, %s, %s, now() + make_interval(days => %s))",
                        (code, ctx.tenant["id"], name[:60], language, INVITE_DAYS))
    from .commands import telegram_link, whatsapp_link
    ways = []
    if ctx.settings.whatsapp_on and ctx.settings.whatsapp_number:
        ways.append(f"On WhatsApp: {whatsapp_link(ctx.settings, f'/start {code}')}")
    tg = telegram_link(ctx)
    if tg:
        ways.append(f"On Telegram: {tg}?start={code}")
    ctx.reply(f"Send {name} this link (works once, for {INVITE_DAYS} days). They open it and send the message it "
              f"fills in; they don't need to be on ATYA already.\n\n" + "\n".join(ways) +
              f"\n\nATYA then asks {name} whether you may ask it to call them, and to send you a short summary "
              "of those calls. Nothing else of theirs is shared with you.", preview=False)
    return "circle:invite"


def remove(ctx, name: str) -> str:
    with ctx.store.tx(system=True) as tx:
        link = find_link(tx, ctx.tenant["id"], name)
        if link is None:
            _, inn = links(tx, ctx.tenant["id"])
            back = next((r for r in inn if _norm(name) in (_norm(r["display_name"] or ""), _norm(first(r)))), None)
            other = back["caller"] if back else None
        else:
            other = link["callee"]
        if other is None:
            ctx.reply(f"There's no one called {name} in your circle. Send /circle to see who's there.")
            return "circle:not-found"
        tx.conn.execute("UPDATE circle_links SET status = 'ended', ended_at = now() WHERE status <> 'ended' "
                        "AND ((caller = %s AND callee = %s) OR (caller = %s AND callee = %s))",
                        (ctx.tenant["id"], other, other, ctx.tenant["id"]))
        them = tx.tenant(other)
    ctx.reply(f"Done: {name} is out of your circle, both ways. No more check-in calls between you.")
    if them:
        tell(ctx.store, them, f"{first(ctx.tenant)} ne check-in calls band kar di hain." if hindi(them) else
             f"{first(ctx.tenant)} ended the check-in calls between you.")
    return "circle:removed"


# --- joining and agreeing -------------------------------------------------------------

def is_code(code: str) -> bool:
    return code.lower().startswith(CODE_PREFIX) and len(code) >= 6


def join(ctx, code: str) -> str:
    """/start c-XXXX: from an invite link. Creates an account for someone new (no waitlist)."""
    code = CODE_PREFIX + code[len(CODE_PREFIX):].upper()
    with ctx.store.tx(system=True) as tx:
        inv = tx._one("SELECT * FROM circle_invites WHERE code = %s", (code,))
    if inv is None or inv["used_at"] is not None or inv["expires_at"] < dt.datetime.now(dt.timezone.utc):
        ctx.reply("That invite link has expired or was already used. Ask for a new one.")
        return "circle:bad-code"
    tenant = ctx.tenant
    if tenant is not None and tenant["id"] == inv["from_tenant"]:
        ctx.reply("That's your own invite link: send it to the person you want to add.")
        return "circle:own-code"
    if tenant is None:
        from .commands import TZ_PROMPT, tz_buttons
        with ctx.store.tx(system=True) as tx:
            whatsapp = ctx.channel == "whatsapp"
            tenant = tx.create_tenant(telegram_user_id=None if whatsapp else ctx.user_id,
                                      telegram_chat_id=None if whatsapp else ctx.chat_id,
                                      whatsapp_phone=str(ctx.user_id) if whatsapp else None,
                                      display_name=ctx.name, status="invited", actor="circle")
            if whatsapp:
                tx.conn.execute("UPDATE tenants SET whatsapp_last_inbound = now() WHERE id = %s", (tenant["id"],))
            if inv["language"]:
                tx.conn.execute("UPDATE tenants SET call_language = %s WHERE id = %s", (inv["language"], tenant["id"]))
            tx.waitlist_remove_person(whatsapp_phone=tenant.get("whatsapp_phone"),
                                      telegram_user_id=tenant.get("telegram_user_id"))
            tx.set_state(pending_key(tenant["id"]), {"code": code})
        ctx.tenant = tenant
        log.info("circle: new account %s from an invite", str(tenant["id"])[:4])
        ctx.reply(TZ_PROMPT, buttons=tz_buttons())
        return "circle:joined-new"
    if tenant["status"] == "invited":
        with ctx.store.tx(system=True) as tx:
            tx.set_state(pending_key(tenant["id"]), {"code": code})
        from .commands import TZ_PROMPT, tz_buttons
        ctx.reply(TZ_PROMPT, buttons=tz_buttons())
        return "circle:joined-invited"
    if inv["language"] and tenant.get("call_language") == "english":
        with ctx.store.tx(system=True) as tx:
            tx.conn.execute("UPDATE tenants SET call_language = %s WHERE id = %s", (inv["language"], tenant["id"]))
            tenant = tx.tenant(tenant["id"])
    ask(ctx.store, tenant, inv)
    return "circle:asked"


def pending_key(tenant_id) -> str:
    return f"circle:pending:{tenant_id}"


def after_onboarding(store: Store, tenant: dict) -> None:
    """A new account made from an invite: once set up, ask the circle question."""
    with store.tx(system=True) as tx:
        pending = tx.get_state(pending_key(tenant["id"]))
        if not pending:
            return
        tx.set_state(pending_key(tenant["id"]), None)
        inv = tx._one("SELECT * FROM circle_invites WHERE code = %s AND used_at IS NULL", (pending["code"],))
        tenant = tx.tenant(tenant["id"])
    if inv:
        ask(store, tenant, inv)


def ask(store: Store, tenant: dict, inv: dict) -> None:
    with store.tx(system=True) as tx:
        inviter = tx.tenant(inv["from_tenant"])
    who = first(inviter)
    if hindi(tenant):
        body = (f"{who} chahte hain ki ATYA unke kehne par aapko call kare (jaise exercise ya khaane ka check-in), "
                f"aur har aisi call ke baad unhe ek chhota summary bheje. Aapka aur kuch {who} ke saath share nahi "
                "hoga. Allow karein?")
        buttons = [[("Haan, allow", f"circle:allow:{inv['code']}"), ("Abhi nahi", f"circle:no:{inv['code']}")]]
    else:
        body = (f"{who} would like ATYA to call you when they ask (for example, to check in on your exercise), and to "
                f"send them a short summary after each of those calls. Nothing else of yours is shared with {who}. "
                "Allow?")
        buttons = [[("Allow", f"circle:allow:{inv['code']}"), ("Not now", f"circle:no:{inv['code']}")]]
    tell(store, tenant, body, buttons)


def tap(store: Store, tenant: dict | None, data: str) -> str:
    """circle:allow|no:<code> (the person being called), circle:back|backno:<link id> (the inviter)."""
    parts = data.split(":", 2)
    if len(parts) != 3 or tenant is None or tenant["status"] != "active":
        return "ignored:circle"
    _, choice, ref = parts
    if choice in ("allow", "no"):
        with store.tx(system=True) as tx:
            inv = tx._one("SELECT * FROM circle_invites WHERE code = %s AND used_at IS NULL", (ref,))
            if inv is None or inv["from_tenant"] == tenant["id"]:
                return "ignored:circle-used"
            tx.conn.execute("UPDATE circle_invites SET used_by = %s, used_at = now() WHERE code = %s",
                            (tenant["id"], ref))
            inviter = tx.tenant(inv["from_tenant"])
            link = make_link(tx, inviter["id"], tenant["id"], inv["name"]) if choice == "allow" else None
        if link is None:
            tell(store, tenant, "Theek hai, koi call nahi hogi." if hindi(tenant) else "Okay, no calls then.")
            tell(store, inviter, f"{inv['name']} said not now to check-in calls.")
            return "circle:declined"
        tell(store, tenant, f"Ho gaya. {first(inviter)} ab ATYA se aapko call karwa sakte hain. Band karne ke liye "
                            "/circle bhejein." if hindi(tenant) else
             f"Done. {first(inviter)} can now ask ATYA to call you. Send /circle any time to see or end it.")
        tell(store, inviter, f"{inv['name']} said yes. Ask me things like \"call {inv['name']} every day at 5:30 pm to "
                             f"check …\" or \"call {inv['name']} tomorrow at 1 to ask …\". You'll get a short report "
                             "after each call.")
        tell(store, inviter, f"Should {first(tenant, inv['name'])} be able to ask ATYA to call you too?",
             [[("Yes", f"circle:back:{link['id']}"), ("No", f"circle:backno:{link['id']}")]])
        return "circle:allowed"
    if choice in ("back", "backno"):
        with store.tx(system=True) as tx:
            link = tx._one("SELECT * FROM circle_links WHERE id = %s AND caller = %s AND status = 'active'",
                           (ref, tenant["id"]))
            if link is None:
                return "ignored:circle-link"
            other = tx.tenant(link["callee"])
            if choice == "backno":
                return "circle:back-declined"
            make_link(tx, other["id"], tenant["id"], first(tenant, "them"))
        tell(store, tenant, f"Done: {link['name']} can ask ATYA to call you too.")
        tell(store, other, f"{first(tenant)} ne bhi allow kiya hai: aap ATYA se unhe call karwa sakte hain, jaise "
                           f"\"{first(tenant)} ko kal 9 baje call karke yaad dilao …\"." if hindi(other) else
             f"{first(tenant)} allowed it too: you can ask ATYA to call them, e.g. \"call {first(tenant)} tomorrow "
             "at 9 to remind them …\".")
        return "circle:back-allowed"
    return "ignored:circle"


# --- check-in calls ----------------------------------------------------------------

def _due_day(rec: dict, local: dt.datetime, at: dt.time, tz) -> dt.date | None:
    """The day this check-in should happen on, if it's today (in the callee's time)."""
    f = rec["fields"]
    repeat = (f.get("Repeat") or "once").lower()
    today = local.date()
    if repeat == "daily":
        return today
    if repeat == "weekdays":
        return today if today.weekday() < 5 else None
    if f.get("Date"):
        try:
            day = dt.date.fromisoformat(str(f["Date"])[:10])
        except ValueError:
            return None
    else:                                          # "once" with no date: the next time it comes round
        made = rec["created_at"].astimezone(tz)
        day = made.date() if made.time() < at else made.date() + dt.timedelta(days=1)
    return today if day == today else None


def queue_checkins(store: Store, now: dt.datetime) -> int:
    """Check-ins whose time has come become calls in the callee's queue."""
    from zoneinfo import ZoneInfo
    with store.tx(system=True) as tx:
        recs = tx._all("SELECT r.* FROM records r JOIN tenants t ON t.id = r.tenant_id WHERE r.type = 'checkin' "
                       "AND coalesce(r.fields->>'Status', 'active') = 'active' AND t.status = 'active'")
    n = 0
    for rec in recs:
        try:
            n += _queue_one(store, rec, now, ZoneInfo)
        except Exception:
            log.exception("check-in %s couldn't be queued", rec["id"])
    return n


def _queue_one(store: Store, rec: dict, now: dt.datetime, ZoneInfo) -> int:
    f = rec["fields"]
    at = briefs.parse_time(str(f.get("Time") or ""))
    if at is None:
        return 0
    with store.tx(system=True) as tx:
        link = find_link(tx, rec["tenant_id"], str(f.get("Person") or ""))
        requester = tx.tenant(rec["tenant_id"])
        callee = tx.tenant(link["callee"]) if link else None
    if link is None or callee is None:
        _pause(store, rec, requester, f"I can't call {f.get('Person') or 'them'}: they're not in your circle "
                                      "(any more). Add them with /circle add <name>; I've paused this check-in.")
        return 0
    tz = ZoneInfo(callee["timezone"] or "UTC")
    local = now.astimezone(tz)
    day = _due_day(rec, local, at, tz)
    if day is None:
        return 0
    due = dt.datetime.combine(day, at, tzinfo=tz)
    if not (due <= local < due + LATE_LIMIT):
        return 0
    with store.tx(system=True) as tx:
        today_n = tx._one("SELECT count(*) AS n FROM calls WHERE kind = 'checkin' AND requested_by = %s "
                          "AND tenant_id = %s AND day = %s", (requester["id"], callee["id"], day))["n"]
        if today_n >= DAILY_CHECKINS and not tx._one("SELECT 1 AS x FROM calls WHERE checkin_id = %s AND day = %s",
                                                     (rec["id"], day)):
            log.info("check-in %s skipped: %d calls to this person today already", rec["id"], today_n)
            return 0
        call = tx.add_call(callee["id"], "checkin", due.astimezone(dt.timezone.utc), about=str(f.get("Purpose") or ""),
                           day=day, requested_by=requester["id"], checkin_id=rec["id"])
    if call and (f.get("Repeat") or "once").lower() == "once":
        from .schema import load_schema
        with store.tx(requester["id"]) as tx:
            tx.update_record(requester["id"], load_schema(requester["schema_name"]), rec["id"], {"Status": "done"},
                             actor="circle")
    return 1 if call else 0


def _pause(store: Store, rec: dict, requester: dict | None, note: str) -> None:
    if requester is None:
        return
    from .schema import load_schema
    with store.tx(requester["id"]) as tx:
        tx.update_record(requester["id"], load_schema(requester["schema_name"]), rec["id"], {"Status": "paused"},
                         actor="circle")
    tell(store, requester, note)


def allowed(store: Store, call: dict) -> bool:
    """A check-in call still has the callee's agreement."""
    with store.tx(system=True) as tx:
        return link_between(tx, call["requested_by"], call["tenant_id"]) is not None


def report(store: Store, llm, call: dict, callee: dict, result: str) -> None:
    """Tell the person who asked how the check-in went (a summary of that purpose only)."""
    with store.tx(system=True) as tx:
        requester = tx.tenant(call["requested_by"]) if call.get("requested_by") else None
        link = link_between(tx, call["requested_by"], callee["id"]) if requester else None
        transcript = (tx._one("SELECT raw_content FROM inbox WHERE id = %s", (call["transcript_id"],))
                      if call.get("transcript_id") else None)
    if requester is None:
        return
    name = link["name"] if link else first(callee)
    about = call.get("about") or "the check-in"
    if result == "answered":
        body = None
        if transcript and llm is not None:
            try:
                body = llm.call_report(transcript=transcript["raw_content"], purpose=about, name=name)
            except Exception:
                log.exception("check-in report failed")
        body = body or f"{name} picked up, but I couldn't summarise the call."
        tell(store, requester, f"📞 {name}, {about}:\n{body}")
        return
    what = {"declined": f"{name} declined the call", "no-line": f"I have no way to call {name}",
            "no-consent": f"{name} is no longer in your circle", "no-minutes": f"{name} is out of call minutes today",
            "unreachable": f"I couldn't reach {name}'s phone"}.get(result, f"I couldn't reach {name}")
    tries = f" ({call.get('attempts', 1)} tries)" if result == "missed" else ""
    tell(store, requester, f"📞 {what}{tries}, for: {about}.")
