"""Google OAuth for the MCP endpoint, restricted to allowlisted emails."""

import logging

from fastmcp.server.auth.providers.google import GoogleProvider

from mailhub_mcp.config import Settings

log = logging.getLogger("mailhub-mcp")

# Callback URLs Claude uses when connecting a custom connector. Any other MCP
# client trying to register is refused.
CLAUDE_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]


class AllowlistGoogleProvider(GoogleProvider):
    """Google login that only accepts tokens for allowlisted, verified emails.

    Anyone can complete the Google login, so the check has to happen on every
    request, not just at sign-in.
    """

    def __init__(self, *, allowed_emails: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self.allowed_emails = allowed_emails

    async def load_access_token(self, token):
        access_token = await super().load_access_token(token)
        if access_token is None:
            return None
        claims = access_token.claims or {}
        email = str(claims.get("email", "")).lower()
        if email in self.allowed_emails and claims.get("email_verified") in (True, "true"):
            return access_token
        log.warning("rejected token for email=%r", email)
        return None


def build_auth(settings: Settings) -> AllowlistGoogleProvider:
    return AllowlistGoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=settings.public_base_url,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        allowed_client_redirect_uris=CLAUDE_REDIRECT_URIS,
        jwt_signing_key=settings.jwt_signing_key,
        extra_authorize_params={"prompt": "select_account"},
        allowed_emails=settings.allowed_emails,
    )
