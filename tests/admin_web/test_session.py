import base64
import hashlib

from mailhub_mcp.admin_web.session import (
    FLOW_SECONDS,
    REAUTH_SECONDS,
    SESSION_SECONDS,
    Flow,
    Session,
    Signer,
    check_csrf,
    csrf_token,
    decode_flow,
    decode_session,
    derive_key,
    encode_flow,
    encode_session,
    is_fresh,
    new_pkce,
    random_token,
    same_origin,
)

KEY = derive_key("k" * 32)
SIGNER = Signer(KEY)
ALLOWED = frozenset({"me@gmail.com"})


def _session(email="me@gmail.com", sid="sid1", login_at=1000.0, auth_at=1000.0):
    return Session(email=email, sid=sid, login_at=login_at, auth_at=auth_at)


def _flow(**kw):
    base = dict(kind="login", state="s", nonce="n", code_verifier="v", created_at=1000.0)
    base.update(kw)
    return Flow(**base)


def test_constants():
    assert (SESSION_SECONDS, REAUTH_SECONDS, FLOW_SECONDS) == (8 * 3600, 300, 600)


def test_derive_key():
    assert len(KEY) == 32
    assert KEY != ("k" * 32).encode()
    assert derive_key("k" * 32) == KEY
    assert derive_key("j" * 32) != KEY


def test_signer_round_trip_and_tamper():
    token = SIGNER.dumps({"a": 1})
    assert SIGNER.loads(token) == {"a": 1}
    for i in range(len(token)):
        flipped = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1 :]
        assert SIGNER.loads(flipped) is None
    assert Signer(derive_key("other" * 8)).loads(token) is None


def test_signer_malformed():
    for bad in ["", "abc", ".", "a.b", "a.b.c", "!!!.???", "é.é"]:
        assert SIGNER.loads(bad) is None
    # valid signature over non-JSON / non-object payload
    for payload in (b"notjson", b"[1,2]"):
        p = base64.urlsafe_b64encode(payload).rstrip(b"=")
        import hmac

        sig = base64.urlsafe_b64encode(hmac.new(KEY, p, hashlib.sha256).digest()).rstrip(b"=")
        assert SIGNER.loads((p + b"." + sig).decode()) is None


def test_session_round_trip_and_expiry():
    tok = encode_session(SIGNER, _session())
    got = decode_session(SIGNER, tok, 1000.0 + SESSION_SECONDS - 1, ALLOWED)
    assert got == _session()
    assert decode_session(SIGNER, tok, 1000.0 + SESSION_SECONDS, ALLOWED) is None


def test_session_missing_tampered_and_disallowed():
    tok = encode_session(SIGNER, _session())
    assert decode_session(SIGNER, None, 1001.0, ALLOWED) is None
    assert decode_session(SIGNER, "", 1001.0, ALLOWED) is None
    assert decode_session(SIGNER, tok + "x", 1001.0, ALLOWED) is None
    assert decode_session(SIGNER, tok, 1001.0, frozenset({"other@gmail.com"})) is None


def test_session_email_lowercased():
    tok = encode_session(SIGNER, _session(email="Me@Gmail.com"))
    got = decode_session(SIGNER, tok, 1001.0, ALLOWED)
    assert got is not None and got.email == "me@gmail.com"


def test_session_unknown_or_missing_fields():
    good = {"email": "me@gmail.com", "sid": "s", "login_at": 1.0, "auth_at": 1.0, "has_auth_time": False}
    assert decode_session(SIGNER, SIGNER.dumps(good), 2.0, ALLOWED) is not None
    assert decode_session(SIGNER, SIGNER.dumps({**good, "extra": 1}), 2.0, ALLOWED) is None
    missing = {k: v for k, v in good.items() if k != "sid"}
    assert decode_session(SIGNER, SIGNER.dumps(missing), 2.0, ALLOWED) is None
    assert decode_session(SIGNER, SIGNER.dumps({**good, "login_at": "x"}), 2.0, ALLOWED) is None


def test_flow_round_trip_and_expiry():
    f = _flow(kind="link", sid="x", email="Me@Gmail.com", alias="a", provider="gmail")
    tok = encode_flow(SIGNER, f)
    assert decode_flow(SIGNER, tok, 1000.0 + FLOW_SECONDS - 1) == f
    assert decode_flow(SIGNER, tok, 1000.0 + FLOW_SECONDS) is None
    assert decode_flow(SIGNER, None, 1001.0) is None
    assert decode_flow(SIGNER, tok + "x", 1001.0) is None
    assert decode_flow(SIGNER, encode_session(SIGNER, _session()), 1001.0) is None


def test_flow_defaults_and_bad_kind():
    f = decode_flow(SIGNER, encode_flow(SIGNER, _flow()), 1001.0)
    assert f is not None and f.return_to == "/admin" and f.sid is None
    bad = {"kind": "evil", "state": "s", "nonce": "n", "code_verifier": "v", "created_at": 1.0}
    assert decode_flow(SIGNER, SIGNER.dumps(bad), 2.0) is None


def test_is_fresh():
    s = _session(auth_at=1000.0)
    assert is_fresh(s, 1000.0 + 299)
    assert not is_fresh(s, 1000.0 + 300)


def test_csrf():
    s, other = _session(sid="a"), _session(sid="b")
    tok = csrf_token(KEY, s)
    assert check_csrf(KEY, s, tok)
    assert not check_csrf(KEY, other, tok)
    assert not check_csrf(KEY, s, None)
    assert not check_csrf(KEY, s, "")
    assert not check_csrf(KEY, s, tok + "x")
    assert not check_csrf(derive_key("z" * 32), s, tok)


def test_same_origin():
    base = "https://example.test"
    assert same_origin(base, "https://example.test", None)
    assert same_origin(base, "HTTPS://Example.Test:443", None)
    assert same_origin(base, None, "https://example.test/admin")
    assert not same_origin(base, "http://example.test:8742", None)
    assert not same_origin(base, "http://example.test", None)
    assert not same_origin(base, "https://evil.test", None)
    assert not same_origin(base, "https://example.test.evil.test", None)
    assert not same_origin(base, "null", "https://example.test/admin")
    assert not same_origin(base, None, None)
    assert not same_origin(base, "", None)
    assert not same_origin(base, "https://example.test:bad", None)
    assert same_origin("http://localhost:8742", "http://localhost:8742", None)
    assert not same_origin("http://localhost:8742", "http://localhost", None)


def test_pkce_and_random_token():
    verifier, challenge = new_pkce()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert "=" not in challenge
    assert new_pkce()[0] != verifier
    assert random_token() != random_token()


def test_flow_carries_login_at_of_the_session_being_reauthenticated():
    f = _flow(sid="x", email="me@gmail.com", login_at=900.0)
    assert decode_flow(SIGNER, encode_flow(SIGNER, f), 1001.0) == f
    assert decode_flow(SIGNER, encode_flow(SIGNER, _flow()), 1001.0).login_at is None
    for bad in ("900", True):
        data = {"kind": "login", "state": "s", "nonce": "n", "code_verifier": "v",
                "created_at": 1000.0, "login_at": bad}
        assert decode_flow(SIGNER, SIGNER.dumps(data), 1001.0) is None
