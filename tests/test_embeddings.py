"""Tests for the embedding layer: fake boto3 client, no AWS credentials required."""

import importlib
import io
import json

import pytest
from botocore.exceptions import ClientError

from app.config import settings
from app.rag import embeddings


class FakeClient:
    """Fake bedrock-runtime: records the calls and returns a constant embedding."""

    def __init__(self, dim: int | None = None, error: Exception | None = None):
        self.dim = dim if dim is not None else settings.embedding_dim
        self.error = error
        self.calls: list[dict] = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        payload = {"embedding": [0.1] * self.dim}
        return {"body": io.BytesIO(json.dumps(payload).encode("utf-8"))}


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(embeddings, "_client", lambda: client)
    return client


def test_request_body_fields(fake_client):
    """The body carries inputText, a consistent dimensions value and normalize on."""
    embeddings.embed_text("hello")

    body = json.loads(fake_client.calls[0]["body"])
    assert body["inputText"] == "hello"
    assert body["dimensions"] == settings.embedding_dim
    assert body["normalize"] is True


def test_model_id_from_settings(fake_client):
    embeddings.embed_text("hello")
    assert fake_client.calls[0]["modelId"] == settings.embedding_model


def test_embed_text_returns_embedding(fake_client):
    vector = embeddings.embed_text("hello")
    assert isinstance(vector, list)
    assert len(vector) == settings.embedding_dim


def test_embed_texts_one_call_per_text_in_order(fake_client):
    texts = ["first", "second", "third"]
    vectors = embeddings.embed_texts(texts)

    assert len(vectors) == len(texts)
    assert len(fake_client.calls) == len(texts)
    sent = [json.loads(c["body"])["inputText"] for c in fake_client.calls]
    assert sent == texts


def test_embed_texts_empty(fake_client):
    assert embeddings.embed_texts([]) == []
    assert fake_client.calls == []


def test_client_error_becomes_runtime_error(monkeypatch):
    """A Bedrock ClientError surfaces as a RuntimeError with an actionable message."""
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no access"}},
        "InvokeModel",
    )
    client = FakeClient(error=error)
    monkeypatch.setattr(embeddings, "_client", lambda: client)

    with pytest.raises(RuntimeError) as exc:
        embeddings.embed_text("hello")

    message = str(exc.value)
    assert "AccessDeniedException" in message
    assert "Model access" in message


def test_import_does_not_build_aws_client(monkeypatch):
    """Importing the modules must not create a boto3 client (lazy client, zero network)."""

    def boom(*args, **kwargs):
        raise AssertionError("boto3.client called at import time")

    monkeypatch.setattr("boto3.client", boom)

    importlib.reload(embeddings)
    importlib.reload(importlib.import_module("app.rag.retrieval"))

    assert embeddings._cached_client is None
