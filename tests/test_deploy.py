"""The first-boot script embeds copies of deploy/server/*; they must not drift."""

from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_user_data_embeds_server_files():
    user_data = (DEPLOY / "user-data.sh").read_text()
    for name in ("docker-compose.yml", "Caddyfile", "update.sh"):
        body = (DEPLOY / "server" / name).read_text()
        assert f"cat > {name} <<'EOF'\n{body}EOF\n" in user_data, f"user-data.sh copy of {name} is out of date"
