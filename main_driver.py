from app import create_app
import os
import sys
from google.cloud import logging

LOCAL_CREDS = os.getenv("LOCAL_CREDS")

if LOCAL_CREDS is not None:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = LOCAL_CREDS
    os.environ["GOOGLE_CLOUD_PROJECT"] = "semianalysis-core"

# Set up logging
ENV_NAME = os.getenv("ENV_NAME", "dev")
LOG_NAME = f"{ENV_NAME}_business_helper"
logging_client = logging.Client()
logger = logging_client.logger(LOG_NAME)

# Log application startup
logger.log_text("Application starting up", severity="INFO")

FLASK_ENV = os.getenv("FLASK_ENV", "development").lower()
logger.log_text(f"Server starting in {FLASK_ENV} mode", severity="INFO")

PORT = int(os.getenv("PORT", 8080))

app = create_app()


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
