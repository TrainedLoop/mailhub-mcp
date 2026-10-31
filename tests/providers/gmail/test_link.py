import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials

from mailhub_mcp.providers import linkers
from mailhub_mcp.providers.base import LinkError
from mailhub_mcp.providers.gmail.link import GmailLinker
from mailhub_mcp.providers.gmail.provider import GMAIL_SCOPE, SCOPES


class Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._body


def token_body(**over):
    body = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "scope": f"openid https://www.googleapis.com/auth/userinfo.email {GMAIL_SCOPE}",
        "id_token": "raw-id-token",
    }
    body.update(over)
    return body


def make(resp=None, claims=None, verify_exc=None, calls=None):
    calls = calls if calls is not None else {}

    def post(url, **kw):
        calls["post"] = (url, kw)
        if isinstance(resp, Exception):
            raise resp
        return resp if resp is not None else Resp(200, token_body())

    def verify(token, request, client_id, **kw):
        calls["verify"] = (token, client_id, kw)
        if verify_exc:
            raise verify_exc
        return claims if claims is not None else {"email": "Me@Gmail.com", "email_verified": True}

    return GmailLinker("cid", "csecret", post=post, verify=verify), calls


def test_label_and_registry():
    assert GmailLinker.label == "Gmail"
    assert isinstance(linkers("a", "b")["gmail"], GmailLinker)


def test_authorization_url():
    linker, _ = make()
    url = linker.authorization_url("https://x/cb", "st", "chal")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "https://x/cb"
    assert q["scope"] == f"{GMAIL_SCOPE} openid email"
    assert q["state"] == "st"
    assert q["code_challenge"] == "chal"
    assert q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline"
    assert q["prompt"] == "consent select_account"
    assert q["include_granted_scopes"] == "false"


def test_complete_happy_path(tmp_path):
    linker, calls = make()
    result = linker.complete("code", "https://x/cb", "ver")
    assert result.address == "me@gmail.com"
    url, kw = calls["post"]
    assert url == "https://oauth2.googleapis.com/token"
    assert kw["timeout"] == 10
    assert kw["data"]["code_verifier"] == "ver"
    assert calls["verify"][1] == "cid"
    assert calls["verify"][2] == {"clock_skew_in_seconds": 10}
    path = tmp_path / "cred.json"
    path.write_text(result.credential_json)
    data = json.loads(result.credential_json)
    assert data["type"] == "authorized_user"
    assert data["scopes"] == [GMAIL_SCOPE]
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    assert creds.refresh_token == "rt-1"
    assert creds.client_id == "cid"


def test_missing_refresh_token():
    linker, _ = make(Resp(200, token_body(refresh_token=None)))
    with pytest.raises(LinkError, match="offline access"):
        linker.complete("c", "r", "v")


def test_scope_without_gmail():
    linker, _ = make(Resp(200, token_body(scope="openid email https://www.googleapis.com/auth/gmail.modify.extra")))
    with pytest.raises(LinkError, match="Gmail access was not granted"):
        linker.complete("c", "r", "v")


@pytest.mark.parametrize("verified", [False, "false", None])
def test_unverified_email(verified):
    linker, _ = make(claims={"email": "a@b.c", "email_verified": verified})
    with pytest.raises(LinkError):
        linker.complete("c", "r", "v")


def test_string_true_verified_ok():
    linker, _ = make(claims={"email": "a@b.c", "email_verified": "true"})
    assert linker.complete("c", "r", "v").address == "a@b.c"


@pytest.mark.parametrize(
    "resp",
    [
        requests.ConnectionError("boom code=secret"),
        Resp(400, {}),
        Resp(200, bad_json=True),
        Resp(200, ["not", "dict"]),
        Resp(200, token_body(id_token=None)),
    ],
)
def test_exchange_failures(resp):
    linker, _ = make(resp)
    with pytest.raises(LinkError) as e:
        linker.complete("secretcode", "r", "v")
    assert "secret" not in str(e.value)


@pytest.mark.parametrize("exc", [ValueError("bad"), GoogleAuthError("bad")])
def test_verification_failure(exc):
    linker, _ = make(verify_exc=exc)
    with pytest.raises(LinkError):
        linker.complete("c", "r", "v")


def test_missing_email_claim():
    linker, _ = make(claims={"email_verified": True})
    with pytest.raises(LinkError):
        linker.complete("c", "r", "v")


def revoker(tmp_path, content, resp):
    path = tmp_path / "c.json"
    if content is not None:
        path.write_text(content)
    calls = {}

    def post(url, **kw):
        calls["post"] = (url, kw)
        if isinstance(resp, Exception):
            raise resp
        return resp

    return GmailLinker("cid", "cs", post=post), path, calls


def test_revoke_ok(tmp_path):
    linker, path, calls = revoker(tmp_path, json.dumps({"refresh_token": "rt"}), Resp(200))
    assert linker.revoke(path) is True
    url, kw = calls["post"]
    assert url == "https://oauth2.googleapis.com/revoke"
    assert kw["data"] == {"token": "rt"}
    assert kw["timeout"] == 10


def test_revoke_400(tmp_path):
    linker, path, _ = revoker(tmp_path, json.dumps({"refresh_token": "rt"}), Resp(400))
    assert linker.revoke(path) is False


def test_revoke_network(tmp_path):
    linker, path, _ = revoker(tmp_path, json.dumps({"refresh_token": "rt"}), requests.ConnectionError())
    assert linker.revoke(path) is False


@pytest.mark.parametrize("content", [None, "not json", "[]", json.dumps({}), json.dumps({"refresh_token": ""})])
def test_revoke_unusable_file(tmp_path, content):
    linker, path, calls = revoker(tmp_path, content, Resp(200))
    assert linker.revoke(path) is False
    assert "post" not in calls
