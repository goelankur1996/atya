"""Google: sign-in (OAuth), and read-only calls to Calendar and Contacts.

Scopes are read-only: calendar events, and contacts (for birthdays). Tokens
are stored encrypted (CONNECTIONS_KEY) in the connections table.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any, Iterator

import requests
from cryptography.fernet import Fernet, InvalidToken

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR = "https://www.googleapis.com/calendar/v3"
PEOPLE = "https://people.googleapis.com/v1"
SCOPES = ("openid email https://www.googleapis.com/auth/calendar.readonly "
          "https://www.googleapis.com/auth/contacts.readonly")
MAX_PAGES = 5


class GoogleError(Exception):
    def __init__(self, message: str, *, revoked: bool = False) -> None:
        super().__init__(message)
        self.revoked = revoked          # the user withdrew access (or it expired): reconnect needed


class Vault:
    """Encrypts tokens at rest."""

    def __init__(self, key: str) -> None:
        self.f = Fernet(key.encode() if isinstance(key, str) else key)

    def seal(self, text: str) -> bytes:
        return self.f.encrypt(text.encode())

    def open(self, blob: bytes | memoryview | None) -> str | None:
        if blob is None:
            return None
        try:
            return self.f.decrypt(bytes(blob)).decode()
        except InvalidToken as e:
            raise GoogleError("stored token can't be decrypted (CONNECTIONS_KEY changed?)", revoked=True) from e


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, http: Any = None) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or requests.Session()

    def auth_url(self, *, state: str, redirect_uri: str) -> str:
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPES, "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state})

    def _token(self, data: dict) -> dict:
        r = self.http.post(TOKEN_URL, data={**data, "client_id": self.client_id,
                                             "client_secret": self.client_secret}, timeout=30)
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            err = body.get("error", "")
            raise GoogleError(f"token: {err} {body.get('error_description', '')}".strip(),
                              revoked=err in ("invalid_grant", "unauthorized_client"))
        return body

    def exchange(self, *, code: str, redirect_uri: str) -> dict:
        return self._token({"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"refresh_token": refresh_token, "grant_type": "refresh_token"})

    def revoke(self, token: str) -> None:
        try:
            self.http.post(REVOKE_URL, data={"token": token}, timeout=15)
        except requests.RequestException:
            pass                        # best effort; the user can also remove access in their Google account

    def email(self, access_token: str) -> str | None:
        r = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        return r.json().get("email") if r.ok else None


class GoogleAPI:
    """Read-only Calendar and People calls for one account."""

    def __init__(self, access_token: str, http: Any = None) -> None:
        self.http = http or requests.Session()
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self.http.get(url, params=params, headers=self.headers, timeout=30)
        if r.status_code == 401:
            raise GoogleError("access token rejected")
        if r.status_code >= 400:
            msg = (r.json().get("error", {}) or {}).get("message", "") if r.content else ""
            raise GoogleError(f"{r.status_code} {msg}".strip(), revoked=r.status_code == 403 and "scope" in msg.lower())
        return r.json()

    def _pages(self, url: str, params: dict, key: str) -> Iterator[dict]:
        token = None
        for _ in range(MAX_PAGES):
            body = self._get(url, {**params, **({"pageToken": token} if token else {})})
            yield from body.get(key, []) or []
            token = body.get("nextPageToken")
            if not token:
                return

    def calendars(self) -> list[dict]:
        cals = list(self._pages(f"{CALENDAR}/users/me/calendarList", {"minAccessRole": "reader"}, "items"))
        return [c for c in cals if c.get("primary") or c.get("selected")][:10]

    def events(self, calendar_id: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
        return list(self._pages(f"{CALENDAR}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events", {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "showDeleted": "true", "maxResults": 250}, "items"))

    def birthdays(self) -> list[dict]:
        """[{"name", "month", "day", "year" (or None), "id"}] for contacts with a birthday."""
        out = []
        for p in self._pages(f"{PEOPLE}/people/me/connections",
                             {"personFields": "names,birthdays", "pageSize": 1000}, "connections"):
            names = p.get("names") or []
            name = (names[0].get("displayName") or "").strip() if names else ""
            for b in p.get("birthdays") or []:
                d = b.get("date") or {}
                if name and d.get("month") and d.get("day"):
                    out.append({"name": name, "month": d["month"], "day": d["day"], "year": d.get("year"),
                                "id": p.get("resourceName", name)})
                    break
        return out
