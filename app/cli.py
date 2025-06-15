"""cli.py – Decentration Engine Command-Line Interface

This module exposes a Click-based CLI for interacting with the Decentration
Engine either *locally* (by directly invoking python verbs) or *remotely* via
HTTP calls to a running Flask/Gunicorn server.

Usage examples
--------------
# Local execution – call the verb in-process
$ python -m app.cli summary                            # 24-hour window
$ python -m app.cli summary -w 72 --include-read       # 72-hour window

# Remote execution – forward the request over HTTP
$ python -m app.cli --api-url http://localhost:8000 summary

Environment variables
---------------------
DECEN_API_URL  If set, acts like the --api-url option (handy for scripts).
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import json
import logging

import click

# Local verbs
from app.verbs import summarize_email, summarize_tweets

# ---------------------------------------------------------------------------
# HTTP helper (remote execution)
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: Dict[str, Any]) -> Any:  # pragma: no cover
    """POST a JSON payload and return the decoded JSON response.

    This tiny helper is only used when the CLI is pointed at a running HTTP
    back-end (e.g. the Flask/Gunicorn service).  We *intentionally* import the
    heavyweight `requests` dependency lazily so that users executing the CLI
    locally do not need it installed.
    """

    try:
        import requests  # pylint: disable=import-error
    except ImportError as exc:  # pragma: no cover – guidance for user
        raise click.ClickException(
            "Remote execution requires the 'requests' package. Install it via "
            "`pip install requests` or omit --api-url to run locally."
        ) from exc

    logging.debug("POST %s – payload size: %d bytes", url, len(json.dumps(payload)))
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:  # pragma: no cover – network issues
        raise click.ClickException(f"HTTP call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Click entry-point
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--api-url",
    envvar="DECEN_API_URL",
    default=None,
    metavar="URL",
    help="If provided, CLI commands are forwarded to the HTTP API at this URL "
    "instead of running locally.",
)
@click.pass_context
def cli(ctx: click.Context, api_url: Optional[str]):  # noqa: D401 – Click callback
    """Decentration Engine command-line interface."""

    # Stash the server URL in Click's context object for downstream commands.
    ctx.obj = {"api_url": api_url}


# ---------------------------------------------------------------------------
# `summary` command – e-mail summarisation
# ---------------------------------------------------------------------------


@cli.command("summary", help="Summarise recent e-mails using the configured LLM.")
@click.option(
    "-w",
    "--lookback-window",
    type=int,
    default=24,
    show_default=True,
    help="How many *hours* to look back when querying e-mails.",
)
@click.option(
    "--include-read/--unread-only",
    default=False,
    help="Include e-mails that are already read (default: unread only).",
)
@click.option(
    "--llm-connection",
    default=None,
    metavar="MODULE",
    help="LLM connection module under app.connections/ to route the call through.",
)
@click.pass_context
def summary_command(
    ctx: click.Context,
    lookback_window: int,
    include_read: bool,
    llm_connection: Optional[str],
) -> None:  # noqa: D401 – Click callback
    """Generate a Markdown summary of recent e-mails."""

    unread_only = not include_read
    api_url: Optional[str] = ctx.obj.get("api_url") if ctx.obj else None

    if api_url:
        # Remote execution – POST to the server's REST endpoint.
        payload = {
            "lookback_window": lookback_window,
            "unread_only": unread_only,
            "llm_connection": llm_connection,
        }
        endpoint = api_url.rstrip("/") + "/v1/summary/email"  # exemplary route
        result = _post_json(endpoint, payload)
        click.echo(result)
    else:
        # Local execution – call the Python verb directly.
        result = summarize_email(
            lookback_window=lookback_window,
            unread_only=unread_only,
            llm_connection=llm_connection,
        )
        click.echo(result)


# ---------------------------------------------------------------------------
# `tweets` command – tweet summarisation
# ---------------------------------------------------------------------------


@cli.command("tweets", help="Summarise recent tweets using the configured LLM.")
@click.option(
    "-a",
    "--account",
    "accounts",
    multiple=True,
    required=True,
    metavar="HANDLE",
    help="Twitter handle(s) (without '@'). Can be provided multiple times.",
)
@click.option(
    "-w",
    "--lookback-window",
    type=int,
    default=24,
    show_default=True,
    help="How many *hours* to look back when querying tweets.",
)
@click.option(
    "--max-results",
    type=int,
    default=1000,
    show_default=True,
    help="Maximum number of tweets to retrieve across all accounts.",
)
@click.option(
    "--llm-connection",
    default=None,
    metavar="MODULE",
    help="LLM connection module under app.connections/ to route the call through.",
)
@click.pass_context
def tweets_command(
    ctx: click.Context,
    accounts: tuple[str, ...],
    lookback_window: int,
    max_results: int,
    llm_connection: Optional[str],
) -> None:  # noqa: D401 – Click callback
    """Generate a Markdown summary of recent tweets."""

    api_url: Optional[str] = ctx.obj.get("api_url") if ctx.obj else None
    accounts_list = list(accounts)

    if api_url:
        # Remote execution – POST to the server's REST endpoint.
        payload = {
            "accounts": accounts_list,
            "lookback_window": lookback_window,
            "max_results": max_results,
            "llm_connection": llm_connection,
        }
        endpoint = api_url.rstrip("/") + "/v1/summary/twitter"  # exemplary route
        result = _post_json(endpoint, payload)
        click.echo(result)
    else:
        # Local execution – call the Python verb directly.
        result = summarize_tweets(
            accounts=accounts_list,
            lookback_window=lookback_window,
            max_results=max_results,
            llm_connection=llm_connection,
        )
        click.echo(result)


# ---------------------------------------------------------------------------
# Entry-point shim for `python -m app.cli`
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover – manual execution shortcut
    cli()  # pylint: disable=no-value-for-parameter
