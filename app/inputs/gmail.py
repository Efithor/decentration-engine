"""
gmail.py – Gmail Input Adapter

Purpose
-------
Periodically queries the Gmail REST API to retrieve inbound and sent messages
so that higher-level verbs (e.g. :pyfunc:`app.verbs.summarize_email`) can
process them.  The adapter purposefully exposes a *very* small public surface
area – currently a single :pyfunc:`query_emails` helper – so that the rest of
the code-base remains decoupled from vendor-specific plumbing.

Only readonly access is required; the OAuth2 scope defaults to
``https://www.googleapis.com/auth/gmail.readonly``.

Design goals
------------
1. **Stateless API** – The function can be called ad-hoc from synchronous
   contexts (CLI, HTTP endpoints, cron jobs) without the need for a long-
   lived event loop or web-hooks.
2. **Graceful degradation** – When the Google client libraries are missing or
   credentials are not available the adapter logs a warning and returns an
   empty list instead of crashing the application.
3. **Lightweight payloads** – Only minimal metadata and a text/plain snippet
   are returned; consumers can fetch full MIME payloads on demand using the
   ``message_id``.

Credential workflow
-------------------
The adapter follows the standard desktop OAuth device-flow recommended by
Google for native / CLI apps:

1. Ensure the *client secret* JSON downloaded from Google Cloud Console is
   stored on disk (default location: ``~/.config/decentration/google_credentials.json``).
2. On first run a browser window will open prompting the user to authorise
   the application; the obtained refresh token is cached to
   ``~/.config/decentration/gmail_token.json`` for subsequent invocations.
3. Both paths can be customised via the ``GMAIL_CLIENT_SECRET`` and
   ``GMAIL_TOKEN_FILE`` environment variables respectively.

Example
-------
>>> from datetime import datetime, timedelta, timezone
>>> from app.inputs.gmail import query_emails
>>> yesterday = datetime.now(timezone.utc) - timedelta(days=1)
>>> mails = query_emails(since=yesterday, unread_only=True)
>>> print(len(mails))
6

"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Dict, Optional
import os
from google.cloud import logging as gcp_logging
import imaplib
import email
from email.header import decode_header
from app.helper_functions import get_secret_value

# ---------------------------------------------------------------------------
# Optional Google dependencies – make import optional so the rest of the code
# base can still be imported when the packages are not installed (e.g. during
# CI runs that do not require Gmail functionality).
# ---------------------------------------------------------------------------
try:
    # Official Google client libs.  Typed as "Any" to avoid enforcing the
    # dependency at static-type-checking time.
    from google.oauth2.credentials import Credentials  # type: ignore
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
except ModuleNotFoundError:  # pragma: no cover – allow code-base to load sans deps
    Credentials = None  # type: ignore  # noqa: N816 – keep original casing
    InstalledAppFlow = None  # type: ignore  # noqa: N816
    build = None  # type: ignore  # noqa: N816

__all__ = [
    "query_emails",
]

# ---------------------------------------------------------------------------
# Constants & configuration helpers
# ---------------------------------------------------------------------------
SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "decentration"
_DEFAULT_CLIENT_SECRET = _DEFAULT_CONFIG_DIR / "google_credentials.json"
_DEFAULT_TOKEN_FILE = _DEFAULT_CONFIG_DIR / "gmail_token.json"

# ---------------------------------------------------------------------------
# Google Cloud Logging setup
# ---------------------------------------------------------------------------
_logging_client = gcp_logging.Client()
_logger_name = f"{os.getenv('ENV_NAME', 'dev')}_decentration_engine"
_gcp_logger = _logging_client.logger(_logger_name)
gcp_logging.log_text = _gcp_logger.log_text  # type: ignore[attr-defined]

# Alias
logging = gcp_logging

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_credential_paths() -> tuple[Path, Path]:
    """Return (client_secret_path, token_path) considering env overrides."""
    client_secret = Path(os.getenv("GMAIL_CLIENT_SECRET", str(_DEFAULT_CLIENT_SECRET)))
    token_file = Path(os.getenv("GMAIL_TOKEN_FILE", str(_DEFAULT_TOKEN_FILE)))
    return client_secret, token_file


@lru_cache(maxsize=1)
def _get_service():  # pragma: no cover – external network calls are not unit-tested
    """Return an authorised Gmail *service* instance.

    The result is cached in-process because the underlying HTTP client is
    thread-safe and relatively expensive to build.
    """

    if Credentials is None or InstalledAppFlow is None or build is None:
        logging.log_text(
            "Google client libraries not available – Gmail adapter disabled."
            , severity="WARNING"
        )
        return None  # type: ignore[return-value]

    client_secret, token_file = _resolve_credential_paths()
    # Ensure config directory exists so we can persist the token file.
    token_file.parent.mkdir(parents=True, exist_ok=True)

    creds: Optional[Credentials] = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover – corrupted token?
            logging.log_text(
                f"Failed to load cached Gmail credentials: {exc}",
                severity="WARNING",
            )
            creds = None

    # Refresh / create credentials when missing or expired.
    if creds is None or not creds.valid:
        if creds is not None and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request  # type: ignore

                creds.refresh(Request())  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover
                logging.log_text(
                    f"Failed to refresh Gmail token; falling back to re-auth: {exc}",
                    severity="WARNING",
                )
                creds = None

        if creds is None:
            if not client_secret.exists():
                logging.log_text(
                    "Gmail client secret not found at %s.  Set GMAIL_CLIENT_SECRET env var or place the file manually.",
                    severity="ERROR",
                )
                return None  # type: ignore[return-value]

            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)  # type: ignore[arg-type]
            creds = flow.run_local_server(port=0)  # type: ignore[assignment]
            # Persist the token for next time.
            try:
                with open(token_file, "w", encoding="utf-8") as fp:
                    fp.write(creds.to_json())  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover
                logging.log_text(
                    f"Could not save Gmail token to disk: {exc}",
                    severity="WARNING",
                )

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover
        logging.log_text(
            f"Failed to build Gmail service: {exc}",
            severity="ERROR",
        )
        return None  # type: ignore[return-value]

    return service


def _get_header(headers: list[dict[str, str]], name: str) -> str:
    """Return the value of a specific RFC-2822 header from Gmail's header list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_plain_text(payload: dict[str, Any]) -> str:
    """Traverse the MIME tree and return the first text/plain part found."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain" and "data" in payload.get("body", {}):
        import base64

        data = payload["body"]["data"]
        try:
            decoded_bytes = base64.urlsafe_b64decode(data)
            return decoded_bytes.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover – defensively handle decode errors
            return ""

    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# IMAP helpers (new – prefer simple app-password auth over OAuth)
# ---------------------------------------------------------------------------


def _get_imap_client() -> imaplib.IMAP4_SSL | None:
    """Return an authenticated IMAP client or ``None`` on failure.

    The function attempts the following credential resolution strategy:

    1. ``GMAIL_APP_PASSWORD`` environment variable (highest precedence).
    2. Secret Manager – the secret name defaults to ``gmail-app-password`` but
       can be overridden via ``GMAIL_APP_PASSWORD_SECRET_ID``.
       Requires ``GOOGLE_CLOUD_PROJECT`` to be set for the current runtime.

    The Gmail username (e-mail address) **must** be supplied via the
    ``GMAIL_USERNAME`` environment variable.
    """

    user = os.getenv("GMAIL_USERNAME")
    if not user:
        logging.log_text(
            "GMAIL_USERNAME env var is missing – cannot authenticate to Gmail.",
            severity="ERROR",
        )
        return None

    # Resolve the app password – environment variable wins over Secret Manager.
    app_password = os.getenv("GMAIL_APP_PASSWORD")

    if not app_password:
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        secret_id = os.getenv("GMAIL_APP_PASSWORD_SECRET_ID", "gmail-app-password")

        if not project_id:
            logging.log_text(
                "GOOGLE_CLOUD_PROJECT not set and no GMAIL_APP_PASSWORD env – cannot fetch Gmail credentials.",
                severity="ERROR",
            )
            return None

        try:
            app_password = get_secret_value(project_id, secret_id)
        except Exception as exc:  # pragma: no cover – secret retrieval failed
            logging.log_text(
                f"Failed to retrieve Gmail app password from Secret Manager: {exc}",
                severity="ERROR",
            )
            return None

    try:
        client = imaplib.IMAP4_SSL("imap.gmail.com")
        client.login(user, app_password)
        return client  # type: ignore[return-value]
    except Exception as exc:  # pragma: no cover – auth/network failure
        logging.log_text(f"IMAP login failed: {exc}", severity="ERROR")
        return None


def _decode_mime_words(header_val: str) -> str:
    """Decode MIME-encoded words (e.g. =?utf-8?q?hello?=)."""

    decoded_fragments: list[str] = []
    for frag, encoding in decode_header(header_val):
        if isinstance(frag, bytes):
            try:
                decoded_fragments.append(frag.decode(encoding or "utf-8", errors="replace"))
            except Exception:  # pragma: no cover
                decoded_fragments.append(frag.decode("utf-8", errors="replace"))
        else:
            decoded_fragments.append(frag)
    return "".join(decoded_fragments)


def _extract_plain_text_email(msg: email.message.Message) -> str:
    """Return the first text/plain part from a MIME message (best effort)."""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disp:
                try:
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
                except Exception:
                    continue
    else:
        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            if payload is not None:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except Exception:
                    pass
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def query_emails(
    *,
    since: datetime,
    categories: Optional[List[str]] = None,
    unread_only: bool = True,
    max_results: int = 100,
) -> List[Dict[str, Any]]:
    """Return messages received *after* ``since``.

    Parameters
    ----------
    since:
        The lower bound (UTC) timestamp; messages older than this value are
        ignored.
    categories:
        Restrict results to the specified Gmail *tabs* / *categories* (same
        semantics as the web-search box, e.g. ``category:primary``).  When
        ``None`` (default) the adapter limits the query to *Primary* and
        *Updates* which are typically the most relevant for user mail.  Pass
        an empty list (``[]``) to disable category filtering entirely.
    unread_only:
        When ``True`` (default) restricts the query to *is:unread* messages.
    max_results:
        Safety cap to avoid accidentally pulling the entire mailbox on first
        run.  Gmail API enforces its own pagination limits – the adapter will
        follow *nextPageToken* until either the result cap is reached or no
        more pages are available.

    Returns
    -------
    List[Dict[str, Any]]
        Each element exposes at least the following keys: ``id``, ``threadId``,
        ``subject``, ``snippet``, and ``body`` (plain-text best-effort).  The
        structure purposefully mirrors the shape consumed by
        :pyfunc:`app.verbs.summarize_email`.
    """
    if since.tzinfo is None:
        raise ValueError("`since` datetime must be timezone-aware (UTC)")

    client = _get_imap_client()
    if client is None:
        # Errors already logged – keep outer API resilient.
        return []

    messages: List[Dict[str, Any]] = []

    # Gmail understands its standard search syntax via the X-GM-RAW extension.
    date_str = since.strftime("%Y/%m/%d")  # e.g. 2025/06/18

    q_terms: list[str] = [f"after:{date_str}"]
    if unread_only:
        q_terms.append("is:unread")

    # Category filtering – best-effort by adding the same raw query clauses.
    if categories is None:
        categories = ["primary", "updates"]

    if categories:
        cat_expr = " OR ".join(f"category:{c.lower()}" for c in categories)
        q_terms.append(f"({cat_expr})")

    raw_query = " ".join(q_terms)

    try:
        client.select("inbox")  # readonly is fine – Gmail ignores the flag.
        status, data = client.uid("SEARCH", "X-GM-RAW", f'"{raw_query}"')
        if status != "OK":
            logging.log_text(f"IMAP search failed: {status}", severity="ERROR")
            return []

        uid_list = data[0].split() if data and data[0] else []
        # Process newest first for parity with REST-based implementation.
        for uid in reversed(uid_list):
            if len(messages) >= max_results:
                break

            status, msg_data = client.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not msg_data or len(msg_data) < 1:
                continue

            raw_email = msg_data[0][1]
            try:
                msg = email.message_from_bytes(raw_email)
            except Exception:
                continue  # skip malformed messages

            # Header helpers.
            subject = _decode_mime_words(msg.get("Subject", ""))
            mail_from = _decode_mime_words(msg.get("From", ""))
            mail_to = _decode_mime_words(msg.get("To", ""))

            body_txt = _extract_plain_text_email(msg)
            snippet = (body_txt.strip().replace("\n", " ")[:120]) if body_txt else ""

            # Parse internal date.
            date_hdr = msg.get("Date")
            try:
                from email.utils import parsedate_to_datetime

                dt_obj = parsedate_to_datetime(date_hdr) if date_hdr else None
                ts = dt_obj.timestamp() if dt_obj else 0.0
            except Exception:
                ts = 0.0

            messages.append(
                {
                    "id": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "thread_id": None,
                    "timestamp": ts,
                    "subject": subject,
                    "from": mail_from,
                    "to": mail_to,
                    "snippet": snippet,
                    "body": body_txt,
                }
            )
    finally:
        try:
            client.logout()
        except Exception:
            pass

    return messages
