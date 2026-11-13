"""Slash commands and onboarding. No AI here.

/start <code>   join with an invite code (or: the owner's first message)
/invite         owner only: create a one-time invite code
/help           what the bot does and the commands
/web            one-time link to the web view
/link           connect the other channel (Telegram <-> WhatsApp) to the same account
/export         download everything as a zip
/delete         erase your account (asks for a typed confirmation code)

New users pick a timezone (buttons, or type a name like Europe/Berlin) before
anything they send is stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import zoneinfo
from dataclasses import dataclass

from . import briefs
from .config import Settings
from .export import build_export
from .schema import load_schema
from .store import Store
from .telegram import TelegramAPI

log = logging.getLogger("commands")

TIMEZONE_CHOICES = [
    ("India", "Asia/Kolkata"), ("UK", "Europe/London"),
    ("US East", "America/New_York"), ("US West", "America/Los_Angeles"),
    ("Central Europe", "Europe/Berlin"), ("Dubai", "Asia/Dubai"),
    ("Singapore", "Asia/Singapore"), ("Sydney", "Australia/Sydney"),
]
DELETE_CONFIRM_MINUTES = 10
LINK_CODE_MINUTES = 15
LOGIN_LINK_MINUTES = 15
STRANGER_REPLY_HOURS = 24

HELP = """Send me anything you want remembered: tasks, people you met, decisions, ideas, links, how your day went. I file it for you.

Ask me questions about it: "what do I owe Rahul?", "what did I decide about pricing?", "what's on this week?"

Progress counts too: "sent the deck to Meera" closes that task.

Reminders: "remind me to call Jim at 2pm" and I'll message you at 2pm. PDFs: send the file and I'll file and summarise it.

Commands:
/brief 9am - a morning list of what's due, every day at that time (/brief off to stop)
/web - a one-time link to browse, search and edit your records in a browser
/link - use me on both Telegram and WhatsApp, with the same records
/export - download everything you've sent and everything I've filed, as a zip
/delete - permanently erase your account and data
/help - this message"""

WELCOME = "You're set up.\n\n" + HELP
INVITE_ONLY = "This bot is invite-only. If someone shared it with you, ask them for an invite code, then send: /start <code>"
TZ_PROMPT = ("Welcome. One question first: which timezone are you in? It's used for dates like "
             "\"by Friday\". Tap one, or type its name (for example Europe/Paris).")


@dataclass
class Ctx:
    store: Store
    tg: TelegramAPI
    settings: Settings
    user_id: int
    chat_id: int
    name: str | None
    tenant: dict | None
    # Inbox row of the command being handled; replies are logged against it.
    inbox_id: object | None = None
    # 'telegram' or 'whatsapp'. On WhatsApp, user_id and chat_id are both the number.
    channel: str = "telegram"
    # On WhatsApp: the Telegram client, only to name the bot in links.
    telegram: TelegramAPI | None = None

    def reply(self, text: str, buttons: list[list[tuple[str, str]]] | None = None,
              preview: bool = True, *, log_text: str | None = None) -> None:
        """Send a reply. log_text: what to store instead (e.g. with a secret hidden)."""
        self.tg.send_message(self.chat_id, text, buttons=buttons, preview=preview)
        self.note_sent(log_text if log_text is not None else text)

    def note_sent(self, text: str) -> None:
        """Keep a copy of a directly-sent message in the outbox (best effort)."""
        if self.inbox_id is None or self.tenant is None:
            return
        try:
            with self.store.tx(self.tenant["id"]) as tx:
                tx.log_sent(self.tenant["id"], chat_id=self.chat_id, body=text, inbox_id=self.inbox_id,
                            channel=self.channel)
        except Exception:
            log.warning("could not log a sent message", exc_info=True)

    @property
    def is_owner(self) -> bool:
        owner = self.settings.owner_telegram_user_id
        if not owner:
            return False
        if self.tenant is not None:          # also true on the owner's linked WhatsApp
            return self.tenant.get("telegram_user_id") == owner
        return self.channel == "telegram" and self.user_id == owner


def tz_buttons() -> list[list[tuple[str, str]]]:
    pairs = [(label, f"tz:{zone}") for label, zone in TIMEZONE_CHOICES]
    return [pairs[i:i + 2] for i in range(0, len(pairs), 2)]


def match_timezone(text: str) -> str | None:
    t = text.strip().replace(" ", "_")
    if not t:
        return None
    for zone in zoneinfo.available_timezones():
        if zone.lower() == t.lower():
            return zone
    return None


def finish_onboarding(ctx: Ctx, zone: str) -> None:
    assert ctx.tenant is not None
    with ctx.store.tx(ctx.tenant["id"]) as tx:
        tx.set_timezone(ctx.tenant["id"], zone, actor="commands")
        tx.set_tenant_status(ctx.tenant["id"], "active", actor="commands")
    ctx.reply(f"Timezone set to {zone}.\n\n{WELCOME}")


def stranger(ctx: Ctx) -> str:
    """Reply to someone without an account, at most once a day."""
    key = f"stranger:{ctx.user_id}" if ctx.channel == "telegram" else f"stranger:{ctx.channel}:{ctx.user_id}"
    with ctx.store.tx(system=True) as tx:
        last = tx.get_state(key)
        now = dt.datetime.now(dt.timezone.utc)
        if last and now - dt.datetime.fromisoformat(last) < dt.timedelta(hours=STRANGER_REPLY_HOURS):
            return "dropped:stranger-quiet"
        tx.set_state(key, now.isoformat())
    ctx.reply(INVITE_ONLY)
    return "replied:stranger"


def handle(ctx: Ctx, text: str) -> str:
    """Handle a message starting with '/'. Returns an outcome label."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()          # "/start@MyBot" -> "/start"
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        return start(ctx, arg)
    if ctx.tenant is None:
        return stranger(ctx)
    if ctx.tenant["status"] == "invited":
        ctx.reply(TZ_PROMPT, buttons=tz_buttons())
        return "prompted:timezone"
    if ctx.tenant["status"] != "active":
        return "dropped:inactive"
    if cmd == "/help":
        ctx.reply(HELP)
        return "help"
    if cmd == "/invite":
        return invite(ctx)
    if cmd == "/brief":
        return brief(ctx, arg)
    if cmd == "/web":
        return web_link(ctx)
    if cmd == "/link":
        return link(ctx)
    if cmd == "/export":
        return export(ctx)
    if cmd == "/delete":
        return delete(ctx, arg)
    ctx.reply(f"I don't know {cmd}. Send /help for the commands.")
    return "unknown-command"


def start(ctx: Ctx, code: str) -> str:
    if code and ctx.tenant is None:
        linked = use_link_code(ctx, code)
        if linked:
            return linked
    if ctx.tenant is not None:
        if ctx.tenant["status"] == "invited":
            ctx.reply(TZ_PROMPT, buttons=tz_buttons())
            return "prompted:timezone"
        ctx.reply("You're already set up.\n\n" + HELP)
        return "already-active"
    if ctx.is_owner and ctx.channel == "telegram":
        with ctx.store.tx(system=True) as tx:
            tx.create_tenant(telegram_user_id=ctx.user_id, telegram_chat_id=ctx.chat_id,
                             display_name=ctx.name, timezone=ctx.settings.owner_timezone,
                             actor="commands")
        ctx.reply(WELCOME)
        return "created:owner"
    if not code:
        return stranger(ctx)
    with ctx.store.tx(system=True) as tx:
        if not tx.invite_is_open(code):
            ctx.reply("That invite code isn't valid or has already been used.")
            return "rejected:bad-code"
        if ctx.channel == "whatsapp":
            tenant = tx.create_tenant(telegram_user_id=None, telegram_chat_id=None,
                                      whatsapp_phone=str(ctx.user_id), display_name=ctx.name,
                                      status="invited", actor="commands")
        else:
            tenant = tx.create_tenant(telegram_user_id=ctx.user_id, telegram_chat_id=ctx.chat_id,
                                      display_name=ctx.name, status="invited", actor="commands")
        tx.use_invite(code, tenant["id"])
    log.info("new user %s joined with an invite on %s", tenant["id"], ctx.channel)
    ctx.tenant = tenant
    ctx.reply(TZ_PROMPT, buttons=tz_buttons())
    return "created:invited"


def invite(ctx: Ctx) -> str:
    if not ctx.is_owner:
        ctx.reply("Only the owner can create invites for now.")
        return "rejected:not-owner"
    assert ctx.tenant is not None
    code = secrets.token_urlsafe(6)
    with ctx.store.tx(ctx.tenant["id"]) as tx:
        tx.create_invite(ctx.tenant["id"], code)
    ways = []
    tg_link = telegram_link(ctx)
    if tg_link:
        ways.append(f"On Telegram: open {tg_link}?start={code}")
    if ctx.settings.whatsapp_on and ctx.settings.whatsapp_number:
        ways.append(f"On WhatsApp: open {whatsapp_link(ctx.settings, f'/start {code}')}")
    ways.append(f"Or send this to the bot:\n/start {code}")
    ctx.reply(f"Invite code (one use): {code}\n\nSend them this:\n" + "\n".join(ways), preview=False)
    return "invite-created"


def telegram_link(ctx: Ctx) -> str | None:
    try:
        bot = ctx.tg if ctx.channel == "telegram" else ctx.telegram
        name = bot.bot_username() if bot is not None else ""
    except Exception:
        return None
    return f"https://t.me/{name}" if name else None


def whatsapp_link(settings: Settings, text: str) -> str:
    from urllib.parse import quote
    return f"https://wa.me/{settings.whatsapp_number}?text={quote(text)}"


def link(ctx: Ctx) -> str:
    """/link: a short-lived code that connects the other channel to this account."""
    assert ctx.tenant is not None
    other = "WhatsApp" if ctx.channel == "telegram" else "Telegram"
    if other == "WhatsApp" and not (ctx.settings.whatsapp_on and ctx.settings.whatsapp_number):
        ctx.reply("WhatsApp isn't switched on yet.")
        return "link:off"
    if other == "WhatsApp" and ctx.tenant.get("whatsapp_phone"):
        ctx.reply("WhatsApp is already connected to this account.")
        return "link:already"
    if other == "Telegram" and ctx.tenant.get("telegram_user_id"):
        ctx.reply("Telegram is already connected to this account.")
        return "link:already"
    code = secrets.token_hex(4).upper()
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=LINK_CODE_MINUTES)
    with ctx.store.tx(system=True) as tx:
        tx.set_state(f"link:{code}", {"tenant_id": str(ctx.tenant["id"]), "expires": expires.isoformat()})
    if other == "WhatsApp":
        how = f"Open {whatsapp_link(ctx.settings, f'/start {code}')} and send the message it fills in."
    else:
        tg = telegram_link(ctx)
        how = (f"Open {tg}?start={code} and tap Start." if tg else "") + f"\nOr send the bot: /start {code}"
    msg = (f"To use {other} with this same account (works once, for {LINK_CODE_MINUTES} minutes):\n"
           f"{{}}\n\nDon't share it: it connects that {other} account to all your records.")
    ctx.reply(msg.format(how.strip()), preview=False, log_text=msg.format("[link hidden]"))
    return "link:code-sent"


def use_link_code(ctx: Ctx, code: str) -> str | None:
    """/start <code> from /link: attach this channel to the existing account."""
    key = f"link:{code.strip().upper()}"
    with ctx.store.tx(system=True) as tx:
        pending = tx.get_state(key)
        if not pending:
            return None
        tx.set_state(key, None)                      # single use, even if expired
        if dt.datetime.now(dt.timezone.utc) > dt.datetime.fromisoformat(pending["expires"]):
            expired = True
        else:
            expired = False
            tenant = tx.tenant(pending["tenant_id"])
            if tenant is None:
                return None
            if ctx.channel == "whatsapp":
                tx.link_whatsapp(tenant["id"], str(ctx.user_id))
            else:
                tx.link_telegram(tenant["id"], ctx.user_id, ctx.chat_id)
            tenant = tx.tenant(tenant["id"])
    if expired:
        ctx.reply("That code has expired. Send /link again where you already use me.")
        return "link:expired"
    ctx.tenant = tenant
    name = "WhatsApp" if ctx.channel == "whatsapp" else "Telegram"
    ctx.reply(f"Connected. {name} now uses your existing account: same records, same reminders. "
              "Reminders and the morning brief go to wherever you last wrote to me.\n\nSend /help for the commands.")
    return "linked"


def brief(ctx: Ctx, arg: str) -> str:
    """/brief [time|off]: the daily morning brief, in the user's own timezone."""
    assert ctx.tenant is not None
    tid = ctx.tenant["id"]
    with ctx.store.tx(tid) as tx:
        current = tx.tenant(tid)
    if not arg:
        if current and current.get("brief_time"):
            ctx.reply(f"Your morning brief comes at {briefs.fmt_time(current['brief_time'])} "
                      f"({current['timezone']}). Change it with /brief 8:30, stop it with /brief off.")
        else:
            ctx.reply("The morning brief is off. Turn it on with a time, for example /brief 9am: "
                      "every day at that time I'll send what's overdue, due today and due this week.")
        return "brief:status"
    if arg.lower() in ("off", "stop", "no", "none"):
        with ctx.store.tx(tid) as tx:
            tx.set_brief(tid, None)
        ctx.reply("Morning brief turned off. /brief 9am turns it back on.")
        return "brief:off"
    at = briefs.parse_time(arg)
    if at is None:
        ctx.reply("I didn't understand that time. Try /brief 9am, /brief 8:30 or /brief 19:00.")
        return "brief:bad-time"
    now = briefs.local_now(current["timezone"] if current else "UTC")
    # Already past that time today: start tomorrow rather than sending one right now.
    skip = now.date() if now.time() >= at else None
    with ctx.store.tx(tid) as tx:
        tx.set_brief(tid, at, skip_until=skip)
    when = "tomorrow" if skip else "today"
    ctx.reply(f"Done. Every day at {briefs.fmt_time(at)} ({current['timezone'] if current else 'UTC'}) "
              f"I'll send what's overdue, due today and due this week. First one {when}. "
              "/brief off stops it.")
    return "brief:on"


def web_link(ctx: Ctx) -> str:
    """A single-use sign-in link for the web view."""
    assert ctx.tenant is not None
    base = ctx.settings.web_base_url
    if not base:
        ctx.reply("The web view isn't switched on yet.")
        return "web:off"
    token = secrets.token_urlsafe(32)
    with ctx.store.tx(ctx.tenant["id"]) as tx:
        tx.create_login_token(ctx.tenant["id"], hashlib.sha256(token.encode()).hexdigest(),
                              LOGIN_LINK_MINUTES)
    msg = (f"Your sign-in link (works once, for {LOGIN_LINK_MINUTES} minutes):\n{base}/login/{{}}\n\n"
           "Don't forward it: anyone who opens it first gets into your account.")
    ctx.reply(msg.format(token), preview=False, log_text=msg.format("[hidden]"))
    return "web:link-sent"


def export(ctx: Ctx) -> str:
    assert ctx.tenant is not None
    if ctx.channel == "whatsapp":
        ctx.reply("WhatsApp can't carry zip files. Send /web for a sign-in link, then tap Export "
                  "at the bottom of the page.")
        return "export:use-web"
    with ctx.store.tx(ctx.tenant["id"]) as tx:
        filename, data = build_export(tx, load_schema(ctx.tenant["schema_name"]), ctx.tenant)
        tx.audit(ctx.tenant["id"], actor="commands", action="send", target_table="none",
                 target_id=None, summary=f"export sent ({len(data)} bytes)")
    if len(data) > 45 * 1024 * 1024:
        ctx.reply("Your export is larger than Telegram allows (50 MB). Ask the owner for it directly.")
        return "export-too-large"
    ctx.tg.send_document(ctx.chat_id, filename, data,
                         caption="Everything you've sent and everything filed for you. README.txt explains the files.")
    ctx.note_sent(f"[file: {filename}, {len(data) // 1024} KB]")
    return "exported"


def delete(ctx: Ctx, arg: str) -> str:
    assert ctx.tenant is not None
    if ctx.is_owner:
        ctx.reply("The owner account can't be deleted from chat.")
        return "rejected:owner"
    key = f"delete:{ctx.tenant['id']}"
    now = dt.datetime.now(dt.timezone.utc)
    with ctx.store.tx(system=True) as tx:
        pending = tx.get_state(key)
    if not arg:
        code = secrets.token_hex(2).upper()
        with ctx.store.tx(system=True) as tx:
            tx.set_state(key, {"code": code, "expires": (now + dt.timedelta(minutes=DELETE_CONFIRM_MINUTES)).isoformat()})
        first = ("I'll send you a final export first." if ctx.channel == "telegram" else
                 "I can't send the export on WhatsApp: if you want a copy, get it first from /web (Export).")
        msg = ("This permanently erases your account: every message, record and log. It can't be undone.\n\n"
               f"{first} To confirm, within {DELETE_CONFIRM_MINUTES} minutes send:\n"
               "/delete {}")
        ctx.reply(msg.format(code), log_text=msg.format("[code hidden]"))
        return "delete:confirm-requested"
    if not pending or arg.strip().upper() != pending["code"] or now > dt.datetime.fromisoformat(pending["expires"]):
        ctx.reply("That confirmation code doesn't match or has expired. Send /delete to start again.")
        return "delete:bad-code"
    if ctx.channel == "telegram":
        export(ctx)
    with ctx.store.tx(system=True) as tx:
        counts = tx.delete_tenant(ctx.tenant["id"])
        tx.set_state(key, None)
    ctx.inbox_id = None  # everything is gone; nothing left to log against
    log.info("deleted tenant %s: %s", ctx.tenant["id"], counts)
    ctx.reply("Done. Your account and all your data have been erased."
              + (" The zip above is your only copy." if ctx.channel == "telegram" else ""))
    return "deleted"
