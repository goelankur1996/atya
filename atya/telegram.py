"""Minimal Telegram Bot API client (long polling + sendMessage)."""

from __future__ import annotations

import logging
from typing import Protocol

import requests

log = logging.getLogger("telegram")

MAX_MESSAGE_CHARS = 4096


class TelegramError(Exception):
    pass


class TelegramAPI(Protocol):
    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]: ...
    def send_message(self, chat_id: int, text: str, reply_to: int | None = None,
                     buttons: list[list[tuple[str, str]]] | None = None) -> dict: ...
    def send_document(self, chat_id: int, filename: str, data: bytes, caption: str = "") -> dict: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def bot_username(self) -> str: ...


class Telegram:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = requests.Session()

    def _call(self, method: str, payload: dict, timeout: int) -> dict | list:
        try:
            r = self.http.post(f"{self.base}/{method}", json=payload, timeout=timeout)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(f"{method}: {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('error_code')} {body.get('description')}")
        return body["result"]

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout + 10)
        assert isinstance(result, list)
        return result

    def send_message(self, chat_id: int, text: str, reply_to: int | None = None,
                     buttons: list[list[tuple[str, str]]] | None = None) -> dict:
        """Send text, split into several messages if it exceeds Telegram's limit.

        buttons: rows of (label, callback_data), attached to the last part.
        """
        last: dict = {}
        parts = split_message(text)
        for i, part in enumerate(parts):
            payload: dict = {"chat_id": chat_id, "text": part}
            if reply_to and i == 0:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in row] for row in buttons]}
            result = self._call("sendMessage", payload, timeout=30)
            assert isinstance(result, dict)
            last = result
        return last

    def send_document(self, chat_id: int, filename: str, data: bytes, caption: str = "") -> dict:
        try:
            r = self.http.post(f"{self.base}/sendDocument", timeout=120,
                               data={"chat_id": str(chat_id), "caption": caption[:1000]},
                               files={"document": (filename, data, "application/zip")})
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(f"sendDocument: {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"sendDocument: {body.get('error_code')} {body.get('description')}")
        return body["result"]

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, timeout=15)

    def bot_username(self) -> str:
        if not getattr(self, "_username", None):
            me = self._call("getMe", {}, timeout=15)
            assert isinstance(me, dict)
            self._username = me.get("username", "")
        return self._username


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on paragraph, then line, then hard boundaries."""
    text = text or "(empty)"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts
