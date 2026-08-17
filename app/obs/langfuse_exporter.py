"""
The Langfuse client, on the same terms as every other client in this project.

Built lazily and cached in `_cached_client` behind `langfuse_client()`, so importing the
app touches no network and starts no background threads - the rule `app/rag/embeddings.py`
set and `tests/test_langfuse_exporter.py` enforces by reloading this module with the
constructor sabotaged.

## Optional dependency, on purpose

`langfuse` is in the `observability` extra rather than the base dependencies. It is an
OpenTelemetry distribution and pulls a tree considerably larger than anything else here,
and `app/obs/tracing.py` is fully useful without it - the structured log is the floor,
this is the dashboard. So `import langfuse` happens **inside** the function, and a missing
package is a disabled exporter with one warning, not an ImportError at startup.

Install it with `uv sync --extra observability`, or leave it out and read the logs.

## Three states, one of which is not a failure

- **Not configured** (no keys): disabled, silently. This is the default and the state the
  test suite runs in - a project checkout has no Langfuse account, and nothing about that
  is worth warning over on every request.
- **Configured but not installed**: disabled, with one warning. Someone asked for
  something they did not install, which is worth saying exactly once.
- **Configured and installed**: enabled.

Flushing is left to Langfuse, which batches on a background thread and registers its own
`atexit` handler. Nothing here flushes per turn: that would put a network round trip in
the path of every answer to make a dashboard slightly fresher.
"""

import logging

from app.config import settings

logger = logging.getLogger("eo_rag.trace")

# The sentinel and the client are distinct: None means "not built yet", _DISABLED means
# "built, and the answer was no" - without which every request would retry the import.
_DISABLED = object()
_cached_client = None


def is_configured() -> bool:
    """Whether keys are present. Says nothing about whether the package is installed."""
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def langfuse_client():
    """The Langfuse client, or None when tracing is not exported anywhere."""
    global _cached_client

    if _cached_client is _DISABLED:
        return None
    if _cached_client is not None:
        return _cached_client

    if not settings.langfuse_enabled or not is_configured():
        _cached_client = _DISABLED
        return None

    try:
        from langfuse import Langfuse
    except ImportError:
        logger.warning(
            "LANGFUSE_PUBLIC_KEY is set but the langfuse package is not installed; "
            "traces stay in the log only. Install it with: uv sync --extra observability"
        )
        _cached_client = _DISABLED
        return None

    try:
        _cached_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:
        # A bad host or a malformed key must not stop the app from answering questions.
        logger.warning("could not build the Langfuse client; continuing", exc_info=True)
        _cached_client = _DISABLED
        return None

    return _cached_client
