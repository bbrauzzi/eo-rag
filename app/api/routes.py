import json
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.graph import (
    ConversationBudgetExceeded,
    answer_question,
    check_conversation_budget,
    stream_answer,
)
from app.api.assets import list_assets, open_asset
from app.api.preview import fetch_preview
from app.db.session import get_db

router = APIRouter()

# Dependency as an annotated type: avoids calling Depends() in argument defaults.
DbSession = Annotated[Session, Depends(get_db)]


class AskRequest(BaseModel):
    question: str
    # Omit to start a new conversation; pass back the id from a previous answer to
    # continue that one.
    conversation_id: str | None = None


class AskResponse(BaseModel):
    answer: str
    sources: list[str]
    conversation_id: str


class AssetResponse(BaseModel):
    key: str
    title: str | None
    type: str | None
    roles: list[str]
    href: str


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/ask", response_model=AskResponse)
def ask(payload: AskRequest, db: DbSession):
    try:
        result = answer_question(db, payload.question, payload.conversation_id)
    except ConversationBudgetExceeded as e:
        # 429 rather than 400: the request is well formed and would have been served a
        # few turns ago. The client's move is to start a new conversation, which the
        # message says, so it goes out as the detail rather than being logged away.
        raise HTTPException(status_code=429, detail=str(e)) from e

    return AskResponse(
        answer=result.text,
        sources=result.sources,
        conversation_id=result.conversation_id,
    )


@router.post("/ask/stream")
def ask_stream(payload: AskRequest, db: DbSession) -> StreamingResponse:
    """
    The same turn as POST /ask, reported as it happens.

    Frames carry one JSON object on a single `data:` line, with the event type inside it
    rather than in an SSE `event:` line: json.dumps escapes newlines, so a frame is
    always exactly one line and the client parser stays trivial.

    The response body is a sync generator, which Starlette runs in a threadpool. The
    SQLAlchemy Session therefore crosses threads on its way in - a sequential handoff
    with no concurrency, which psycopg3 connections tolerate - and stays open for the
    whole stream because FastAPI unwinds the dependency stack after the response is
    finished, not after this function returns. `tests/test_ask_stream.py` pins that.

    The budget is checked here, before the generator exists, because that is the last
    moment a status code can still be chosen: everything the generator raises has to be
    reported inside a 200 that has already been sent.
    """
    try:
        check_conversation_budget(payload.conversation_id)
    except ConversationBudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e)) from e

    def frames() -> Iterator[str]:
        try:
            for event in stream_answer(db, payload.question, payload.conversation_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            # Broad on purpose, and the only place in the project that is: the status
            # line went out with the first frame, so a failure here has nowhere else to
            # go. Failing *tools* never reach this - the graph turns those into errored
            # tool_results the model can answer around.
            message = json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False)
            yield f"data: {message}\n\n"

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx not to buffer the body the day one sits in front of this.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/preview/{item_id}")
def preview(item_id: str) -> Response:
    """
    The preview image of one STAC scene, proxied so the browser gets it same-origin.

    An item id and not a URL: the frontend cannot point this at anything the configured
    catalog did not hand back for that id. See `app/api/preview.py` for why proxying at
    all - in short, the map needs the image as a WebGL texture, which makes it a CORS
    request against a host that owes us nothing.
    """
    try:
        body, media_type = fetch_preview(item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return Response(
        content=body,
        media_type=media_type,
        # The assets of a published scene do not change, and this is now our own origin
        # to cache - no third party's headers involved.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/items/{item_id}/assets", response_model=list[AssetResponse])
def item_assets(item_id: str) -> list[AssetResponse]:
    """Every downloadable asset of one scene, so the frontend can offer them by key."""
    try:
        assets = list_assets(item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return [AssetResponse(**vars(asset)) for asset in assets]


@router.get("/items/{item_id}/assets/{asset_key}")
def item_asset(item_id: str, asset_key: str) -> StreamingResponse:
    """
    One asset of one scene, streamed through the API as a download.

    An item id and an asset key, never a URL - the same containment as `/preview`, and
    for a band that can be most of a gigabyte the streaming is what keeps it from
    arriving in memory first. See `app/api/assets.py` for why this is proxied when a
    plain link to the catalog would be cheaper.
    """
    try:
        download = open_asset(item_id, asset_key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    headers = {
        # Quoted, and the filename is built by `_filename` rather than taken from the
        # catalog: a header value is not a place to interpolate a remote string loosely.
        "Content-Disposition": f'attachment; filename="{download.filename}"',
        "Cache-Control": "public, max-age=86400",
    }
    if download.size is not None:
        # What gives the browser a progress bar instead of a spinner of unknown length.
        headers["Content-Length"] = str(download.size)

    return StreamingResponse(
        download.chunks,
        media_type=download.media_type,
        headers=headers,
    )
