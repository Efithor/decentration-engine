"""Comprehensive pytest suite for `app.inputs.gmail`."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Lightweight stubs for optional Google Cloud dependencies so that importing
# the production modules does not require the real `google-cloud-*` packages
# to be installed inside the test environment.
# ---------------------------------------------------------------------------

import sys
import types

# Root `google` package.
google_root = types.ModuleType("google")
sys.modules.setdefault("google", google_root)

# `google.cloud` namespace package.
google_cloud = types.ModuleType("google.cloud")
sys.modules.setdefault("google.cloud", google_cloud)
google_root.cloud = google_cloud

# google.cloud.logging ------------------------------------------------------
gcp_logging = types.ModuleType("google.cloud.logging")


class _DummyLogger:
    def log_text(self, *args, **kwargs):
        pass


class _DummyLoggingClient:  # noqa: D101 (private helper)
    def __init__(self, *args, **kwargs):
        pass

    def logger(self, name):  # noqa: D401
        return _DummyLogger()


gcp_logging.Client = _DummyLoggingClient  # type: ignore[attr-defined]
google_cloud.logging = gcp_logging
sys.modules.setdefault("google.cloud.logging", gcp_logging)

# google.cloud.secretmanager ------------------------------------------------
secretmanager = types.ModuleType("google.cloud.secretmanager")


class _DummySecretManagerClient:  # noqa: D101
    def access_secret_version(self, request):  # noqa: D401
        class _Payload:
            data = b"dummy-secret"

        class _Response:
            payload = _Payload()

        return _Response()


secretmanager.SecretManagerServiceClient = _DummySecretManagerClient  # type: ignore[attr-defined]
google_cloud.secretmanager = secretmanager
sys.modules.setdefault("google.cloud.secretmanager", secretmanager)

# google.cloud.sql.connector -----------------------------------------------
sql_connector = types.ModuleType("google.cloud.sql.connector")


class _DummyConnector:  # noqa: D101
    def connect(self, *args, **kwargs):  # noqa: D401
        return None


sql_connector.Connector = _DummyConnector  # type: ignore[attr-defined]
sys.modules.setdefault("google.cloud.sql.connector", sql_connector)

# Expose parent namespace `google.cloud.sql` so that "from google.cloud.sql.connector import Connector" works.
sql_pkg = types.ModuleType("google.cloud.sql")
sql_pkg.connector = sql_connector
google_cloud.sql = sql_pkg
sys.modules.setdefault("google.cloud.sql", sql_pkg)

# pandas --------------------------------------------------------------------
pandas_stub = types.ModuleType("pandas")


class _DummyDataFrame(list):
    def to_dict(self, orient="records"):
        return list(self)


pandas_stub.DataFrame = _DummyDataFrame  # type: ignore[attr-defined]
pandas_stub.read_sql = lambda *args, **kwargs: _DummyDataFrame()  # type: ignore[attr-defined]
sys.modules.setdefault("pandas", pandas_stub)

# sqlalchemy ----------------------------------------------------------------
sqlalchemy_stub = types.ModuleType("sqlalchemy")


def _dummy_create_engine(*args, **kwargs):  # noqa: D401
    class _DummyEngine:  # noqa: D101
        def dispose(self):  # noqa: D401
            pass

    return _DummyEngine()


def _dummy_text(query):  # noqa: D401
    return query


sqlalchemy_stub.create_engine = _dummy_create_engine  # type: ignore[attr-defined]
sqlalchemy_stub.text = _dummy_text  # type: ignore[attr-defined]
sys.modules.setdefault("sqlalchemy", sqlalchemy_stub)

# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

import app.inputs.gmail as gmail


# ---------------------------------------------------------------------------
# Helper fixtures & stubs
# ---------------------------------------------------------------------------


class FakeIMAPClient:
    """A minimal stub mimicking the parts of `imaplib.IMAP4_SSL` used by gmail.py."""

    def __init__(self, uid_to_msg: dict[bytes, bytes]):
        # Mapping from UID -> raw RFC-822 bytes
        self._uid_to_msg = uid_to_msg
        self.selected: list[str] = []
        self.raw_search_query: str | None = None

    # ----- Basic IMAP verbs -------------------------------------------------

    def select(self, mailbox: str, *_, **__):  # noqa: D401  (simple method)
        """Record selected mailbox; gmail.py ignores the return payload."""
        self.selected.append(mailbox)
        # Mimic (status, data) tuple that imaplib returns
        return "OK", [b""]

    def uid(self, command: str, *args):  # noqa: D401
        """Handle only the SEARCH and FETCH commands used by gmail.query_emails."""

        if command == "SEARCH":
            # SEARCH is invoked as: client.uid("SEARCH", "X-GM-RAW", f'"{raw_query}"')
            if len(args) >= 3:
                # Third arg is the quoted raw query
                self.raw_search_query = args[2].strip("\"")  # remove surrounding quotes
            # Return all UIDs separated by spaces, newest UID *last* (Gmail semantics)
            uid_blob = b" ".join(self._uid_to_msg.keys())
            return "OK", [uid_blob]

        if command == "FETCH":
            uid, msg_part = args[0], args[1]
            # gmail.query_emails always asks for "(RFC822)"
            assert msg_part == "(RFC822)"  # strict parity with production call
            msg_bytes = self._uid_to_msg.get(uid if isinstance(uid, bytes) else uid.encode())
            if msg_bytes is None:
                return "NO", []
            # The real imaplib packs the payload inside a weird tuple structure; we need
            # only the 2-tuple (b"1 (RFC822 {size})", raw_bytes) for gmail.py to work.
            meta = f"{uid.decode() if isinstance(uid, bytes) else uid} (RFC822 {{len}})".encode()
            return "OK", [(meta, msg_bytes)]

        # Any other command is unimplemented for the purposes of the tests.
        return "BAD", []

    def logout(self):  # noqa: D401  (simple method)
        """Included just so gmail.query_emails can call it safely."""
        return "BYE", []


@pytest.fixture
def sample_email_bytes() -> bytes:
    """Return a simple plain-text e-mail encoded as raw RFC-822 bytes."""

    msg = EmailMessage()
    msg["Subject"] = "Test Email"
    msg["From"] = "sender@example.com"
    msg["To"] = "receiver@example.com"
    msg.set_content("Hello pytest world!")
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# Unit tests for private helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "encoded, expected",
    [
        ("=?utf-8?b?SGVsbG8gd29ybGQ=?=", "Hello world"),
        ("=?utf-8?q?hello=5Fworld?=", "hello_world"),
        ("Plain ASCII", "Plain ASCII"),
    ],
)
def test_decode_mime_words(encoded: str, expected: str):
    """_decode_mime_words should correctly decode various encodings."""

    assert gmail._decode_mime_words(encoded) == expected  # type: ignore[attr-defined]


def test_extract_plain_text_email(sample_email_bytes: bytes):
    """Ensure plain-text part is extracted from a simple message."""

    from email import message_from_bytes

    msg_obj = message_from_bytes(sample_email_bytes)
    body = gmail._extract_plain_text_email(msg_obj)  # type: ignore[attr-defined]
    assert body.strip() == "Hello pytest world!"


# ---------------------------------------------------------------------------
# query_emails – main public API
# ---------------------------------------------------------------------------


def _patch_imap(monkeypatch: pytest.MonkeyPatch, imap_client: FakeIMAPClient):
    """Helper to force gmail._get_imap_client to return our fake client."""

    monkeypatch.setattr(gmail, "_get_imap_client", lambda: imap_client)


def test_query_emails_basic_flow(monkeypatch: pytest.MonkeyPatch, sample_email_bytes: bytes):
    """Happy-path: one message returned, all fields populated."""

    fake_client = FakeIMAPClient({b"1": sample_email_bytes})
    _patch_imap(monkeypatch, fake_client)

    since_ts = datetime.now(tz=timezone.utc) - timedelta(days=1)
    results = gmail.query_emails(since=since_ts, max_results=10)

    assert len(results) == 1
    msg = results[0]

    # Sanity-check a handful of expected keys
    for key in ["id", "subject", "from", "to", "snippet", "body", "timestamp"]:
        assert key in msg
    assert msg["subject"] == "Test Email"
    assert "Hello pytest world!" in msg["body"]


def test_query_emails_respects_max_results(monkeypatch: pytest.MonkeyPatch, sample_email_bytes: bytes):
    """Verify that the `max_results` guard is honoured."""

    fake_client = FakeIMAPClient({b"1": sample_email_bytes, b"2": sample_email_bytes, b"3": sample_email_bytes})
    _patch_imap(monkeypatch, fake_client)

    since_ts = datetime.now(tz=timezone.utc) - timedelta(days=1)
    results = gmail.query_emails(since=since_ts, max_results=2)

    assert len(results) == 2


def test_query_emails_returns_empty_when_no_client(monkeypatch: pytest.MonkeyPatch):
    """If _get_imap_client returns None the function should degrade gracefully."""

    monkeypatch.setattr(gmail, "_get_imap_client", lambda: None)

    since_ts = datetime.now(tz=timezone.utc) - timedelta(days=1)
    assert gmail.query_emails(since=since_ts) == []


def test_query_emails_naive_datetime_raises(sample_email_bytes: bytes):
    """`since` must be timezone-aware."""

    naive_dt = datetime.now()  # no tzinfo
    with pytest.raises(ValueError):
        gmail.query_emails(since=naive_dt)


def test_query_emails_builds_correct_search_query(monkeypatch: pytest.MonkeyPatch, sample_email_bytes: bytes):
    """Inspect the raw X-GM-RAW query generated by the function."""

    fake_client = FakeIMAPClient({b"1": sample_email_bytes})
    _patch_imap(monkeypatch, fake_client)

    # Case 1: default arguments (unread_only=True + default categories)
    since_ts = datetime.now(tz=timezone.utc) - timedelta(days=1)
    gmail.query_emails(since=since_ts, max_results=1)

    raw_q = fake_client.raw_search_query or ""
    assert "is:unread" in raw_q
    # Default categories are primary OR updates
    assert "category:primary" in raw_q and "category:updates" in raw_q

    # Case 2: custom args
    fake_client_2 = FakeIMAPClient({b"1": sample_email_bytes})
    _patch_imap(monkeypatch, fake_client_2)
    gmail.query_emails(since=since_ts, categories=[], unread_only=False, max_results=1)

    raw_q2 = fake_client_2.raw_search_query or ""
    assert "is:unread" not in raw_q2  # flag suppressed
    assert "category:" not in raw_q2  # no categories filter


# ---------------------------------------------------------------------------
# _resolve_credential_paths behaviour (no real filesystem access required)
# ---------------------------------------------------------------------------


def test_resolve_credential_paths_env_override(monkeypatch: pytest.MonkeyPatch):
    """Environment variable overrides should take precedence over defaults."""

    fake_client_secret = "/tmp/foo.json"
    fake_token = "/tmp/bar.json"

    monkeypatch.setenv("GMAIL_CLIENT_SECRET", fake_client_secret)
    monkeypatch.setenv("GMAIL_TOKEN_FILE", fake_token)

    cs_path, token_path = gmail._resolve_credential_paths()  # type: ignore[attr-defined]
    assert str(cs_path) == fake_client_secret
    assert str(token_path) == fake_token