"""Circle: inviting someone (new or already on ATYA), agreeing both ways, check-in calls queued
from checkin records, the call's prompt and language, and the report back to whoever asked."""
import datetime as dt

import pytest

from atya import circle, commands, wakecall
from atya.config import Settings
from atya.schema import load_schema
from tests.test_pipeline import FakeLLM, FakeTelegram
from tests.test_wakecall import OWNER, Phone, friend, ist, outbox, owner  # noqa: F401  (fixture)

S = load_schema("default")
DAD, NEW = 777_000_601, 777_000_602


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    tg_caller_api_id="1", tg_caller_api_hash="h", tg_caller_session="s")


@pytest.fixture(autouse=True)
def clean(store):
    def wipe():
        with store.tx(system=True) as tx:
            for uid in (DAD, NEW):
                t = tx.tenant_by_telegram(uid)
                if t:
                    tx.delete_tenant(t["id"])
            tx.conn.execute("DELETE FROM circle_links")
            tx.conn.execute("DELETE FROM circle_invites")
    wipe()
    yield
    wipe()


def ctx_for(store, settings, tenant, uid, name="X"):
    tg = FakeTelegram()
    return commands.Ctx(store=store, tg=tg, settings=settings, tenant=tenant, channel="telegram",
                        user_id=uid, chat_id=uid, name=name), tg


def bodies(store, tid):
    return [r["body"] for r in outbox_rows(store, tid)]


def outbox_rows(store, tid):
    with store.tx(tid) as tx:
        return tx._all("SELECT * FROM outbox WHERE tenant_id = %s ORDER BY created_at", (tid,))


def invite(store, settings, owner, name="Dad", lang="hinglish"):
    ctx, tg = ctx_for(store, settings, owner, OWNER, "Ankur")
    assert circle.command(ctx, f"add {name} {lang}") == "circle:invite"
    with store.tx(system=True) as tx:
        return tx._one("SELECT * FROM circle_invites WHERE from_tenant = %s ORDER BY created_at DESC LIMIT 1",
                       (owner["id"],))


def tap_last(store, tenant, label):
    row = [r for r in outbox_rows(store, tenant["id"]) if r["buttons"]][-1]
    data = next(b[1] for b in row["buttons"][0] if b[0] == label)
    with store.tx(system=True) as tx:
        tenant = tx.tenant(tenant["id"])
    return circle.tap(store, tenant, data)


def test_existing_user_agrees_both_ways(store, settings, owner):
    dad = friend(store, DAD, "Rajesh Goel", "rajesh")
    inv = invite(store, settings, owner)
    ctx, _ = ctx_for(store, settings, dad, DAD, "Rajesh")
    assert commands.start(ctx, inv["code"].lower()) == "circle:asked"
    ask = bodies(store, dad["id"])[-1]
    assert ask.startswith("Ankur chahte hain") and "call ya message" in ask and "Allow karein?" in ask              # in his language
    assert tap_last(store, dad, "Haan, allow") == "circle:allowed"
    assert "Rajesh" not in bodies(store, owner["id"])[-2] and "Dad said yes" in bodies(store, owner["id"])[-2]
    assert tap_last(store, owner, "Yes") == "circle:back-allowed"
    with store.tx(system=True) as tx:
        assert circle.find_link(tx, owner["id"], "dad")["callee"] == dad["id"]
        assert circle.find_link(tx, dad["id"], "Ankur")["callee"] == owner["id"]
        assert tx.tenant(dad["id"])["call_language"] == "hinglish"
    # The invite works once; /circle lists both directions; remove ends both.
    assert commands.start(ctx, inv["code"]) == "circle:bad-code"
    octx, tg = ctx_for(store, settings, owner, OWNER)
    circle.command(octx, "")
    assert "You can ask ATYA to call: Dad." in tg.sent[-1][1] and "Can ask ATYA to call you: Rajesh." in tg.sent[-1][1]
    assert circle.command(octx, "remove dad") == "circle:removed"
    with store.tx(system=True) as tx:
        assert circle.find_link(tx, owner["id"], "dad") is None and circle.find_link(tx, dad["id"], "ankur") is None


def test_someone_new_joins_from_the_link(store, settings, owner):
    inv = invite(store, settings, owner, "Meera", "english")
    ctx, tg = ctx_for(store, settings, None, NEW, "Meera")
    assert commands.start(ctx, inv["code"]) == "circle:joined-new"          # no waitlist
    assert tg.sent[-1][1] == commands.TZ_PROMPT
    commands.finish_onboarding(ctx, "Asia/Kolkata")                          # then the circle question
    assert bodies(store, ctx.tenant["id"])[-1].startswith("Ankur would like ATYA to call or message you")


def make_checkin(store, owner, **fields):
    with store.tx(owner["id"]) as tx:
        rec, _ = tx.create_record(owner["id"], S, "checkin", title="Dad: situps", enforce_verbatim=False, actor="t",
                                  fields={"Person": "Dad", "Purpose": "did he do his situps; motivate him if not",
                                          "Via": "call", "Time": "17:30", "Repeat": "daily", "Status": "active",
                                          **fields})
    return rec


def linked_dad(store, settings, owner):
    dad = friend(store, DAD, "Rajesh Goel", "rajesh")
    inv = invite(store, settings, owner)
    ctx, _ = ctx_for(store, settings, dad, DAD)
    commands.start(ctx, inv["code"])
    tap_last(store, dad, "Haan, allow")
    with store.tx(system=True) as tx:
        tx.conn.execute("DELETE FROM calls")
        return tx.tenant(dad["id"])


class ReportLLM(FakeLLM):
    def call_report(self, *, transcript, purpose, name):
        self.report_args = (transcript, purpose, name)
        return "Not done yet; he'll do them after the guests leave."


def test_daily_checkin_calls_dad_and_reports_back(store, settings, owner):
    dad = linked_dad(store, settings, owner)
    make_checkin(store, owner)
    assert wakecall.tick(store, settings, Phone("declined"), now=ist(17, 0)) is None          # not yet
    seen = []

    async def answered(tenant, call):
        seen.append((tenant["id"], call["kind"], call["about"]))
        tid = wakecall.save_transcript(store, tenant, [("ASSISTANT", "Namaste Rajesh ji"), ("USER", "abhi nahi kiya")],
                                       dt.datetime.now(dt.timezone.utc))
        with store.tx(system=True) as tx:
            tx.set_call_transcript(call["id"], tid)
        return "answered"
    llm = ReportLLM()
    assert wakecall.tick(store, settings, answered, now=ist(17, 31), llm=llm) == "checkin:answered:answered"
    assert seen == [(dad["id"], "checkin", "did he do his situps; motivate him if not")]
    assert "abhi nahi kiya" in llm.report_args[0] and llm.report_args[2] == "Dad"
    assert bodies(store, owner["id"])[-1] == ("📞 Dad (did he do his situps; motivate him if not):\n"
                                              "Not done yet; he'll do them after the guests leave.")
    assert wakecall.tick(store, settings, answered, now=ist(17, 40), llm=llm) is None         # once a day


def test_unanswered_checkin_retries_then_tells_you(store, settings, owner):
    linked_dad(store, settings, owner)
    make_checkin(store, owner, Repeat="once", Date="2026-11-16")
    phone = Phone("missed", "missed", "missed")
    assert wakecall.tick(store, settings, phone, now=ist(17, 30)) == "checkin:missed:retry"
    assert wakecall.tick(store, settings, phone, now=ist(17, 41)) == "checkin:missed:retry"
    assert wakecall.tick(store, settings, phone, now=ist(17, 52)) == "checkin:missed:missed"
    assert bodies(store, owner["id"])[-1] == "📞 I couldn't reach Dad after 3 tries (did he do his situps; " \
                                             "motivate him if not)."
    with store.tx(owner["id"]) as tx:                                    # a one-off is done once queued
        (rec,) = tx._all("SELECT * FROM records WHERE tenant_id = %s AND type = 'checkin'", (owner["id"],))
    assert rec["fields"]["Status"] == "done"


def test_checkin_for_someone_not_in_the_circle_is_paused(store, settings, owner):
    rec = make_checkin(store, owner, Person="Uncle")
    assert wakecall.tick(store, settings, Phone(), now=ist(17, 30)) is None
    assert "not in your circle" in bodies(store, owner["id"])[-1]
    with store.tx(owner["id"]) as tx:
        assert tx.record(owner["id"], rec["id"])["fields"]["Status"] == "paused"


def test_checkin_prompt_is_in_their_language():
    dad = {"display_name": "Rajesh Goel", "call_language": "hinglish"}
    call = {"kind": "checkin", "about": "situps", "requester_name": "Ankur"}
    p = wakecall.build_prompt(dad, call, dt.datetime(2026, 11, 17, 17, 30), "B", "arjun", None)
    assert "Ankur asked ATYA to check in with them" in p and "Speak Hinglish" in p and "Ek second." in p
    s = Settings(database_url="x", telegram_bot_token="x", anthropic_api_key="x", owner_telegram_user_id=1)
    assert wakecall.voice_for(s, dad) == "arjun" and wakecall.voice_for(s, {"call_language": "english"}) == "matthew"


class RelayLLM(FakeLLM):
    def relay_question(self, *, purpose, requester, name, language):
        return f"{requester} pooch rahe hain: ghar aate hue kaun si ice cream laayein?"

    def is_answer(self, *, question, message):
        return "ice cream" in message or "butterscotch" in message


def test_ask_by_message_and_the_reply_goes_back(store, settings, owner):
    from atya import processor
    dad = linked_dad(store, settings, owner)
    with store.tx(owner["id"]) as tx:                                     # what filing makes of "ask dad …"
        tx.create_record(owner["id"], S, "checkin", title="Dad: ice cream", enforce_verbatim=False, actor="t",
                         fields={"Person": "Dad", "Purpose": "which ice cream should I bring on the way back",
                                 "Via": "message", "Repeat": "once", "Status": "active"})
    llm = RelayLLM()
    now = dt.datetime.now(dt.timezone.utc)
    assert circle.queue_checkins(store, now, llm) == 1 and circle.queue_checkins(store, now, llm) == 0
    sent = bodies(store, dad["id"])[-1]
    assert sent.startswith("Ankur pooch rahe hain: ghar aate hue kaun si ice cream") and "reply karein" in sent
    with store.tx(dad["id"]) as tx:                                      # an unrelated message is his own
        tx.add_inbox(dad["id"], source="telegram", raw_content="remind me to pay the electricity bill")
        tx.add_inbox(dad["id"], source="telegram", raw_content="butterscotch, aur mummy ke liye kulfi")
    llm.plans["remind me to pay the electricity bill"] = {"writes": [{"type": "task", "title": "Pay electricity bill",
                                                                      "fields": {}}]}
    while processor.process_once(store, llm, settings):
        pass
    assert bodies(store, owner["id"])[-1] == ("💬 Dad (which ice cream should I bring on the way back):\n"
                                              "butterscotch, aur mummy ke liye kulfi")
    assert bodies(store, dad["id"])[-1] == "Ankur ko bata diya."
    with store.tx(dad["id"]) as tx:
        assert tx._all("SELECT * FROM records WHERE tenant_id = %s AND title = 'Pay electricity bill'", (dad["id"],))


def test_unanswered_question_is_reported(store, settings, owner):
    dad = linked_dad(store, settings, owner)
    with store.tx(owner["id"]) as tx:
        tx.create_record(owner["id"], S, "checkin", title="Dad: protein", enforce_verbatim=False, actor="t",
                         fields={"Person": "Dad", "Purpose": "did he eat eggs today", "Via": "message",
                                 "Repeat": "once", "Status": "active", "Wait Hours": 2})
    now = dt.datetime.now(dt.timezone.utc)
    assert circle.queue_checkins(store, now) == 1                          # no model: sent plainly
    assert bodies(store, dad["id"])[-1].startswith("Ankur pooch rahe hain: did he eat eggs today")
    assert circle.expire_asks(store, now + dt.timedelta(hours=1)) == 0
    assert circle.expire_asks(store, now + dt.timedelta(hours=2, minutes=1)) == 1
    assert bodies(store, owner["id"])[-1] == "💬 Dad hasn't replied (did he eat eggs today) after 2 hours."


def test_names_come_from_the_profile_and_fix_links(store, settings, owner):
    dad = linked_dad(store, settings, owner)
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET display_name = NULL WHERE id = %s", (dad["id"],))
        tx.conn.execute("UPDATE circle_links SET name = 'them' WHERE caller = %s", (dad["id"],))
        dad = tx.tenant(dad["id"])
    assert circle.fill_name(store, dad, "💎")["display_name"] is None                  # not a name
    assert circle.fill_name(store, dad, "Rajesh Goel")["display_name"] == "Rajesh Goel"
    ctx, _ = ctx_for(store, settings, {**owner, "display_name": None}, OWNER)
    assert circle.command(ctx, "add Mom") == "circle:no-own-name"                       # who's asking?
    assert commands.name_cmd(ctx, "Ankur Goel") == "name:set"
    with store.tx(system=True) as tx:
        assert tx.tenant(owner["id"])["display_name"] == "Ankur Goel"
        assert circle.called(tx, dad["id"], tx.tenant(owner["id"])) == "Ankur"
