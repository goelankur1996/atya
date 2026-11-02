# Read-only health check, run on the server:
#   deploy/run.sh "$(cat deploy/diagnose.sh)"
# Does not call getUpdates (that would steal messages from the running app).
cd /opt/atya
echo "== containers"; docker compose ps --format '{{.Service}} {{.Status}}'
echo "== telegram (bot + webhook)"
docker compose exec -T app python - <<'PY'
import os, requests
t = os.environ["TELEGRAM_BOT_TOKEN"].strip()
me = requests.get(f"https://api.telegram.org/bot{t}/getMe", timeout=15).json()
print("bot:", me.get("result", {}).get("username"), "ok" if me.get("ok") else me)
w = requests.get(f"https://api.telegram.org/bot{t}/getWebhookInfo", timeout=15).json().get("result", {})
print("webhook url:", repr(w.get("url")), "| pending updates:", w.get("pending_update_count"),
      "| last error:", w.get("last_error_message"))
print("owner id length:", len(os.environ.get("OWNER_TELEGRAM_USER_ID", "").strip()))
PY
echo "== database"
docker compose exec -T db psql -U atya -d atya -At <<'SQL'
SELECT 'state: ' || key || ' = ' || value::text FROM service_state WHERE key NOT LIKE 'stranger:%';
SELECT 'strangers seen: ' || count(*) FROM service_state WHERE key LIKE 'stranger:%';
SELECT 'tenant: status=' || status || ' tg_id_len=' || length(telegram_user_id::text) FROM tenants;
SELECT 'inbox last: ' || max(captured_at)::text || ' | pending=' || count(*) FILTER (WHERE status = 'pending') FROM inbox;
SELECT 'conn: ' || coalesce(usename::text, '-') || ' ' || coalesce(state, '-') || ' wait=' || coalesce(wait_event_type || '/' || wait_event, '-')
       || ' age=' || coalesce(date_trunc('second', now() - xact_start)::text, '-') || ' q=' || left(regexp_replace(query, '\s+', ' ', 'g'), 80)
  FROM pg_stat_activity WHERE datname = 'atya' AND pid <> pg_backend_pid();
SQL
echo "== app log (last 30)"; docker compose logs --tail=30 app
