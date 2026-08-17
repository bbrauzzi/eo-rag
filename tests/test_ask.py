"""
Tests for the /ask endpoint: HTTP concerns only.

The route is a thin adapter over the graph, so the graph is faked here and its own
behaviour is covered in tests/test_agent_graph.py. No database, no network.
"""

import pytest
from fastapi.testclient import TestClient

from app.agents.graph import Answer, ConversationBudgetExceeded
from app.api import routes
from app.api.assets import Asset, AssetDownload
from app.db.session import get_db
from app.main import app


@pytest.fixture
def db_sentinel():
    """Replaces the real session dependency; nothing ever calls it."""
    sentinel = object()
    app.dependency_overrides[get_db] = lambda: sentinel
    yield sentinel
    app.dependency_overrides.clear()


@pytest.fixture
def client(db_sentinel):
    return TestClient(app)


@pytest.fixture
def fake_graph(monkeypatch):
    """Patches answer_question() and records what the route passes to it."""

    def _install(answer=None, error=None):
        calls: list[dict] = []

        def _answer_question(db, question, conversation_id=None):
            calls.append(
                {"db": db, "question": question, "conversation_id": conversation_id}
            )
            if error is not None:
                raise error
            return answer or Answer(
                text="An answer.",
                sources=["stac-spec.md"],
                steps=1,
                conversation_id=conversation_id or "generated-id",
            )

        monkeypatch.setattr(routes, "answer_question", _answer_question)
        return calls

    return _install


def test_health():
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_returns_the_answer_its_sources_and_the_conversation(client, fake_graph):
    fake_graph(
        Answer(
            text="STAC Items are GeoJSON.",
            sources=["a.md", "b.md"],
            steps=2,
            conversation_id="c1",
        )
    )

    body = client.post("/ask", json={"question": "What are STAC Items?"}).json()

    assert body == {
        "answer": "STAC Items are GeoJSON.",
        "sources": ["a.md", "b.md"],
        "conversation_id": "c1",
    }


def test_the_graph_receives_the_question_and_the_session(
    client, fake_graph, db_sentinel
):
    calls = fake_graph()

    client.post("/ask", json={"question": "What are STAC Items?"})

    assert calls == [
        {"db": db_sentinel, "question": "What are STAC Items?", "conversation_id": None}
    ]


def test_a_conversation_id_is_passed_through(client, fake_graph):
    """How a follow-up question is attached to the conversation it belongs to."""
    calls = fake_graph()

    body = client.post(
        "/ask", json={"question": "And Collections?", "conversation_id": "c7"}
    ).json()

    assert calls[0]["conversation_id"] == "c7"
    assert body["conversation_id"] == "c7"


def test_omitting_the_conversation_id_still_returns_one(client, fake_graph):
    fake_graph()

    assert client.post("/ask", json={"question": "q"}).json()["conversation_id"]


def test_step_count_is_not_part_of_the_response(client, fake_graph):
    """steps is there for observability, not for the API contract."""
    fake_graph(Answer(text="a", sources=[], steps=4, conversation_id="c1"))

    assert set(client.post("/ask", json={"question": "q"}).json()) == {
        "answer",
        "sources",
        "conversation_id",
    }


def test_an_answer_with_no_sources_is_valid(client, fake_graph):
    fake_graph(
        Answer(
            text="I could not find anything.", sources=[], steps=1, conversation_id="c1"
        )
    )

    assert client.post("/ask", json={"question": "q"}).json()["sources"] == []


def test_missing_question_is_rejected(client):
    assert client.post("/ask", json={}).status_code == 422


# --- the conversation budget ------------------------------------------------


def test_a_conversation_over_its_budget_is_a_429(client, fake_graph):
    """
    429 and not 400 or 500: the request is well formed and would have been served a few
    turns ago. Nothing went wrong - a limit was enforced.
    """
    fake_graph(
        error=ConversationBudgetExceeded(
            "This conversation has reached its limit of 20 turns. Start a new one to continue."
        )
    )

    response = client.post("/ask", json={"question": "q", "conversation_id": "spent"})

    assert response.status_code == 429
    # The message says what to do about it, so it goes to the client rather than a log.
    assert "Start a new one" in response.json()["detail"]


def test_the_budget_refusal_does_not_surface_as_a_500(client, fake_graph):
    """It is a RuntimeError subclass, so nothing else may catch it as one first."""
    fake_graph(error=ConversationBudgetExceeded("spent"))

    assert client.post("/ask", json={"question": "q"}).status_code != 500


# --- the preview proxy ------------------------------------------------------


@pytest.fixture
def fake_preview(monkeypatch):
    """Patches fetch_preview(); the module's own behaviour is in tests/test_preview.py."""

    def _install(result=(b"\xff\xd8jpeg", "image/jpeg"), error=None):
        calls: list[str] = []

        def _fetch_preview(item_id):
            calls.append(item_id)
            if error is not None:
                raise error
            return result

        monkeypatch.setattr(routes, "fetch_preview", _fetch_preview)
        return calls

    return _install


def test_a_preview_comes_back_as_the_image_itself(client, fake_preview):
    calls = fake_preview()

    response = client.get("/preview/S2B_33TTG_20240130_0_L2A")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"\xff\xd8jpeg"
    assert calls == ["S2B_33TTG_20240130_0_L2A"]


def test_a_preview_is_cacheable_because_it_is_now_our_own_origin(client, fake_preview):
    fake_preview()

    assert "max-age" in client.get("/preview/S2B_test").headers["cache-control"]


def test_an_item_id_with_a_slash_does_not_escape_the_route(client, fake_preview):
    """The path parameter is one segment: nothing here can be steered elsewhere."""
    calls = fake_preview()

    assert client.get("/preview/a/b").status_code == 404
    assert calls == []


def test_no_such_item_or_no_preview_is_a_404(client, fake_preview):
    fake_preview(error=ValueError("Item 'nope' carries no preview image"))

    response = client.get("/preview/nope")

    assert response.status_code == 404
    assert "no preview image" in response.json()["detail"]


def test_an_unreachable_asset_host_is_a_502_not_a_500(client, fake_preview):
    """It is upstream that failed, and the message says which upstream."""
    fake_preview(error=RuntimeError("Preview for S2B_test unreachable: no route"))

    response = client.get("/preview/S2B_test")

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


# --- the asset downloads ----------------------------------------------------


@pytest.fixture
def fake_assets(monkeypatch):
    """Patches both entry points; the module's behaviour is in tests/test_assets.py."""

    def _install(listed=None, download=None, list_error=None, open_error=None):
        calls: list[tuple] = []

        def _list_assets(item_id):
            calls.append(("list", item_id))
            if list_error is not None:
                raise list_error
            return (
                listed
                if listed is not None
                else [
                    Asset(
                        "red",
                        "Red - 10m",
                        "image/tiff",
                        ["data"],
                        "https://cogs.test/B04.tif",
                    )
                ]
            )

        def _open_asset(item_id, asset_key):
            calls.append(("open", item_id, asset_key))
            if open_error is not None:
                raise open_error
            return download or AssetDownload(
                iter([b"II*\x00", b"tif"]), "image/tiff", "S2B_test_red.tif", 7
            )

        monkeypatch.setattr(routes, "list_assets", _list_assets)
        monkeypatch.setattr(routes, "open_asset", _open_asset)
        return calls

    return _install


def test_the_assets_of_an_item_are_listed_by_key(client, fake_assets):
    calls = fake_assets()

    response = client.get("/items/S2B_test/assets")

    assert response.status_code == 200
    assert response.json() == [
        {
            "key": "red",
            "title": "Red - 10m",
            "type": "image/tiff",
            "roles": ["data"],
            "href": "https://cogs.test/B04.tif",
        }
    ]
    assert calls == [("list", "S2B_test")]


def test_an_asset_comes_back_as_a_named_download(client, fake_assets):
    """The filename is the point: every scene's red band is otherwise B04.tif."""
    calls = fake_assets()

    response = client.get("/items/S2B_test/assets/red")

    assert response.status_code == 200
    assert response.content == b"II*\x00tif"
    assert response.headers["content-type"] == "image/tiff"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="S2B_test_red.tif"'
    )
    assert response.headers["content-length"] == "7"
    assert calls == [("open", "S2B_test", "red")]


def test_an_asset_of_unknown_length_carries_no_content_length(client, fake_assets):
    """A wrong progress bar is worse than none; the browser gets a spinner instead."""
    fake_assets(
        download=AssetDownload(iter([b"tif"]), "image/tiff", "S2B_test_red.tif", None)
    )

    response = client.get("/items/S2B_test/assets/red")

    assert response.headers.get("content-length") is None


def test_an_unknown_item_or_asset_key_is_a_404(client, fake_assets):
    fake_assets(open_error=ValueError("Item 'S2B_test' has no asset 'nir'"))

    response = client.get("/items/S2B_test/assets/nir")

    assert response.status_code == 404
    assert "no asset 'nir'" in response.json()["detail"]


def test_a_failing_asset_host_is_a_502_not_a_500(client, fake_assets):
    fake_assets(
        open_error=RuntimeError("Asset 'red' of S2B_test unreachable: no route")
    )

    response = client.get("/items/S2B_test/assets/red")

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


def test_a_failing_catalog_on_the_listing_is_a_502(client, fake_assets):
    fake_assets(list_error=RuntimeError("STAC search timed out"))

    assert client.get("/items/S2B_test/assets").status_code == 502
