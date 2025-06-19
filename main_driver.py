from app import create_app
import os
import sys
from google.cloud import logging
import logging as pylogging

LOCAL_CREDS = os.getenv("LOCAL_CREDS")

if LOCAL_CREDS is not None:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = LOCAL_CREDS
    os.environ["GOOGLE_CLOUD_PROJECT"] = os.getenv("GOOGLE_CLOUD_PROJECT")

# Set up logging
ENV_NAME = os.getenv("ENV_NAME", "dev")
LOG_NAME = f"{ENV_NAME}_decentration_engine"
logging_client = logging.Client()
logger = logging_client.logger(LOG_NAME)

# ---------------------------------------------------------------------------
# Google Cloud Logging – centralised configuration
# ---------------------------------------------------------------------------

class CloudLoggingHandler(pylogging.Handler):
    """Stdlib logging handler that forwards records to Google Cloud Logging."""

    def __init__(self, gcp_logger):  # noqa: D401 – simple pass-through
        super().__init__()
        self._gcp_logger = gcp_logger

    def emit(self, record: pylogging.LogRecord) -> None:  # noqa: D401
        try:
            msg = self.format(record)
            severity = record.levelname.upper()
            self._gcp_logger.log_text(msg, severity=severity)
        except Exception:  # pragma: no cover – never let logging crash the app
            # Fallback to default error handling (stderr).
            super().handleError(record)

# ---------------------------------------------------------------------
# Attach Cloud Logging handler to *root* logger so that modules using the
# stdlib ``logging`` API are transparently forwarded to GCP.
# ---------------------------------------------------------------------

_handler = CloudLoggingHandler(logger)
_handler.setFormatter(
    pylogging.Formatter("%(asctime)s %(levelname)s %(name)s – %(message)s")
)

root_logger = pylogging.getLogger()
root_logger.setLevel(pylogging.INFO)
root_logger.addHandler(_handler)

# Log application startup
logger.log_text("Application starting up", severity="INFO")

FLASK_ENV = os.getenv("FLASK_ENV", "development").lower()
logger.log_text(f"Server starting in {FLASK_ENV} mode", severity="INFO")

PORT = int(os.getenv("PORT", 8080))

# Pass the Google Cloud logger to the Flask factory
app = create_app(logger)


def run_server() -> None:
    """
    Run the appropriate web server based on the environment configuration.

    This function determines the current environment from environment variables
    and starts either a production Gunicorn server with multiple workers or a
    development Flask server with debug enabled.

    For production, it programmatically configures and starts Gunicorn with
    appropriate settings for load handling and timeout. For development,
    it starts the Flask development server with debug mode enabled.

    Environment Variables
    --------------------
    FLASK_ENV : str
        Environment setting that determines which server to run.
        Values can be "production" or anything else (treated as development)

    Notes
    -----
    Production settings include 4 workers and a 120-second timeout to
    accommodate longer-running requests like data processing tasks.
    Development server runs with debug=True for auto-reloading and
    detailed error messages.
    """

    if FLASK_ENV == "production":
        # -----------------------------
        # Start Gunicorn programmatically
        # -----------------------------
        # Import in a more testable way - allows direct patching of wsgi_app
        from gunicorn.app.wsgiapp import run

        sys.argv = [
            "gunicorn",
            "main_driver:app",  # The WSGI entrypoint (module:variable)
            "--bind",
            f"0.0.0.0:{PORT}",
            "--workers",
            "1",
            "--timeout",
            "120",
        ]
        run()  # This will block until Gunicorn exits
    else:
        # -----------------------------
        # Start the Flask development server
        # -----------------------------
        app.run(host="0.0.0.0", port=PORT, debug=True)


if __name__ == "__main__":
    run_server()
