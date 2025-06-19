"""Comprehensive tests for `app.connections.openai_client`."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stub external dependencies **before** importing the module under test so that
# its top-level import logic sees the fake packages.
# ---------------------------------------------------------------------------

import sys
import types

# -------------------------- Google Cloud stubs -----------------------------

google_root = sys.modules.setdefault("google", types.ModuleType("google"))
google_cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
google_root.cloud = google_cloud

# google.cloud.logging
logging_mod = types.ModuleType("google.cloud.logging")

class _DummyLogger:
    def log_text(self, *args, **kwargs):
        pass

class _DummyLoggingClient:  # noqa: D101
    def logger(self, *_):  # noqa: D401
        return _DummyLogger()

logging_mod.Client = _DummyLoggingClient  # type: ignore[attr-defined]
sys.modules.setdefault("google.cloud.logging", logging_mod)
google_cloud.logging = logging_mod

# google.cloud.secretmanager (needed by helper_functions)
secretmanager_mod = types.ModuleType("google.cloud.secretmanager")
class _DummySecretManagerClient:  # noqa: D101
    def access_secret_version(self, request):  # noqa: D401
        class _Payload:  # noqa: D101
            data = b"dummy"
        class _Response:  # noqa: D101
            payload = _Payload()
        return _Response()
secretmanager_mod.SecretManagerServiceClient = _DummySecretManagerClient  # type: ignore[attr-defined]
sys.modules.setdefault("google.cloud.secretmanager", secretmanager_mod)
google_cloud.secretmanager = secretmanager_mod

# import for helper_functions uses google.cloud.sql.connector etc.; stub minimal
sql_connector_mod = types.ModuleType("google.cloud.sql.connector")
class _DummyConnector:  # noqa: D101
    def connect(self, *_, **__):
        return None
sql_connector_mod.Connector = _DummyConnector  # type: ignore[attr-defined]
sys.modules.setdefault("google.cloud.sql.connector", sql_connector_mod)

sql_pkg = types.ModuleType("google.cloud.sql")
sql_pkg.connector = sql_connector_mod
sys.modules.setdefault("google.cloud.sql", sql_pkg)
google_cloud.sql = sql_pkg

# Stub pandas & sqlalchemy same as gmail tests (lightweight)
pandas_stub = types.ModuleType("pandas")
class _DummyDF(list):
    def to_dict(self, orient="records"):
        return list(self)
pandas_stub.DataFrame = _DummyDF  # type: ignore[attr-defined]
pandas_stub.read_sql = lambda *a, **k: _DummyDF()  # type: ignore[attr-defined]
sys.modules.setdefault("pandas", pandas_stub)

sqlalchemy_stub = types.ModuleType("sqlalchemy")
sqlalchemy_stub.create_engine = lambda *a, **k: object()  # type: ignore[attr-defined]
sqlalchemy_stub.text = lambda q: q  # type: ignore[attr-defined]
sys.modules.setdefault("sqlalchemy", sqlalchemy_stub)

# ------------------------------ OpenAI stub --------------------------------

openai_stub = types.ModuleType("openai")
# Error sub-module with exception classes
error_mod = types.ModuleType("openai.error")
class RateLimitError(Exception):
    pass
class APIConnectionError(Exception):
    pass
class APIError(Exception):
    pass
class Timeout(Exception):
    pass
for exc_cls in [RateLimitError, APIConnectionError, APIError, Timeout]:
    setattr(error_mod, exc_cls.__name__, exc_cls)
openai_stub.error = error_mod
sys.modules["openai.error"] = error_mod

# These will be patched per-test to inject different behaviours
_openai_call_counter: dict[str, int] = {}

def make_openai_factory(create_func):
    """Return a factory emulating openai.OpenAI with custom create()."""
    class _FakeCompletions:  # noqa: D101
        def __init__(self, creator):
            self._creator = creator
        def create(self, **payload):  # noqa: D401
            return self._creator(**payload)
    class _FakeChat:  # noqa: D101
        def __init__(self, creator):
            self.completions = _FakeCompletions(creator)
    class _FakeClient:  # noqa: D101
        def __init__(self):
            self.chat = _FakeChat(create_func)
    return _FakeClient

openai_stub.api_key = None
openai_stub.OpenAI = make_openai_factory(lambda **p: {"ok": True})  # default no-op factory
# Fallback path for legacy SDK (<1.x)
class _LegacyChatComp:  # noqa: D101
    def create(self, **payload):
        return {"legacy": True}
openai_stub.ChatCompletion = types.SimpleNamespace(create=_LegacyChatComp().create)  # type: ignore[attr-defined]

sys.modules.setdefault("openai", openai_stub)

# ---------------------------------------------------------------------------
# Only now import the module under test
# ---------------------------------------------------------------------------

import importlib
app_openai_client = importlib.import_module("app.connections.openai_client")

# ---------------------------------------------------------------------------
# Pytest starts here
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402  (import after stubs)
import pytest  # noqa: E402


# ------------------------- private helper tests ----------------------------

def test_hash_messages_consistency():
    msgs = [{"role": "user", "content": "hello"}]
    h1 = app_openai_client._hash_messages(msgs)  # type: ignore[attr-defined]
    h2 = app_openai_client._hash_messages(msgs)  # type: ignore[attr-defined]
    assert h1 == h2
    # Altering content changes hash
    msgs2 = [{"role": "user", "content": "hello!"}]
    assert app_openai_client._hash_messages(msgs2) != h1  # type: ignore[attr-defined]


def test_get_model_hierarchy(monkeypatch):
    # param wins
    assert app_openai_client._get_model("foo") == "foo"  # type: ignore[attr-defined]
    # env var next
    monkeypatch.setenv("OPENAI_MODEL", "bar")
    assert app_openai_client._get_model(None) == "bar"  # type: ignore[attr-defined]
    # fallback default constant
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert app_openai_client._get_model(None) == app_openai_client._DEFAULT_MODEL  # type: ignore[attr-defined]


# ------------------------- chat_completion tests --------------------------

def _patch_openai(monkeypatch, create_impl):
    """Replace openai.OpenAI factory with one that uses *create_impl*."""
    factory_cls = make_openai_factory(create_impl)
    monkeypatch.setattr(openai_stub, "OpenAI", factory_cls)
    # Ensure client module picks new factory (no reload needed – attribute lookup each call)


def test_chat_completion_basic(monkeypatch):
    calls = []
    def _create(**payload):
        calls.append(payload)
        return {"resp": "ok"}
    _patch_openai(monkeypatch, _create)
    app_openai_client._CACHE.clear()  # ensure clean cache
    res = app_openai_client.chat_completion([{"role": "user", "content": "hi"}])
    assert res == {"resp": "ok"}
    assert len(calls) == 1


def test_chat_completion_cache(monkeypatch):
    call_count = {"n": 0}
    def _create(**payload):
        call_count["n"] += 1
        return {"resp": call_count["n"]}
    _patch_openai(monkeypatch, _create)
    app_openai_client._CACHE.clear()
    msgs = [{"role": "user", "content": "hi"}]
    first = app_openai_client.chat_completion(msgs, cache=True)
    second = app_openai_client.chat_completion(msgs, cache=True)
    assert first == second
    # Underlying OpenAI call executed only once due to cache
    assert call_count["n"] == 1


def test_chat_completion_stream(monkeypatch):
    def _create(**payload):
        # Ensure stream flag arrives correctly
        assert payload["stream"] is True
        return iter([{"delta": "chunk"}])
    _patch_openai(monkeypatch, _create)
    res_iter = app_openai_client.chat_completion([{"role": "user", "content": "hi"}], stream=True)
    assert iter(res_iter) is res_iter  # iterator returned unchanged
    assert list(res_iter) == [{"delta": "chunk"}]


def test_chat_completion_retry(monkeypatch):
    attempts = {"n": 0}
    def _create(**payload):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise openai_stub.error.RateLimitError("Too many requests")
        return {"ok": True}
    _patch_openai(monkeypatch, _create)
    # Patch time.sleep to avoid real waits
    monkeypatch.setattr(app_openai_client.time, "sleep", lambda *_: None)  # type: ignore[attr-defined]
    app_openai_client._CACHE.clear()
    result = app_openai_client.chat_completion([{"role": "user", "content": "hi"}], max_retries=5)
    assert result == {"ok": True}
    assert attempts["n"] == 3  # two failures + final success 