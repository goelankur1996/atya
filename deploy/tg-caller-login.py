#!/usr/bin/env python3
"""Log the second Telegram account (the one that calls you) in once, on your Mac.

Reads the API id and hash from SSM (/atya/TG_CALLER_API_ID, /atya/TG_CALLER_API_HASH),
asks for the phone number, the login code Telegram sends to that account's app and,
if the account has one, its two-step verification password. The resulting session is
written straight to SSM as /atya/TG_CALLER_SESSION (SecureString). It is never printed.

    python3 -m venv /tmp/tgl && /tmp/tgl/bin/pip install -q telethon==1.45.0
    /tmp/tgl/bin/python deploy/tg-caller-login.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

REGION = os.environ.get("AWS_REGION", "us-east-1")


def ssm_get(name: str) -> str:
    out = subprocess.run(["aws", "ssm", "get-parameter", "--region", REGION, "--name", name,
                          "--with-decryption", "--query", "Parameter.Value", "--output", "text"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"Couldn't read {name} from SSM: {out.stderr.strip()}")
    return out.stdout.strip()


def ssm_put_secret(name: str, value: str) -> None:
    # Through a private temp file, so the value never appears in the process list.
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"Name": name, "Value": value, "Type": "SecureString", "Overwrite": True}, f)
        out = subprocess.run(["aws", "ssm", "put-parameter", "--region", REGION,
                              "--cli-input-json", f"file://{path}"], capture_output=True, text=True)
    finally:
        os.remove(path)
    if out.returncode != 0:
        sys.exit(f"Couldn't save {name} to SSM: {out.stderr.strip()}")


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(ssm_get("/atya/TG_CALLER_API_ID"))
    api_hash = ssm_get("/atya/TG_CALLER_API_HASH")
    client = TelegramClient(StringSession(), api_id, api_hash,
                            device_model="ATYA server", system_version="Linux", app_version="1.0")
    print("Log in with the SECOND account (the one that will call you).")
    await client.start(phone=lambda: input("Phone number of the second account (with +country code): "),
                       code_callback=lambda: input("Login code (sent to that account's Telegram app): "))
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    ssm_put_secret("/atya/TG_CALLER_SESSION", session)
    print(f"Logged in as {me.first_name or ''} (id {me.id}). Session saved to SSM as /atya/TG_CALLER_SESSION.")
    print("Next: deploy/deploy.sh, then the ring test.")


if __name__ == "__main__":
    asyncio.run(main())
