"""
twitter.py – X (Twitter) Input Adapter

Purpose
-------
Periodically queries the X (formerly Twitter) REST API v2 to retrieve recent
tweets from a set of accounts so that higher-level verbs can process them.
Similar to *gmail.py*, the adapter's public surface area is intentionally tiny
– currently a single :pyfunc:`query_tweets` helper – keeping the rest of the
code-base agnostic of vendor-specific details.

The function only requires *read-only* OAuth2 Bearer Token credentials. The
recommended approach is to create a *Project* & *App* in the X Developer
portal, enable *OAuth2 Machine-to-Machine* (or *App-only*), and copy the
*Bearer Token* string.

Design goals
------------
1. **Stateless API** – Can be called from synchronous contexts (CLI, cron,
   etc.) without background event-loops.
2. **Graceful degradation** – When dependencies or credentials are missing the
   adapter logs a warning and returns an empty list rather than crashing the
   application.
3. **Lightweight payloads** – Only essential metadata is returned; callers can
   query the full payload on demand using the tweet ``id``.

Credential workflow
-------------------
Set the Bearer Token via the ``X_BEARER_TOKEN`` environment variable (preferred)
or place it in ``~/.config/decentration/x_bearer_token.txt``.  The helper
:pyfunc:`_resolve_credential` searches both locations.

Example
-------
>>> from datetime import datetime, timedelta, timezone
>>> from app.inputs.twitter import query_tweets
>>> accounts = ["elonmusk", "TwitterDev"]
>>> yesterday = datetime.now(timezone.utc) - timedelta(days=1)
>>> tweets = query_tweets(accounts=accounts, since=yesterday)
>>> print(len(tweets))
42
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
import os
from google.cloud import logging as gcp_logging

# ---------------------------------------------------------------------------
# Optional Tweepy dependency (Twitter/X API v2).
# ---------------------------------------------------------------------------
try:
    import tweepy  # type: ignore
except ModuleNotFoundError:  # pragma: no cover – allow code-base to load sans deps
    tweepy = None  # type: ignore  # noqa: N816

__all__ = [
    "query_tweets",
]

# ---------------------------------------------------------------------------
# Configuration helpers & constants
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "decentration"
_DEFAULT_TOKEN_FILE = _DEFAULT_CONFIG_DIR / "x_bearer_token.txt"

# ---------------------------------------------------------------------------
# Google Cloud Logging setup
# ---------------------------------------------------------------------------
_logging_client = gcp_logging.Client()
_logger_name = f"{os.getenv('ENV_NAME', 'dev')}_decentration_engine"
_gcp_logger = _logging_client.logger(_logger_name)
gcp_logging.log_text = _gcp_logger.log_text  # type: ignore[attr-defined]

# Alias
logging = gcp_logging

def _resolve_credential() -> Optional[str]:
    """Return Bearer Token string or ``None`` when unavailable."""
    token = os.getenv("X_BEARER_TOKEN")
    if token:
        return token.strip()

    try:
        return _DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_client():  # pragma: no cover – external network calls are not unit-tested
    """Return an authorised *tweepy.Client* instance (v2)."""
    if tweepy is None:
        logging.log_text(
            "Tweepy not available – Twitter adapter disabled.",
            severity="WARNING",
        )
        return None  # type: ignore[return-value]

    bearer = _resolve_credential()
    if not bearer:
        logging.log_text(
            f"X API Bearer Token missing – set X_BEARER_TOKEN env var or place token in {_DEFAULT_TOKEN_FILE}",
            severity="WARNING",
        )
        return None  # type: ignore[return-value]

    try:
        client = tweepy.Client(bearer_token=bearer, wait_on_rate_limit=True)
    except Exception as exc:  # pragma: no cover – invalid token / network issue
        logging.log_text(
            f"Failed to create Tweepy client: {exc}",
            severity="ERROR",
        )
        return None  # type: ignore[return-value]

    return client


@lru_cache(maxsize=1024)
def _lookup_user_id(username: str) -> Optional[int]:
    """Return *numeric* user id for a given handle; ``None`` on failure."""
    client = _get_client()
    if client is None:
        return None

    try:
        resp = client.get_user(username=username)
    except Exception as exc:  # pragma: no cover – network / auth errors
        logging.log_text(
            f"Could not resolve X user '{username}': {exc}",
            severity="WARNING",
        )
        return None

    return resp.data.id if resp and resp.data else None  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def query_tweets(
    *,
    accounts: List[str],
    since: datetime,
    max_results: int = 1000,
) -> List[Dict[str, Any]]:
    """Return tweets *after* ``since`` UTC from the specified ``accounts``.

    Parameters
    ----------
    accounts:
        List of usernames (without leading '@').
    since:
        Lower bound (UTC) timestamp; tweets older than this value are ignored.
    max_results:
        Safety cap across *all* accounts combined.

    Returns
    -------
    List[Dict[str, Any]]
        Each dict exposes at least: ``id``, ``author``, ``timestamp`` (float
        epoch-seconds), and ``text``.
    """
    if since.tzinfo is None:
        raise ValueError("`since` datetime must be timezone-aware (UTC)")

    client = _get_client()
    if client is None:
        return []

    tweets: List[Dict[str, Any]] = []
    for username in accounts:
        if len(tweets) >= max_results:
            break

        user_id = _lookup_user_id(username)
        if user_id is None:
            continue

        # Pagination loop – Tweepy handles tokens internally when using Paginator.
        try:
            paginator = tweepy.Paginator(
                client.get_users_tweets,
                user_id,
                start_time=since.isoformat(),
                exclude=["retweets", "replies"],
                tweet_fields=["created_at", "text", "id"],
                max_results=100,  # API limit per request
            )
        except Exception as exc:  # pragma: no cover
            logging.log_text(
                f"Could not initiate tweet pagination for {username}: {exc}",
                severity="WARNING",
            )
            continue

        for page in paginator:
            if page.data is None:
                break
            for tweet in page.data:
                tweets.append(
                    {
                        "id": tweet.id,
                        "author": username,
                        "timestamp": tweet.created_at.replace(
                            tzinfo=timezone.utc
                        ).timestamp()  # type: ignore[union-attr]
                        if tweet.created_at
                        else None,
                        "text": tweet.text,
                    }
                )
                if len(tweets) >= max_results:
                    break
            if len(tweets) >= max_results:
                break

    tweets.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
    return tweets
