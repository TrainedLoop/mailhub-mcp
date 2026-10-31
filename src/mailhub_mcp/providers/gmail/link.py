"""Link a Gmail mailbox from the admin page: consent URL, code exchange, revocation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from mailhub_mcp.providers.base import LinkError, LinkResult
from mailhub_mcp.providers.gmail.provider import GMAIL_SCOPE

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_FAILED = "Google linking failed; try again"


class GmailLinker:
    label: ClassVar[str] = "Gmail"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        post: Any = requests.post,
        verify: Any = google_id_token.verify_oauth2_token,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._post = post
        self._verify = verify

    def authorization_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": f"{GMAIL_SCOPE} openid email",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def complete(self, code: str, redirect_uri: str, code_verifier: str) -> LinkResult:
        try:
            resp = self._post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code_verifier": code_verifier,
                },
                timeout=10,
            )
        except requests.RequestException:
            raise LinkError(_FAILED) from None
        if resp.status_code != 200:
            raise LinkError(_FAILED)
        try:
            body = resp.json()
        except ValueError:
            raise LinkError(_FAILED) from None
        if not isinstance(body, dict):
            raise LinkError(_FAILED)
        raw_id = body.get("id_token")
        if not raw_id or not isinstance(raw_id, str):
            raise LinkError(_FAILED)
        refresh = body.get("refresh_token")
        if not refresh or not isinstance(refresh, str):
            raise LinkError(
                "Google did not return offline access; remove mailhub at "
                "myaccount.google.com/permissions and link again"
            )
        scope = body.get("scope")
        if not isinstance(scope, str) or GMAIL_SCOPE not in scope.split():
            raise LinkError("Gmail access was not granted; tick the Gmail permission on Google's consent screen")
        try:
            claims = self._verify(
                raw_id, google_requests.Request(), self._client_id, clock_skew_in_seconds=10
            )
        except (ValueError, GoogleAuthError):
            raise LinkError(_FAILED) from None
        verified = claims.get("email_verified")
        if not (verified is True or verified == "true"):
            raise LinkError(_FAILED)
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise LinkError(_FAILED)
        credential = {
            "type": "authorized_user",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh,
            "token": body.get("access_token"),
            "token_uri": TOKEN_URL,
            "scopes": [GMAIL_SCOPE],
        }
        return LinkResult(json.dumps(credential), email.lower())

    def revoke(self, credential_path: Path) -> bool:
        try:
            data = json.loads(Path(credential_path).read_text())
        except (OSError, ValueError):
            return False
        token = data.get("refresh_token") if isinstance(data, dict) else None
        if not token or not isinstance(token, str):
            return False
        try:
            resp = self._post(REVOKE_URL, data={"token": token}, timeout=10)
        except requests.RequestException:
            return False
        return resp.status_code == 200
