"""
Shared fixtures.

The one thing here is global by necessity: the rate limiter is application middleware, so
it applies to every request any test makes, and its window is shared across a whole
session because the middleware instance is built once when the app starts. Left on, the
HTTP test files would refuse each other's requests - twelve tests posting to /ask exceed
ten per minute, and which ones failed would depend on the order they ran in.

So it is off by default and `tests/test_ratelimit.py` turns it back on explicitly, which
is also the honest division: every other file is testing something the limiter is not
part of.
"""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def rate_limiting_off(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
