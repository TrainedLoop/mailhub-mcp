import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from google.auth.exceptions import TransportError

from mailhub_mcp.admin_web.identity import GoogleIdentity, LoginError, OwnerLogin


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def make(claims=None, response=None, post_exc=None, verify_exc=None):
    calls = {}

    def post(url, data=None, timeout=None):
        calls["post"] = (url, data, timeout)
        if post_exc:
            raise post_exc
        return response or FakeResponse(200, {"id_token": "tok"})

    def verify(token, request, client_id, **kw):
        calls["verify"] = (token, client_id)
        calls["verify_kw"] = kw
        if verify_exc:
            raise verify_exc
        return claims

    return GoogleIdentity("cid", "secret", post=post, verify=verify), calls


GOOD = {"email": "Me@Gmail.com", "email_verified": True, "nonce": "n", "auth_time": 1700000000}


def complete(ident):
    return ident.complete("code", "https://x/cb", "ver", "n")


def test_authorization_url():
    ident, _ = make()
    url = ident.authorization_url("https://x/cb", "st", "no", "chal")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "https://x/cb"
    assert q["scope"] == "openid email"
    assert q["state"] == "st"
    assert q["nonce"] == "no"
    assert q["code_challenge"] == "chal"
    assert q["code_challenge_method"] == "S256"
    assert q["prompt"] == "select_account"
    assert q["claims"] == '{"id_token":{"auth_time":{"essential":true}}}'
    assert json.loads(q["claims"]) == {"id_token": {"auth_time": {"essential": True}}}
    assert "max_age" not in q


def test_authorization_url_with_max_age():
    ident, _ = make()
    url = ident.authorization_url("https://x/cb", "st", "no", "chal", max_age=300)
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["max_age"] == "300"
    assert q["prompt"] == "select_account"


def test_happy_path_and_post_args():
    ident, calls = make(GOOD)
    assert complete(ident) == OwnerLogin("me@gmail.com", 1700000000.0)
    url, data, timeout = calls["post"]
    assert url == "https://oauth2.googleapis.com/token"
    assert timeout == 10
    assert data == {
        "grant_type": "authorization_code",
        "code": "code",
        "redirect_uri": "https://x/cb",
        "client_id": "cid",
        "client_secret": "secret",
        "code_verifier": "ver",
    }
    assert calls["verify"] == ("tok", "cid")
    assert calls["verify_kw"] == {"clock_skew_in_seconds": 10}  # same tolerance as the Gmail linker


def test_no_auth_time():
    ident, _ = make({k: v for k, v in GOOD.items() if k != "auth_time"})
    assert complete(ident).auth_time is None


@pytest.mark.parametrize("bad", ["123", True, None, [1]])
def test_non_numeric_auth_time_is_none(bad):
    ident, _ = make({**GOOD, "auth_time": bad})
    assert complete(ident).auth_time is None


def test_float_auth_time():
    ident, _ = make({**GOOD, "auth_time": 1.5})
    assert complete(ident).auth_time == 1.5


def test_email_verified_string_true():
    ident, _ = make({**GOOD, "email_verified": "true"})
    assert complete(ident).email == "me@gmail.com"


@pytest.mark.parametrize("claims", [
    {**GOOD, "nonce": "other"},
    {k: v for k, v in GOOD.items() if k != "nonce"},
    {**GOOD, "email_verified": False},
    {**GOOD, "email_verified": "yes"},
    {**GOOD, "email_verified": 1},
    {k: v for k, v in GOOD.items() if k != "email"},
    {**GOOD, "email": ""},
])
def test_bad_claims(claims):
    ident, _ = make(claims)
    with pytest.raises(LoginError):
        complete(ident)


def test_token_endpoint_error():
    ident, _ = make(GOOD, response=FakeResponse(400, {"error": "x"}))
    with pytest.raises(LoginError, match="Google sign-in failed"):
        complete(ident)


def test_missing_id_token():
    ident, _ = make(GOOD, response=FakeResponse(200, {}))
    with pytest.raises(LoginError):
        complete(ident)


def test_non_json_body():
    class Bad(FakeResponse):
        def json(self):
            raise ValueError("secret-code")

    ident, _ = make(GOOD, response=Bad())
    with pytest.raises(LoginError) as e:
        complete(ident)
    assert "secret-code" not in str(e.value)


def test_request_exception():
    ident, _ = make(GOOD, post_exc=requests.ConnectionError("code=code leak"))
    with pytest.raises(LoginError) as e:
        complete(ident)
    assert str(e.value) == "Google sign-in failed"


def test_verify_value_error():
    ident, _ = make(verify_exc=ValueError("tok bad"))
    with pytest.raises(LoginError) as e:
        complete(ident)
    assert "tok" not in str(e.value)


def test_verify_transport_error():
    ident, _ = make(verify_exc=TransportError("certs fetch failed"))
    with pytest.raises(LoginError, match="Google sign-in failed"):
        complete(ident)


# --- reason codes: logged by the routes; never part of the text shown to the user ----------------


class _NotJson(FakeResponse):
    def json(self):
        raise ValueError("not json")


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"post_exc": requests.ConnectionError("x")}, "token-exchange"),
    ({"response": FakeResponse(400, {"error": "x"})}, "token-exchange"),
    ({"response": _NotJson()}, "token-exchange"),
    ({"response": FakeResponse(200, {})}, "token-exchange"),
    ({"verify_exc": ValueError("bad")}, "verify"),
    ({"verify_exc": TransportError("certs")}, "verify"),
    ({"claims": {**GOOD, "nonce": "other"}}, "nonce"),
    ({"claims": {**GOOD, "email_verified": False}}, "unverified-email"),
    ({"claims": {k: v for k, v in GOOD.items() if k != "email"}}, "no-email"),
    ({"claims": {**GOOD, "email": ""}}, "no-email"),
])
def test_login_error_carries_a_reason_code_and_the_generic_text(kwargs, reason):
    ident, _ = make(**({"claims": GOOD} | kwargs))
    with pytest.raises(LoginError) as e:
        complete(ident)
    assert e.value.reason == reason
    assert str(e.value) == "Google sign-in failed"
