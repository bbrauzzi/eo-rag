"""
Tests for the app object itself.

The static mount is the one thing here that can go wrong quietly: at "/" it catches every
path the router did not claim first, and it must not make anything - the local
`uvicorn --reload` workflow, or this suite - depend on a frontend build having been run.
"""

import importlib

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import main
from app.api.routes import router


def test_the_app_starts_and_answers_without_a_built_frontend():
    """
    Which is the situation of every checkout that has not run `npm run build`: the
    frontend is built in the image's node stage, never in the repository.
    """
    importlib.reload(main)

    client = TestClient(main.app)

    assert client.get("/health").json() == {"status": "ok"}
    # Nothing is mounted, so the root is simply not a route.
    assert client.get("/").status_code == 404


def test_the_mount_does_not_shadow_the_api(tmp_path):
    """
    Built the way main.py builds it - router first, mount second - because that order is
    the whole guarantee. Reversed, "/ask" would be looked for as a file and 404.
    """
    built = tmp_path / "frontend_dist"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html><title>EO Copilot</title>")

    app = FastAPI()
    app.include_router(router)
    app.mount("/", StaticFiles(directory=built, html=True), name="ui")

    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    # 422 and not 404: the router still owns these, the mount never sees them.
    assert client.post("/ask", json={}).status_code == 422
    assert client.post("/ask/stream", json={}).status_code == 422
    # And the page is there for everything else.
    assert "EO Copilot" in client.get("/").text
