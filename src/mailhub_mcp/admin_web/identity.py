"""Owner identity for the admin page: Google OpenID Connect sign-in."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_FAILED = "Google sign-in failed"
_CLAIMS = json.dumps({"id_token": {"auth_time": {"essential": True}}}, separators=(",", ":"))


class LoginError(Exception):
    """Sign-in failed. The message is safe to show to the user; `reason` is a fixed code for the log
    ("token-exchange", "verify", "nonce", "unverified-email", "no-email"), never shown or user data."""

    def __init__(self, message: str, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class OwnerLogin:
    email: str
    auth_time: float | None


class OwnerIdentity(Protocol):
    def authorization_url(
        self, redirect_uri: str, state: str, nonce: str, code_challenge: str,
        *, max_age: int | None = None,
    ) -> str: ...

    def complete(
        self, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> OwnerLogin: ...


class GoogleIdentity:
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

    def authorization_url(
        self, redirect_uri: str, state: str, nonce: str, code_challenge: str,
        *, max_age: int | None = None,
    ) -> str:
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
            "claims": _CLAIMS,
        }
        if max_age is not None:
            params["max_age"] = str(max_age)  # OIDC: ask for a sign-in at most this old
        return f"{AUTH_URL}?{urlencode(params)}"

    def complete(
        self, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> OwnerLogin:
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
            raise LoginError(_FAILED, "token-exchange") from None
        if resp.status_code != 200:
            raise LoginError(_FAILED, "token-exchange")
        try:
            raw = resp.json().get("id_token")
        except (ValueError, AttributeError):
            raise LoginError(_FAILED, "token-exchange") from None
        if not raw or not isinstance(raw, str):
            raise LoginError(_FAILED, "token-exchange")
        try:
            # 10 s of clock skew, as in the Gmail linker: a host clock slightly behind Google's
            # would otherwise reject a token issued "in the future".
            claims = self._verify(raw, google_requests.Request(), self._client_id, clock_skew_in_seconds=10)
        except (ValueError, GoogleAuthError):
            raise LoginError(_FAILED, "verify") from None
        if claims.get("nonce") != nonce:
            raise LoginError(_FAILED, "nonce")
        verified = claims.get("email_verified")
        if not (verified is True or verified == "true"):
            raise LoginError(_FAILED, "unverified-email")
        email = claims.get("email")
        if not isinstance(email, str) or not email:
            raise LoginError(_FAILED, "no-email")
        auth_time = claims.get("auth_time")
        if isinstance(auth_time, bool) or not isinstance(auth_time, (int, float)):
            auth_time = None
        return OwnerLogin(email.lower(), float(auth_time) if auth_time is not None else None)
