import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError, TransportError

from mailhub_mcp.core.errors import AuthExpired, MailhubError, MessageNotFound, TransientError
from mailhub_mcp.providers import PROVIDERS
from mailhub_mcp.providers.gmail import provider as provider_module
from mailhub_mcp.providers.gmail.provider import GmailProvider, default_build
from tests.fakes.fake_gmail import FakeGmail, http_error
from tests.providers.contract import ProviderContract, default_seed


def seeded_fake(seed) -> FakeGmail:
    fake = FakeGmail()
    for m in seed["messages"]:
        fake.add_message(
            m["id"], thread_id=m["thread_id"], labels=m["labels"],
            headers={"From": m["from"], "Subject": m["subject"], "Message-ID": m["message_id_header"]},
            body=m["body"], internal_date=m["internal_date"],
        )
    for name in seed["labels"]:
        fake.labels.append({"id": f"L_{name}", "name": name, "type": "user"})
    for i in seed["transient_ids"]:
        fake.add_message(i, labels=["INBOX"])  # listed by search, but fetching it fails
        fake.get_errors[i] = http_error(503, "unavailable")
    return fake


TOKEN = {"type": "authorized_user", "client_id": "c", "client_secret": "s", "refresh_token": "r"}


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps(TOKEN))
    return path


@pytest.fixture
def fake():
    return seeded_fake(default_seed())


class TestGmailContract(ProviderContract):
    @pytest.fixture
    def provider(self, seed, token_file):
        fake = seeded_fake(seed)
        return GmailProvider("a", token_file, build_service=lambda c: fake)


@pytest.fixture
def gmail(fake, token_file):
    return GmailProvider("a", token_file, build_service=lambda c: fake)


def test_registered():
    assert PROVIDERS["gmail"] is GmailProvider


def test_refresh_error_becomes_auth_expired(gmail, fake):
    fake.fail = RefreshError("revoked")
    with pytest.raises(AuthExpired) as e:
        gmail.search("in:inbox", 5)
    assert "mailhub-authorize gmail <client_secret.json> a" in str(e.value)


def test_503_on_thread_is_transient(gmail, fake):
    fake.fail = http_error(503, "unavailable")
    with pytest.raises(TransientError):
        gmail.get_thread("t1")


def test_429_is_transient(gmail, fake):
    fake.fail = http_error(429, "slow down")
    with pytest.raises(TransientError):
        gmail.list_labels()


def test_403_rate_limit_is_transient(gmail, fake):
    import json

    import httplib2
    from googleapiclient.errors import HttpError

    body = {"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}]}}
    fake.fail = HttpError(httplib2.Response({"status": 403}), json.dumps(body).encode())
    with pytest.raises(TransientError):
        gmail.list_labels()


def test_plain_403_is_not_transient(gmail, fake):
    fake.fail = http_error(403, "forbidden")
    with pytest.raises(MailhubError) as e:
        gmail.list_labels()
    assert not isinstance(e.value, TransientError)


def test_unknown_thread_not_found(gmail):
    with pytest.raises(MessageNotFound):
        gmail.get_thread("nope")


def test_unknown_reply_target_not_found(gmail):
    with pytest.raises(MessageNotFound):
        gmail.create_draft("x@y.com", "s", "b", None, "nope")


def test_other_status_is_mailhub_error(gmail, fake):
    fake.fail = http_error(403, "Forbidden")
    with pytest.raises(MailhubError) as e:
        gmail.list_labels()
    assert not isinstance(e.value, (TransientError, AuthExpired))
    assert "403" in str(e.value)


def test_transport_error_is_transient(gmail, fake):
    fake.fail = TransportError("connection reset")
    with pytest.raises(TransientError):
        gmail.search("in:inbox", 5)


def test_transport_error_while_refreshing_is_transient(token_file):
    def boom(creds):
        raise TransportError("refresh failed")

    provider = GmailProvider("a", token_file, build_service=boom)
    with pytest.raises(TransientError):
        provider.list_labels()


def test_missing_token_file_is_auth_expired(tmp_path):
    provider = GmailProvider("a", tmp_path / "does-not-exist.json", build_service=lambda c: None)
    with pytest.raises(AuthExpired) as e:
        provider.list_labels()
    assert "mailhub-authorize gmail <client_secret.json> a" in str(e.value)


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"refresh_token": "r"}'])
def test_corrupt_token_file_is_auth_expired(tmp_path, content):
    path = tmp_path / "a.json"
    path.write_text(content)
    provider = GmailProvider("a", path, build_service=lambda c: None)
    with pytest.raises(AuthExpired):
        provider.address()


def test_concurrent_calls_load_and_refresh_credentials_once(token_file, monkeypatch, fake):
    loads = []
    real_load = provider_module.Credentials.from_authorized_user_file
    state = {"running": 0, "max": 0}
    guard = threading.Lock()

    def counting_load(*a, **k):
        loads.append(1)
        time.sleep(0.05)
        return real_load(*a, **k)

    def slow_build(creds):
        with guard:
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
        time.sleep(0.05)
        with guard:
            state["running"] -= 1
        return fake

    monkeypatch.setattr(
        provider_module.Credentials, "from_authorized_user_file", staticmethod(counting_load)
    )
    provider = GmailProvider("a", token_file, build_service=slow_build)
    threads = [threading.Thread(target=provider.list_labels) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loads) == 1
    assert state["max"] == 1  # the lock serializes credential load, refresh and client build


def test_default_build_refreshes_invalid_credentials(monkeypatch):
    creds = SimpleNamespace(valid=False, refresh=MagicMock())
    build = MagicMock(return_value="service")
    monkeypatch.setattr(provider_module, "build", build)
    assert default_build(creds) == "service"
    creds.refresh.assert_called_once()
    build.assert_called_once_with("gmail", "v1", credentials=creds, cache_discovery=False)


def test_default_build_skips_refresh_for_valid_credentials(monkeypatch):
    creds = SimpleNamespace(valid=True, refresh=MagicMock())
    build = MagicMock(return_value="service")
    monkeypatch.setattr(provider_module, "build", build)
    default_build(creds)
    creds.refresh.assert_not_called()
    build.assert_called_once_with("gmail", "v1", credentials=creds, cache_discovery=False)


def test_get_messages_classifies_failures(gmail, fake):
    fake.get_errors["m1"] = http_error(403, "forbidden")
    fake.get_errors["m2"] = http_error(503, "unavailable")
    fake.get_errors["m3"] = http_error(429, "slow down")
    r = gmail.get_messages(["m1", "m2", "m3", "nope"], "labels")
    assert r.failed == {
        "m1": "could not be read",
        "m2": "temporary failure, retry",
        "m3": "temporary failure, retry",
    }
    assert r.missing == ["nope"]


def test_provider_shares_one_rate_limiter_across_calls(gmail, monkeypatch):
    from mailhub_mcp.providers.gmail import api

    seen = []
    real = api.batch_get

    def spy(svc, ids, fmt, limiter=None):
        seen.append(limiter)
        return real(svc, ids, fmt, limiter=limiter)

    monkeypatch.setattr(api, "batch_get", spy)
    gmail.get_messages(["m1"], "labels")
    gmail.search("in:inbox", 5)
    assert len(seen) >= 2 and seen[0] is not None and all(x is seen[0] for x in seen)


def test_provider_charges_every_call_to_its_shared_limiter(tmp_path):
    fake = seeded_fake(default_seed())
    fake.add_message("mx", thread_id="tx", labels=["INBOX"])
    provider = GmailProvider("a", tmp_path / "a.json", build_service=lambda creds: fake)
    provider._creds = object()
    units = []
    provider._limiter = SimpleNamespace(acquire=units.append)

    provider.address()
    assert units == [1]
    units.clear()
    provider.list_ids("", 5)
    provider.get_thread("tx")
    provider.list_labels()
    provider.ensure_label("brand-new")
    provider.modify_labels(["mx"], ["INBOX"], [])
    provider.create_draft("x@y.com", "s", "b", None, None)
    assert units == [5, 10, 1, 1, 5, 50, 10]
