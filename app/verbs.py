from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from pathlib import Path
import os
from google.cloud import logging as gcp_logging

"""verbs.py – Action primitives for the Decentration Engine

This module exposes *verbs* – high-level actions that the agent can execute in
response to a user request or an autonomous trigger.  Verbs are intentionally
kept thin; they orchestrate calls to lower-level *connections* (OpenAI, vector
stores, etc.) and *inputs* (Gmail, Slack, …) without embedding business logic
in the application layer.

The first verb implemented is ``summarize_email``.  Given a look-back window
and an optional flag for *unread only*, the function retrieves recent e-mails
via :pymod:`app.inputs.gmail`, pipes the content through the configured LLM
connection, and returns a Markdown summary.
"""

# Local package imports – these modules are expected to be implemented
# elsewhere in the codebase.  They currently expose placeholder stubs so that
# importing them during early development does not raise runtime errors.
try:
    from app.inputs import gmail  # type: ignore
except ImportError:  # pragma: no cover – in case inputs package is renamed
    gmail = None  # type: ignore

try:
    from app.connections import openai_client  # type: ignore
except ImportError:  # pragma: no cover – handle missing dependency gracefully
    openai_client = None  # type: ignore

try:
    from app.inputs import twitter  # type: ignore
except ImportError:  # pragma: no cover – in case inputs package is renamed
    twitter = None  # type: ignore

__all__ = [
    "summarize_email",
    "summarize_tweets",
]


# ---------------------------------------------------------------------------
# Google Cloud Logging setup
# ---------------------------------------------------------------------------
# Provide a module-level logger that mirrors the behaviour of the
# stdlib ``logging`` calls, but routes everything to Google Cloud
# Logging via ``log_text``.  We expose the convenience attribute
# ``gcp_logging.log_text`` so call-sites can use the pattern
# ``logging.log_text(msg, severity="WARNING")`` as requested.

_logging_client = gcp_logging.Client()
_logger_name = f"{os.getenv('ENV_NAME', 'dev')}_decentration_engine"
_gcp_logger = _logging_client.logger(_logger_name)

# Attach the helper directly onto the imported module so that downstream
# code can call ``logging.log_text`` without needing explicit access to
# the instantiated logger.
gcp_logging.log_text = _gcp_logger.log_text  # type: ignore[attr-defined]

# Re-export under the well-known name expected by existing source files.
logging = gcp_logging


# ---------------------------------------------------------------------------
# Helper utilities (internal)
# ---------------------------------------------------------------------------


def _retrieve_emails(
    since: datetime,
    unread_only: bool = True,
) -> List[dict]:
    """Fetch e-mails from the Gmail adapter.

    The function delegates to :pyfunc:`app.inputs.gmail.query_emails`.  Until
    the Gmail adapter is fully implemented, we fall back to an empty list so
    that the verb can be imported without crashing test suites.
    """

    if gmail is None or not hasattr(gmail, "query_emails"):
        logging.log_text(
            "Gmail adapter is not ready – returning an empty result set.  "
            "Implement `query_emails` in `app.inputs.gmail` to enable the "
            "email summarisation verb.",
            severity="WARNING",
        )
        return []

    try:
        return gmail.query_emails(since=since, unread_only=unread_only)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover – defensive guardrail
        logging.log_text(
            f"Failed to fetch e-mails via Gmail adapter: {exc}",
            severity="ERROR",
        )
        return []


def _retrieve_tweets(
    *,
    accounts: List[str],
    since: datetime,
    max_results: int = 1000,
) -> List[dict]:
    """Fetch tweets from the Twitter adapter.

    Delegates to :pyfunc:`app.inputs.twitter.query_tweets`.  Returns an empty
    list when the adapter or its dependencies/credentials are not ready so
    that importing the verb does not raise during development or CI.
    """

    if twitter is None or not hasattr(twitter, "query_tweets"):
        logging.log_text(
            "Twitter adapter is not ready – returning an empty result set.  "
            "Implement `query_tweets` in `app.inputs.twitter` and ensure the "
            "Tweepy dependency & credentials are configured to enable the "
            "tweet summarisation verb.",
            severity="WARNING",
        )
        return []

    try:
        return twitter.query_tweets(
            accounts=accounts, since=since, max_results=max_results
        )  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover – defensive guardrail
        logging.log_text(
            f"Failed to fetch tweets via Twitter adapter: {exc}",
            severity="ERROR",
        )
        return []


def _summarise_with_llm(
    chunks: List[str],
    llm_connection: str | None = None,
) -> str:
    """Send the text dump to the LLM and return a Markdown summary.

    Parameters
    ----------
    chunks:
        A list of raw text.  The payload size should be
        kept within the model's context window; callers *should* pre-chunk or
        truncate if necessary.
    llm_connection:
        Name of the :pymod:`app.connections` module to route the call through.
        If ``None``, we default to the OpenAI client.
    """

    # Resolve backend – future implementations could support Anthropic, Azure
    # OpenAI, local models, etc.
    if llm_connection is None:
        client = openai_client  # default fallback
    else:
        try:
            from importlib import import_module

            client = import_module(f"app.connections.{llm_connection}")
        except ModuleNotFoundError as exc:  # pragma: no cover
            logging.log_text(
                f"LLM connection '{llm_connection}' not found: {exc}",
                severity="ERROR",
            )
            client = openai_client  # graceful fallback

    if client is None or not hasattr(client, "chat_completion"):
        logging.log_text(
            "LLM client does not expose `chat_completion`; returning a "
            "placeholder summary.",
            severity="ERROR",
        )
        return "(LLM back-end not configured – summary unavailable)"

    # NEW: Pull organisational context from YAML config files to enrich the prompt.
    config_context: str = ""
    try:
        config_dir = Path(__file__).resolve().parent.parent / "config"
        config_files = [
            ("objectives.yaml", "Objectives"),
            ("priorities.yaml", "Priorities"),
            ("the_future.yaml", "Outlook"),
            ("features_of_success.yaml", "Methodology"),
        ]
        parts: list[str] = []
        for fname, section_title in config_files:
            file_path = config_dir / fname
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as fp:
                        yaml_text = fp.read().strip()
                        parts.append(f"### {section_title}\n{yaml_text}")
                except Exception as exc:
                    logging.log_text(
                        f"Failed to read config file {fname}: {exc}",
                        severity="WARNING",
                    )
        if parts:
            config_context = "\n\n".join(parts)
    except Exception as exc:
        # Defensive: never let prompt construction crash the verb.
        logging.log_text(
            f"Unable to assemble config context for LLM prompt: {exc}",
            severity="ERROR",
        )
        config_context = ""

    # Assemble messages according to the OpenAI /chat/completions schema.
    system_prompt_parts: list[str] = [
        "You are a personal executive assistant for the user.",
        "When summarising content (e-mails, tweets, and other messages), surface insights that align with the user's objectives, priorities, outlook, and methodology, as described below.",
    ]
    if config_context:
        system_prompt_parts.append(config_context)
    # Original instructions for formatting remain, appended last so they are prominent.
    system_prompt_parts.append(
        "Provide the summary as a concise, actionable, and chronologically ordered Markdown bullet list. Highlight key decisions, deadlines, and open questions. Use rich formatting where helpful (e.g. `code` spans for dates, *italics* for emphasis)."
    )

    system_prompt: str = "\n\n".join(system_prompt_parts)

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": "\n\n---\n\n".join(chunks) or "No text found.",
        },
    ]

    try:
        response: str | dict = client.chat_completion(messages=messages)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover – keep demo resilient
        logging.log_text(
            f"LLM call failed: {exc}",
            severity="ERROR",
        )
        return "(LLM call failed – see logs for details)"

    # The exact response shape depends on the client wrapper.  We handle a few
    # common cases and fall back to a string conversion otherwise.
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        # OpenAI python-sdk style: {"choices": [{"message": {"content": "..."}}]}
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            logging.log_text(
                "Unexpected LLM response schema; using str() cast.",
                severity="WARNING",
            )
            return str(response)

    return str(response)


# ---------------------------------------------------------------------------
# Public API – verbs
# ---------------------------------------------------------------------------


def summarize_email(
    lookback_window: int = 24,
    *,
    unread_only: bool = True,
    llm_connection: Optional[str] = None,
) -> str:
    """Generate a summary of recent e-mails.

    Parameters
    ----------
    lookback_window:
        Number of *hours* to look back from *now* when querying e-mails.
    unread_only:
        If ``True`` (default), only include messages that are still unread.
    llm_connection:
        Identifier of the LLM connection module under :pymod:`app.connections`.
        When omitted, the OpenAI connection is used by default.

    Returns
    -------
    str
        A Markdown-formatted summary produced by the language model.
    """

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_window)

    emails: List[dict] = _retrieve_emails(since=since, unread_only=unread_only)

    # Extract human-readable chunks – we prefer full bodies but fall back to
    # subject + snippet where unavailable.
    chunks: List[str] = []
    for mail in emails:
        if "body" in mail and mail["body"]:
            chunks.append(mail["body"])
        else:
            subject = mail.get("subject", "(no subject)")
            snippet = mail.get("snippet", "")
            chunks.append(f"Subject: {subject}\nSnippet: {snippet}")

    summary = _summarise_with_llm(chunks, llm_connection=llm_connection)
    return summary


def summarize_tweets(
    accounts: List[str],
    lookback_window: int = 24,
    *,
    max_results: int = 1000,
    llm_connection: Optional[str] = None,
) -> str:
    """Generate a summary of recent tweets for the given accounts.

    Parameters
    ----------
    accounts:
        List of Twitter handles (without the leading '@').  At least one
        account must be provided.
    lookback_window:
        Number of *hours* to look back from *now* when querying tweets.
    max_results:
        Safety cap across *all* accounts combined.
    llm_connection:
        Identifier of the LLM connection module under :pymod:`app.connections`.
        When omitted, the OpenAI connection is used by default.

    Returns
    -------
    str
        A Markdown-formatted summary produced by the language model.
    """

    if not accounts:
        raise ValueError("`accounts` must contain at least one Twitter handle")

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_window)

    tweets: List[dict] = _retrieve_tweets(
        accounts=accounts, since=since, max_results=max_results
    )

    # Convert tweets into textual chunks.  We include author and ISO timestamp
    # metadata to help the LLM establish context and ordering.
    chunks: List[str] = []
    for tw in tweets:
        timestamp = tw.get("timestamp")
        ts_iso = (
            datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
            if timestamp
            else "(unknown time)"
        )
        author = tw.get("author", "unknown")
        text = tw.get("text", "")
        chunks.append(f"@{author} – {ts_iso}:\n{text}")

    summary = _summarise_with_llm(chunks, llm_connection=llm_connection)
    return summary
