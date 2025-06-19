"""Tests for `app.helper_functions`."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Provide minimal stubs for all heavy external dependencies required by
# helper_functions so we can import the module offline.
# ---------------------------------------------------------------------------

import sys
import types

# -------------------------- Google Cloud stubs -----------------------------

google_root = sys.modules.setdefault("google", types.ModuleType("google"))
google_cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
google_root.cloud = google_cloud

# google.cloud.logging ------------------------------------------------------
gcp_logging_mod = types.ModuleType("google.cloud.logging")

class _StubLogger:  # noqa: D101
    def __init__(self):
        self.records: list[tuple] = []
    def log_text(self, text: str, *, severity: str | None = None):  # noqa: D401
        self.records.append((text, severity))

class _StubLoggingClient:  # noqa: D101
    def logger(self, name):  # noqa: D401
        return _StubLogger()

gcp_logging_mod.Client = _StubLoggingClient  # type: ignore[attr-defined]

google_cloud.logging = gcp_logging_mod
sys.modules.setdefault("google.cloud.logging", gcp_logging_mod)

# google.cloud.secretmanager ------------------------------------------------
secretmanager_mod = types.ModuleType("google.cloud.secretmanager")

class _StubSecretManagerClient:  # noqa: D101
    def __init__(self):
        self.request_args: list[dict] = []
    class _Response:  # noqa: D101
        def __init__(self):
            import types as _types
            self.payload = _types.SimpleNamespace(data=b"stub-secret")
    def access_secret_version(self, request):  # noqa: D401
        self.request_args.append(request)
        return self._Response()

secretmanager_mod.SecretManagerServiceClient = _StubSecretManagerClient  # type: ignore[attr-defined]

google_cloud.secretmanager = secretmanager_mod
sys.modules.setdefault("google.cloud.secretmanager", secretmanager_mod)

# google.cloud.sql.connector -----------------------------------------------
sql_connector_mod = types.ModuleType("google.cloud.sql.connector")

class _StubConnector:  # noqa: D101
    def __init__(self):
        self.connect_args: list[tuple] = []
    def connect(self, *args, **kwargs):  # noqa: D401
        self.connect_args.append((args, kwargs))
        return None

sql_connector_mod.Connector = _StubConnector  # type: ignore[attr-defined]

google_cloud.sql = types.ModuleType("google.cloud.sql")
google_cloud.sql.connector = sql_connector_mod
sys.modules.setdefault("google.cloud.sql.connector", sql_connector_mod)
sys.modules.setdefault("google.cloud.sql", google_cloud.sql)

# ------------------------------ pandas stub --------------------------------
pandas_stub = types.ModuleType("pandas")

class _DummyDF(list):
    def to_dict(self, orient="records"):
        return list(self)

pandas_stub.DataFrame = _DummyDF  # type: ignore[attr-defined]

def _stub_read_sql(query, engine, params=None):  # noqa: D401
    # Return DataFrame with a single row for testing.
    return _DummyDF([{"foo": 1}])

pandas_stub.read_sql = _stub_read_sql  # type: ignore[attr-defined]

sys.modules.setdefault("pandas", pandas_stub)

# ----------------------------- sqlalchemy stub -----------------------------
sqlalchemy_stub = types.ModuleType("sqlalchemy")

class _StubEngine:  # noqa: D101
    def __init__(self):
        self.disposed = False
    def dispose(self):  # noqa: D401
        self.disposed = True


def _stub_create_engine(*args, **kwargs):  # noqa: D401
    return _StubEngine()

def _stub_text(query):  # noqa: D401
    return query

sqlalchemy_stub.create_engine = _stub_create_engine  # type: ignore[attr-defined]
sqlalchemy_stub.text = _stub_text  # type: ignore[attr-defined]

sys.modules.setdefault("sqlalchemy", sqlalchemy_stub)

# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

import importlib
helper_functions = importlib.import_module("app.helper_functions")

# ---------------------------------------------------------------------------
# Pytest
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

# ---------------------------- get_secret_value -----------------------------

def test_get_secret_value_returns_payload(monkeypatch):
    secret = helper_functions.get_secret_value("proj", "sec")
    assert secret == "stub-secret"

# --------------------------- engine_scope context --------------------------

def test_engine_scope_yields_and_disposes():
    with helper_functions.engine_scope() as engine:
        # Ensure we received the stub engine
        from sqlalchemy import create_engine  # type: ignore  # uses stub
        assert isinstance(engine, _StubEngine)
        # Use engine (no-op)
    # After context the engine should be disposed
    assert engine.disposed is True

# ------------------------------ run_query ----------------------------------

def test_run_query_success(monkeypatch):
    # Capture logging
    records: list[tuple] = []
    monkeypatch.setattr(helper_functions.logging, "log_text", lambda *args, **kw: records.append((args, kw)))

    result = helper_functions.run_query("SELECT 1", params=None)
    assert result == [{"foo": 1}]
    # log_text called at least twice (start + finish)
    assert len(records) >= 2


def test_run_query_failure(monkeypatch):
    # Monkeypatch pandas.read_sql to raise
    def _raise(*args, **kwargs):
        raise RuntimeError("DB fail")
    monkeypatch.setattr(sys.modules["pandas"], "read_sql", _raise)

    with pytest.raises(RuntimeError):
        helper_functions.run_query("SELECT 1") 