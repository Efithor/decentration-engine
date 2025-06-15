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
import logging
import os

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
        logging.warning(
            "Google client libraries not available – Gmail adapter disabled."
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
            logging.warning("Failed to load cached Gmail credentials: %s", exc)
            creds = None

    # Refresh / create credentials when missing or expired.
    if creds is None or not creds.valid:
        if creds is not None and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request  # type: ignore

                creds.refresh(Request())  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover
                logging.warning(
                    "Failed to refresh Gmail token; falling back to re-auth: %s", exc
                )
                creds = None

        if creds is None:
            if not client_secret.exists():
                logging.error(
                    "Gmail client secret not found at %s.  Set GMAIL_CLIENT_SECRET env var or place the file manually.",
                    client_secret,
                )
                return None  # type: ignore[return-value]

            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)  # type: ignore[arg-type]
            creds = flow.run_local_server(port=0)  # type: ignore[assignment]
            # Persist the token for next time.
            try:
                with open(token_file, "w", encoding="utf-8") as fp:
                    fp.write(creds.to_json())  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover
                logging.warning("Could not save Gmail token to disk: %s", exc)

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover
        logging.error("Failed to build Gmail service: %s", exc)
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
# Public API
# ---------------------------------------------------------------------------


def query_emails(
    *,
    since: datetime,
    unread_only: bool = True,
    max_results: int = 100,
) -> List[Dict[str, Any]]:
    """Return messages received *after* ``since``.

    Parameters
    ----------
    since:
        The lower bound (UTC) timestamp; messages older than this value are
        ignored.
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

    service = _get_service()
    if service is None:  # dependency / credential issue – already logged
        return []

    # Compose Gmail query string.
    epoch_sec = int(since.timestamp())
    q_terms: list[str] = [f"after:{epoch_sec}"]
    if unread_only:
        q_terms.append("is:unread")
    q = " ".join(q_terms)

    messages: List[Dict[str, Any]] = []
    page_token: Optional[str] = None

    while True:
        try:
            resp = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=q,
                    pageToken=page_token,
                    maxResults=min(500, max_results - len(messages)),  # Gmail max
                )
                .execute()
            )
        except Exception as exc:  # pragma: no cover – network / quota errors
            logging.error("Gmail API messages.list failed: %s", exc)
            break

        ids = [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")

        # Fetch message details in batches (sequentially to keep things simple).
        for msg_id in ids:
            if len(messages) >= max_results:
                break
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
            except Exception as exc:  # pragma: no cover
                logging.warning("Could not fetch Gmail message %s: %s", msg_id, exc)
                continue

            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            snippet = msg.get("snippet", "")
            body_txt = _extract_plain_text(payload)

            messages.append(
                {
                    "id": msg_id,
                    "thread_id": msg.get("threadId"),
                    "timestamp": int(msg.get("internalDate", 0)) / 1000.0,
                    "subject": _get_header(headers, "Subject"),
                    "from": _get_header(headers, "From"),
                    "to": _get_header(headers, "To"),
                    "snippet": snippet,
                    "body": body_txt,
                }
            )

        if page_token is None or len(messages) >= max_results:
            break

    return messages
