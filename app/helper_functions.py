import os
import pandas as pd
from sqlalchemy import create_engine, text
from google.cloud import logging as gcp_logging, secretmanager
from google.cloud.sql.connector import Connector
from contextlib import contextmanager


LOCAL_CREDS = os.getenv("LOCAL_CREDS")

if LOCAL_CREDS is not None:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = LOCAL_CREDS
    os.environ["GOOGLE_CLOUD_PROJECT"] = os.getenv("GOOGLE_CLOUD_PROJECT")


# ---------------------------------------------------------------------------
# Google Cloud Logging setup (mirrors pattern used in `verbs.py`)
# ---------------------------------------------------------------------------
_logging_client = gcp_logging.Client()
_logger_name = f"{os.getenv('ENV_NAME', 'dev')}_decentration_engine"
_gcp_logger = _logging_client.logger(_logger_name)
# Expose the convenience helper so callers can use `logging.log_text(...)`.
gcp_logging.log_text = _gcp_logger.log_text  # type: ignore[attr-defined]
# Re-export under the well-known name.
logging = gcp_logging


def get_secret_value(project_id, secret_id, version_id="latest"):
    """
    Retrieve a secret value from Google Cloud Secret Manager.

    This function accesses a secret stored in Google Cloud Secret Manager
    and returns its value as a string. It uses Application Default Credentials (ADC)
    from the environment for authentication.

    Parameters
    ----------
    project_id : str
        The Google Cloud project ID where the secret is stored
    secret_id : str
        The ID of the secret to retrieve
    version_id : str, optional
        The version of the secret to retrieve, defaults to "latest"

    Returns
    -------
    str
        The secret payload as a UTF-8 decoded string

    Notes
    -----
    Requires appropriate GCP permissions to access Secret Manager resources.
    """
    # Emit debug-level log – never include the secret payload itself.
    logging.log_text(
        f"Fetching secret '{secret_id}' from project '{project_id}' (version '{version_id}').",
        severity="DEBUG",
    )
    # Initialize Secret Manager client; uses ADC from the VM
    client = secretmanager.SecretManagerServiceClient()

    # Build the resource name of the secret version
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

    # Access the secret version
    response = client.access_secret_version(request={"name": name})

    # Secret payload
    payload = response.payload.data.decode("UTF-8")

    logging.log_text(f"Successfully fetched secret '{secret_id}'.", severity="INFO")
    return payload


SERVICE_ACCOUNT_NAME = os.getenv("API_DB_USER")
SERVICE_ACCOUNT_SECRET = get_secret_value(
    os.getenv("GOOGLE_CLOUD_PROJECT"), "decentration-engine-app-password"
)

DB_CONNECTION_STRING = "postgresql+pg8000://"


@contextmanager
def engine_scope():
    """
    Context manager that creates and safely disposes of a SQLAlchemy database engine.

    This context manager establishes a connection to the PostgreSQL database in
    Google Cloud SQL using the Connector library, creates a SQLAlchemy engine,
    and ensures the connection is properly closed and resources are released
    when the context exits.

    Yields
    ------
    sqlalchemy.engine.Engine
        A configured SQLAlchemy engine object that can be used for database operations

    Notes
    -----
    Uses credentials from environment variables and Google Cloud SQL Connector
    to establish the connection. The engine is properly disposed of when the
    context exits, which closes all open connections and releases the connection pool.
    """
    connector = Connector()
    engine = create_engine(
        DB_CONNECTION_STRING,
        creator=lambda: connector.connect(
            "EfithorZone:us-central1:decentration-engine-db",
            "pg8000",
            user=SERVICE_ACCOUNT_NAME,
            password=SERVICE_ACCOUNT_SECRET,
            db="postgres",
        ),
    )

    logging.log_text("Database engine created via Connector.", severity="DEBUG")
    try:
        yield engine
    finally:
        # Disposing the engine closes all open connections and releases the pool.
        engine.dispose()
        logging.log_text("Database engine disposed.", severity="DEBUG")


def run_query(query: str, params: dict | None = None):
    """
    Execute a parameterized SQL query as a Celery task and return the results.

    This Celery task function executes a SQL query against the database using
    the engine_scope context manager, converts the results to a pandas DataFrame,
    and returns the data as a dictionary format suitable for JSON serialization.

    Parameters
    ----------
    query : str
        The SQL query to execute, which may contain parameter placeholders
    params : dict, optional
        Dictionary of parameter values to bind to the query, defaults to None

    Returns
    -------
    list
        A list of dictionaries representing the query results, where each dictionary
        corresponds to a row and keys are column names

    Notes
    -----
    The query is converted to a SQLAlchemy text object before execution to
    support parameterization and safe query construction.
    """
    if params is None:
        params = {}

    # Log the incoming SQL query without exposing full text (may include PII).
    logging.log_text(
        f"Executing SQL query. Params provided: {list(params.keys())}",
        severity="INFO",
    )

    query = text(query)
    with engine_scope() as engine:
        try:
            df = pd.read_sql(query, engine, params=params)
        except Exception as exc:
            logging.log_text(f"Database query failed: {exc}", severity="ERROR")
            raise

        logging.log_text(f"Query returned {len(df)} rows.", severity="DEBUG")
        return df.to_dict("records")
