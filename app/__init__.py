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

try:
    from google.cloud import logging as gcloud_logging  # type: ignore
except ImportError:  # pragma: no cover – GCP library might be absent locally
    gcloud_logging = None  # type: ignore

# ---------------------------------------------------------------------------
# Environment & logging setup (executed at import time)
# ---------------------------------------------------------------------------

LOCAL_CREDS: str | None = os.getenv("LOCAL_CREDS")
if LOCAL_CREDS is not None:
    # Allow local development without gcloud auth dance
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", LOCAL_CREDS)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "decentration-engine")

# Configure a project-wide logger.  Prefer Google Cloud Logging where available
# for seamless integration with Cloud Run / GKE, but fall back to stdlib logging
# so that the code remains portable.
if gcloud_logging is not None:
    _logging_client = gcloud_logging.Client()
    _log_name = f"{os.getenv('ENV_NAME', 'dev')}_decentration_engine"
    logger = _logging_client.logger(_log_name)

    # Make sure Python warnings / logging end up in Cloud Logging, too.
    gcloud_logging.Client().setup_logging(log_level=logging.INFO)

    # Helper alias to align with the template API (logger.log_text).
    _log = logger.log_text  # type: ignore[attr-defined]
else:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s – %(message)s")
    logger = logging.getLogger("decentration-engine")

    def _log(message: str, *, severity: str = "INFO") -> None:  # noqa: D401 – simple helper
        """Mimic the google-cloud logger API when unavailable locally."""
        level = severity.upper()
        getattr(logger, level.lower(), logger.info)(message)

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
