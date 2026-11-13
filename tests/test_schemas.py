"""The default schema is generic; the owner's schema adds his trackers and nothing else differs."""

from __future__ import annotations

from atya.__main__ import sync_owner_schema
from atya.config import Settings
from atya.schema import load_schema
from tests.test_pipeline import OWNER

PERSONAL = ("fitness", "robotics_milestone", "meal", "vla_milestone")


def test_default_has_no_personal_trackers():
    d = load_schema("default")
    assert not set(PERSONAL) & set(d.types)
    assert not any(t.child for t in d.types.values())
    text = (d.render_types() + d.rules_routing + d.rules_hard).lower()
    for word in ("pullup", "robotics", "vla", "meals"):
        assert word not in text


def test_owner_schema_is_default_plus_trackers():
    d, a = load_schema("default"), load_schema("ankur")
    assert set(a.types) == set(d.types) | set(PERSONAL)
    for k in d.types:
        assert a.types[k] == d.types[k], f"{k} differs between default.yaml and ankur.yaml"
    assert a.rules_routing == d.rules_routing and a.rules_hard == d.rules_hard


def test_owner_account_switches_to_own_schema(store, db_url):
    s = Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x", owner_telegram_user_id=OWNER)
    with store.tx(system=True) as tx:
        old = tx.tenant_by_telegram(OWNER)
        if old:
            tx.delete_tenant(old["id"])
        t = tx.create_tenant(telegram_user_id=OWNER, telegram_chat_id=555, timezone="UTC")
        f = tx.create_tenant(telegram_user_id=880009, telegram_chat_id=880009, timezone="UTC")
    try:
        sync_owner_schema(store, s)
        with store.tx(system=True) as tx:
            assert tx.tenant(t["id"])["schema_name"] == "ankur"
            assert tx.tenant(f["id"])["schema_name"] == "default"
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(f["id"])
