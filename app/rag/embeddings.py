"""
Embeddings via Amazon Bedrock (Titan Text Embeddings V2).

The single place that talks to Bedrock: ingestion and retrieval both go through
here, so model and dimension stay consistent between indexing and querying.
"""

import json

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

_cached_client = None


def _client():
    """boto3 client built and cached on first use (no side effects at import time)."""
    global _cached_client
    if _cached_client is None:
        _cached_client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        )
    return _cached_client


def embed_text(text: str) -> list[float]:
    """Compute the embedding of a single text. Titan has no input_type: no asymmetry
    between document and query."""
    body = json.dumps(
        {
            "inputText": text,
            "dimensions": settings.embedding_dim,
            "normalize": True,
        }
    )

    try:
        resp = _client().invoke_model(modelId=settings.embedding_model, body=body)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        hint = (
            f" AccessDeniedException usually means access to model "
            f"'{settings.embedding_model}' is not enabled for region "
            f"'{settings.aws_region}' in the Bedrock console (Model access)."
            if code == "AccessDeniedException"
            else " Check your AWS credentials, region and model name."
        )
        raise RuntimeError(f"Bedrock call failed ({code or 'ClientError'}).{hint}") from e

    return json.loads(resp["body"].read())["embedding"]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed several texts, one at a time: Titan's InvokeModel accepts a single
    inputText per request, there is no real batch API.
    If the corpus grows, the next step is to parallelize with ThreadPoolExecutor.
    """
    return [embed_text(t) for t in texts]
