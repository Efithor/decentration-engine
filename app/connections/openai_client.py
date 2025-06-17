"""openai_client.py – OpenAI API Wrapper

Purpose
-------
Encapsulates all interactions with the OpenAI REST API.  Centralizing the logic
makes it easier to swap out model endpoints, implement retry/backoff policies,
and add caching.

Features to add
---------------
* `chat_completion(messages: list[dict])` – thin wrapper around `/chat/completions`.
* Automatic exponential backoff on 429 / 5xx responses.
* Optional in-memory or Redis cache keyed on prompt hash.
* Streaming support.
* Model selection via environment variable (`OPENAI_MODEL`, default "gpt-4o").
"""

# TODO: Implement OpenAI client wrapper.
# ---------------------------------------------------------------------------
# OpenAI client implementation
# ---------------------------------------------------------------------------
from __future__ import annotations

# Standard library imports
import os
import time
import json
import hashlib
from google.cloud import logging as gcp_logging
from typing import Any, Callable, Dict, List, Optional, Union

# Third-party dependency – ensure it is installed in your environment.
try:
    import openai  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "The 'openai' package is required for app.connections.openai_client "
        "but is not installed. Install it via `pip install openai`."
    ) from exc

from app.helper_functions import get_secret_value  # Local import to avoid cycles

__all__ = [
    "chat_completion",
]

# ---------------------------------------------------------------------------
# Configuration & in-memory cache
# ---------------------------------------------------------------------------

_DEFAULT_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
_MAX_CACHE_SIZE: int = 256  # naive FIFO cache size
_CACHE: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Google Cloud Logging setup
# ---------------------------------------------------------------------------
_logging_client = gcp_logging.Client()
_logger_name = f"{os.getenv('ENV_NAME', 'dev')}_decentration_engine"
_gcp_logger = _logging_client.logger(_logger_name)
gcp_logging.log_text = _gcp_logger.log_text  # type: ignore[attr-defined]

# Re-export for use below
logging = gcp_logging

# ---------------------------------------------------------------------------
# OpenAI API key resolution (env var > Secret Manager > error)
# ---------------------------------------------------------------------------
# The OpenAI SDK looks for an ``OPENAI_API_KEY`` environment variable or the
# ``openai.api_key`` attribute. We first honor any key already present in the
# environment so local overrides (e.g., `.env` files) work as expected. When
# the variable is missing, we fall back to Google Secret Manager via the
# helper ``get_secret_value`` utility.

# Attempt 1 – environment variable implicitly handled by the OpenAI SDK.
_OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")

# Attempt 2 – fetch from Secret Manager when env var is absent.
if _OPENAI_API_KEY is None:  # pragma: no cover – requires GCP runtime
    try:
        _project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "EfithorZone")
        _secret_id = os.getenv("OPENAI_SECRET_ID", "decentration-engine-key")
        _OPENAI_API_KEY = get_secret_value(_project_id, _secret_id)
        logging.log_text(
            "Loaded OpenAI API key from Secret Manager.", severity="INFO"
        )
    except Exception as exc:  # pragma: no cover – network / IAM failures
        logging.log_text(
            f"Failed to retrieve OpenAI API key from Secret Manager: {exc}",
            severity="ERROR",
        )
        _OPENAI_API_KEY = None

# Propagate the key to the OpenAI SDK if we have one.
if _OPENAI_API_KEY:
    # New SDKs expose a global config object where ``api_key`` is respected by
    # the default client. For <1.x we assign the module attribute directly.
    openai.api_key = _OPENAI_API_KEY

def _hash_messages(messages: List[dict]) -> str:
    """Return a stable SHA-256 hash for a list of chat messages."""
    canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_model(model: Optional[str] = None) -> str:
    """Resolve the model name from *param* > env var > hard-coded default."""
    return model or _DEFAULT_MODEL


def _retry(
    fn: Callable[[], Any],
    *,
    max_retries: int = 6,
    backoff_factor: float = 2.0,
    initial_delay: float = 1.0,
) -> Any:
    """Retry helper with exponential back-off for transient OpenAI errors."""
    attempt = 0
    delay = initial_delay
    while True:
        try:
            return fn()
        except (
            getattr(openai.error, "RateLimitError", Exception),
            getattr(openai.error, "APIConnectionError", Exception),
            getattr(openai.error, "APIError", Exception),
            getattr(openai.error, "Timeout", Exception),
        ) as exc:  # pragma: no cover – best-effort mapping across SDK versions
            attempt += 1
            if attempt > max_retries:
                logging.log_text(
                    f"OpenAI request failed after {attempt} attempts",
                    severity="ERROR",
                )
                raise
            logging.log_text(
                f"OpenAI request failed ({exc}). Retrying in {delay:.1f}s (attempt {attempt}/{max_retries})…",
                severity="WARNING",
            )
            time.sleep(delay)
            delay *= backoff_factor


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chat_completion(
    messages: List[dict],
    *,
    model: Optional[str] = None,
    stream: bool = False,
    cache: bool = False,
    max_retries: int = 6,
    **kwargs: Any,
) -> Union[Dict[str, Any], str]:
    """Thin wrapper around the OpenAI *chat/completions* endpoint.

    Parameters
    ----------
    messages:
        List of chat messages following the OpenAI schema.
    model:
        Override the model name. Falls back to the ``OPENAI_MODEL`` env var or
        ``"gpt-4o"`` when omitted.
    stream:
        If ``True``, the call is forwarded with streaming enabled and the raw
        iterator is returned. Caching is disabled in streaming mode.
    cache:
        Enable naive in-memory caching keyed on a SHA-256 hash of *messages* and
        the model. Only effective when ``stream`` is ``False``.
    max_retries:
        Maximum number of automatic retries applied to transient 429/5xx errors.
    **kwargs:
        Additional parameters forwarded verbatim to the OpenAI SDK.

    Returns
    -------
    dict | str
        Raw response dictionary (or OpenAI iterator when ``stream=True``).
    """
    resolved_model = _get_model(model)

    payload: Dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "stream": stream,
        **kwargs,
    }

    # ---------------------------------------------------------------------
    # Optional cache – bypass when streaming is requested.
    # ---------------------------------------------------------------------
    cache_key = ""
    if cache and not stream:
        cache_key = f"{resolved_model}:{_hash_messages(messages)}"
        if cache_key in _CACHE:
            return _CACHE[cache_key]

    def _dispatch() -> Any:
        """Handle both legacy (<1.x) and modern (>=1.x) OpenAI SDKs."""
        if hasattr(openai, "OpenAI"):
            # SDK v1.^
            _client = openai.OpenAI()
            resp = _client.chat.completions.create(**payload)
            # .model_dump() is v1.7+; older versions expose .dict() instead.
            for attr in ("model_dump", "dict"):
                if hasattr(resp, attr):
                    return getattr(resp, attr)()
            return resp  # Fallback – unexpected SDK variant.
        else:  # pragma: no cover – legacy pathway
            return openai.ChatCompletion.create(**payload)  # type: ignore[call-arg]

    # Streaming responses cannot be retried mid-flight. We therefore wrap the
    # *creation* call in the retry helper and return the resulting iterator as-is.
    response = _retry(_dispatch, max_retries=max_retries)

    if cache and not stream:
        if len(_CACHE) >= _MAX_CACHE_SIZE:
            try:
                _CACHE.pop(next(iter(_CACHE)))  # FIFO eviction
            except StopIteration:  # pragma: no cover – shouldn't occur
                pass
        _CACHE[cache_key] = response

    return response
