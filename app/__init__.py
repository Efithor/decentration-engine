"""
Decentration Engine – Agentic AI Life Organizer

This package houses the web application that orchestrates the life-management
agent.  All concrete logic is intentionally deferred; the goal of this file is
only to expose the symbols expected by other parts of the codebase and to act
as an architectural sign-post for future contributors.
"""

# ---------------------------------------------------------------------------
# Imports (kept to an absolute minimum – pull heavy deps only when implemented)
# ---------------------------------------------------------------------------
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Third-party dependencies
# ---------------------------------------------------------------------------
import os
import logging

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ---------------------------------------------------------------------------
# Environment & logging setup (executed at import time)
# ---------------------------------------------------------------------------

# NOTE:
# The Google Cloud Logger instance is created centrally in ``main_driver.py``
# and passed down to :pyfunc:`create_app` during initialisation.  This module
# therefore **must not** instantiate its own Cloud Logging client.  Instead we
# provide a lightweight "_log" helper that delegates to the supplied logger
# when available or falls back to the stdlib ``logging`` package otherwise.

# Keep LOCAL_CREDS convenience shim for local development (no auth dance).
LOCAL_CREDS: str | None = os.getenv("LOCAL_CREDS")
if LOCAL_CREDS is not None:
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", LOCAL_CREDS)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "decentration-engine")

# ---------------------------------------------------------------------------
# Internal logging facade – resolved at *runtime* in create_app.
# ---------------------------------------------------------------------------

# Forward declaration so that type-checkers are happy.
from typing import Callable  # noqa: E402  – runtime import order is fine here

_log: Callable[[str], None]


def _default_log(message: str, *, severity: str = "INFO") -> None:  # noqa: D401
    """Fallback logger that writes to the stdlib root logger."""
    level = severity.upper()
    getattr(logging, level.lower(), logging.info)(message)


# Initialise the helper with the default implementation; it will be *patched*
# in ``create_app`` when a Google Cloud logger is supplied by the caller.
_log = _default_log

# Log module import (occurs once per worker)
_log("Flask application factory module imported", severity="INFO")

# ---------------------------------------------------------------------------
# Rate limiter – instantiated at module level to avoid circular imports
# ---------------------------------------------------------------------------

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(*_: Any, **__: Any) -> Flask:  # type: ignore[override]
    """Create and configure the Flask application instance.

    The function keeps the public signature flexible (\*args, \*\*kwargs) so that future
    extensions – for example, dynamic config injection – do not break callers.
    """

    # Optional positional / keyword arguments can include a Google Cloud
    # ``logger`` object. Accept it via \*args / \*\*kwargs to avoid a breaking
    # change for existing import paths while still enabling dependency
    # injection from ``main_driver``.

    gcp_logger = None
    if _ and hasattr(_[0], "log_text"):
        # Assume first positional arg is the logger.
        gcp_logger = _[0]
    elif "logger" in __ and hasattr(__["logger"], "log_text"):
        gcp_logger = __["logger"]

    # Patch the module-level _log helper *once* so that downstream importers
    # (e.g. blueprints) pick up the correct implementation.
    global _log  # noqa: PLW0603 – intentional global state
    if gcp_logger is not None:
        _log = lambda msg, *, severity="INFO": gcp_logger.log_text(  # type: ignore  # noqa: E731
            msg, severity=severity.upper()
        )
    else:
        # Keep default stdlib logger.
        _log = _default_log

    # ---------------------------------------------------------------------
    # Initialise base Flask app
    # ---------------------------------------------------------------------
    app = Flask(__name__)

    # Attach the rate limiter after app creation
    limiter.init_app(app)

    # ---------------------------------------------------------------------
    # Health check route – required by Cloud Run / load-balancers
    # ---------------------------------------------------------------------
    @app.route("/", methods=["GET"])
    @limiter.exempt  # noqa: WPS437 – third-party decorator
    def health_check():  # type: ignore[return-value]
        """Light-weight liveness probe endpoint."""
        return jsonify({"status": "ok"}), 200

    # ---------------------------------------------------------------------
    # Rate-limit error handler – converts 429 into JSON response & structured log
    # ---------------------------------------------------------------------
    @app.errorhandler(429)  # type: ignore[arg-type]
    def _ratelimit_handler(error):  # noqa: D401 – internal handler
        client_ip = request.remote_addr or "unknown"
        user_agent = request.headers.get("User-Agent", "Unknown")
        _log(
            f"Rate limit exceeded: {error} – IP: {client_ip}, User-Agent: {user_agent}",
            severity="WARNING",
        )
        return (
            jsonify({
                "status": "error",
                "message": "Rate limit exceeded. Please try again later.",
            }),
            429,
        )

    # ---------------------------------------------------------------------
    # Future blueprint registrations / CLI hooks live here
    # ---------------------------------------------------------------------
    # Example (kept commented until the modules land):
    # from app.api import api_bp
    # app.register_blueprint(api_bp)

    _log("Flask application initialised", severity="INFO")
    return app
