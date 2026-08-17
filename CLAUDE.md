# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dev tooling runs through `uv` (no committed venv; `--extra dev` pulls pytest/ruff):

```bash
uv run --extra dev pytest -q                        # full suite
uv run --extra dev pytest tests/test_ingest.py -q   # one file
uv run --extra dev pytest -q -k standalone_chunk    # one test by name
uv run --extra dev ruff check .                     # lint

uv sync --extra observability                       # optional: the Langfuse exporter
uv sync --extra mcp                                 # optional: the MCP server
```

The MCP server is not part of `pytest` either — it is a second front end onto the same
tools, run as a subprocess or mounted at `/mcp`:

```bash
python -m app.mcp.server                  # stdio, what an MCP client launches
eo-rag-mcp                                # the same thing, as a console script
docker compose up -d                      # streamable HTTP at :8000/mcp
```

The eval harness is not part of `pytest` — it needs live services and spends money:

```bash
python -m scripts.eval --smoke            # are the live services up? no cases, no spend
python -m scripts.eval --retrieval-only   # embeddings + pgvector only, free
python -m scripts.eval                    # everything, live model and catalog calls
python -m scripts.eval --compare          # judge against evals/baseline.json, exit 1 on regression
python -m scripts.eval --save-baseline    # accept the current scores as the standard
```

The suite must pass **with and without** each optional extra — tracing works without
Langfuse, and the MCP adapters are testable without the SDK. That is the point of both
extras being separate. `uv run --extra dev` is **additive** and will not remove an extra a
previous command installed, so the without-the-extra run needs an explicit sync:

```bash
uv sync --extra dev && uv run --no-sync pytest -q   # 1 skipped: tests/test_mcp_server.py
uv run --extra dev --extra mcp pytest -q            # everything
```

Running the stack:

```bash
docker compose up -d          # Postgres+pgvector (:5432) and the API + UI (:8000, --reload)
docker compose up -d db       # DB only, then run the API locally:
uv run uvicorn app.main:app --reload

docker compose exec api python -m app.rag.ingest data/stac-spec-core.md --source "stac-spec.md"
curl -X POST http://localhost:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "What are STAC Items?"}'
```

The frontend has its own toolchain, and is not wired into `pytest`:

```bash
cd frontend && npm install
npm run dev        # :5173, proxying /ask, /ask/stream and /health to :8000
npm test           # vitest, over the SSE frame parser
npm run build      # what the Docker image's node stage runs
npx tsc -b --noEmit
```

## Architecture

A tool-calling agent over EO/STAC technical documentation and live STAC catalogs:

- **Ingestion** (`app/rag/ingest.py`, run as a CLI module): markdown → structural
  chunks → embeddings → `doc_chunks` rows. Not wired to the API.
- **Query** (`POST /ask` in `app/api/routes.py`): a thin adapter over the LangGraph
  graph in `app/agents/graph.py`, which offers Claude `rag_lookup`, `stac_search` and
  `compute_index` and iterates until it produces text → answer, deduped sources,
  conversation id.
- **Streaming** (`POST /ask/stream`): the same turn reported as it happens, over
  `stream_answer`. See "Two entry points, one graph" below.
- **Interface** (`frontend/`): React + Vite + TypeScript, Tailwind v4, MapLibre GL.
  Built in the Docker image's node stage and served by FastAPI; never built in the repo.

- **MCP** (`app/mcp/`): the same three tools and the corpus, exposed over the Model
  Context Protocol — stdio for desktop clients, streamable HTTP mounted at `/mcp`.

`ROADMAP.md` steps 0-10 are complete; what is left is the Cross-cutting list at its end.

### The graph is two nodes and a conditional edge

`START → agent → (tool_use blocks?) → tools → agent → … → END`. That conditional edge
is the router: it dispatches on what the model actually asked for. There is no separate
classification node on purpose — a hard upfront "documentation vs. data" decision cannot
express *both*, and the question chaining `rag_lookup` and `stac_search` is the one
verified live in `VERIFY.md`. It would also cost an extra model call to decide something
the model decides for free as part of the turn it is taking anyway.

### The step cap is a hard cap

`settings.max_agent_steps` bounds how many rounds of tools the model gets. The `agent`
node stops passing `tools` once the cap is reached, so the last turn has no choice but
to conclude with what was gathered: tools run at most `max_agent_steps` times, the model
is called at most `max_agent_steps + 1` times, and the caller always gets an answer.

`recursion_limit` is derived from the cap (`2 * max + 5`) because agent and tools
alternate; LangGraph's default of 25 would otherwise fire before the cap ever applied.

### The step cap bounds a turn; the budget bounds the thread

`max_conversation_turns` and `max_conversation_cost_usd` (either at 0 disabling its own
check) are what the step cap cannot express: a conversation could be continued forever,
each turn resending a history that only grows, and the step cap would permit every one of
them. Both ride the checkpointer as accumulating state, so they are per thread.

Two consequences that look like bugs otherwise:

- **The cap is crossed, not respected exactly.** `_check_budget` runs *before* a turn, so
  the turn that exceeds the budget runs to completion and the *next* one is refused.
  Checking afterwards would not be a limit — the tokens are already bought.
- **An unrecognized `CLAUDE_MODEL` is priced at the most expensive model known**, not at
  zero (`app/agents/cost.py`). A guardrail that fails open is not a guardrail; erring
  upwards ends a conversation early, which is the survivable direction.

`ConversationBudgetExceeded` is a `RuntimeError` so the streaming path's existing handling
covers it, and its own type so the routes can answer **429** rather than 500 — the request
is well formed and would have been served a few turns ago. `/ask/stream` checks in the
route, before the generator exists, because that is the last moment a status code can be
chosen; `stream_answer` keeps its own check as the actual guarantee.

Prices in `MODEL_PRICING` are a transcribed copy that nothing can verify at runtime — the
API bills the account, it does not return a price. The cap bounds an *estimate* of
list-price spend. Cache tokens are priced despite always being zero here, so that adding
prompt caching does not silently start under-reporting.

A tool that raises does not surface as a 500. The `tools` node catches
`ValueError`/`TypeError`/`RuntimeError` and hands the message back as an errored
`tool_result` — a malformed bbox or an unreachable catalog is something the model can
explain or retry.

### State is plain data, dependencies are not state

The state has two kinds of field and the reducer is the difference. `messages`, `turns`
and `cost_usd` **accumulate** across the thread; `steps`, `sources` and `features`
describe the turn just taken and are reset by each invocation's input. So a follow-up
answered purely from history legitimately returns **empty sources and no footprints** —
no tool ran. The streaming path leans on that: a turn with no new footprints sends no
`features` event at all, which is what leaves the map showing the scenes the follow-up is
*about*, with no clearing rule for the frontend to get wrong.

`_turn_input` contributes `"turns": 1` rather than resetting it — with an adding reducer
that counts invocations, which is what "turns in this conversation" means. `cost_usd` is
deliberately absent from it: only the `agent` node contributes, and naming it there would
reset nothing and confuse plenty.

Assistant turns are stored as dicts, not Anthropic SDK block objects
(`model_dump(exclude_none=True)`): the checkpointer serializes the state, and SDK objects
either fail to serialize or come back as dicts on a resumed turn, which would force the
code to handle both shapes.

The SQLAlchemy `Session` travels in the LangGraph **context** (`AgentContext`), not the
state, precisely so it is never checkpointed — a resumed conversation would otherwise
come back holding a session that closed long ago.

### The agent node streams, and does so synchronously

`agent` calls `messages.stream()` and pushes each text delta onto LangGraph's custom
channel through `get_stream_writer()`. That call must happen **inside** the node body —
it reads the running config.

It is deliberately not async. An async-only node makes `graph.invoke()` raise
`TypeError: No synchronous function provided to "agent"`, so `answer_question` would need
an `asyncio.run` bridge — and a module-cached `AsyncAnthropic` holds an `httpx.AsyncClient`
whose pooled connections die with the loop that created them, so the second `/ask` onwards
would 500 with `RuntimeError: Event loop is closed`. Under `.invoke()` the writer simply
goes nowhere, so **one node body serves both entry points** and there is no async twin to
keep in step. If this ever has to be async, the only safe shape is
`RunnableLambda(agent, afunc=aagent)` with two cached clients.

### Two entry points, one graph

`answer_question` invokes; `stream_answer` streams with `stream_mode=["custom", "values"]`
and yields event dicts with no transport framing (`app/api/routes.py` turns them into
SSE). They share `_turn_input` and `_turn_config` so the per-turn reset and the
`recursion_limit` derivation cannot drift, and they share the compiled graph and therefore
the checkpointer — a conversation can move between them. `test_the_streaming_and_the_blocking_path_agree`
is the guard.

Two consequences worth knowing before they look like bugs:

- **The streamed tokens are a superset of `done.answer`.** Tokens come from every agent
  turn, including the preamble the model writes next to a `tool_use`; `_answer_text` reads
  the last turn's text blocks alone.
- **`steps` is exposed on the streaming path only.** `/ask` still returns exactly
  `{answer, sources, conversation_id}`, and `tests/test_ask.py` pins that.

SSE frames are one JSON object on a single `data:` line with the type inside, not named
`event:` lines: `json.dumps` escapes newlines, so a frame is always one line and the
client parser stays trivial. The broad `except` in `ask_stream` is the only one in the
project — the status line went out with the first frame, so a failure there has nowhere
else to go.

### A stopped stream leaves a tool call open, and every later turn pays for it

`_repair_interrupted_turn` runs before each turn on a thread, and exists because of a
failure whose symptom points at the wrong turn entirely.

A client that goes away mid-stream — the Stop button, a closed tab, a dropped connection —
makes Starlette close the response generator, which abandons the graph **between the
`agent` and `tools` supersteps**. `agent`'s write is already checkpointed, so the history
now ends with an assistant turn carrying `tool_use` blocks that nothing will ever answer.
Anthropic refuses that outright:

```
messages.4: `tool_use` ids were found without `tool_result` blocks immediately after
```

So the interrupted turn looks fine and *every subsequent turn on that thread* 400s. One
Stop and the conversation is dead until a new `conversation_id` is started — which is how
it presents: "Stop broke my chat", with a raw API error in the UI.

The repair injects errored `tool_result`s for the dangling calls, on the same principle as
a tool that raises: tell the model the call produced no result and let it answer around
it. On the way *in*, because there is no way out — by the time we know, the generator is
already being closed.

Its test abandons the graph's own generator between the two supersteps rather than
planting a damaged message, since a planted one would only prove the repair works against
a shape the test invented.

### The footprints go to the map, not to the model

`_summarize_item` carries `geometry`; `model_view` strips it back out before the result is
`json.dumps`'d into a `tool_result`; `item_footprint` turns what is left into the GeoJSON
the map draws, and the `tools` node accumulates those into `features`, deduped by id.
Measured live: 1699 bytes reach the model for two scenes, polygons reach the UI.

`_polygon_from_bbox` (the fallback for an item with no geometry) lives in `stac_search`
next to `_validate_bbox` for one reason: that function deliberately allows `west > east`
for an antimeridian crossing, so the module that owns [west, south, east, north] is the
one that must split such a bbox into a MultiPolygon. The frontend then only ever sees one
shape and never has to know the rule. `assertLonLat` in `frontend/src/map/layers.ts` is a
tripwire for the reverse mistake — a swap does not fail, it just draws Rome in the Gulf of
Guinea.

### Quicklooks, and why previews are proxied

Clicking a footprint lays that scene's preview over it as a MapLibre `image` source,
positioned on the footprint's four corners rather than its bbox — an MGRS tile is a
quadrilateral rotated a few degrees off north, and the bbox would stretch the image into
that rotation. `imageCorners` sorts the ring north-to-south then west-to-east and falls
back to the bbox for anything that is not a clean four-corner ring. It has its own test:
a wrong ordering does not fail, it silently mirrors the image.

`syncQuicklooks` reconciles drawn against wanted rather than adding and removing, because
the two drift apart on their own — a new search replaces the features under a quicklook
still switched on.

**The browser never loads a catalog thumbnail directly.** It goes through
`GET /preview/{item_id}` (`app/api/preview.py`), and the reason is worth keeping because
it looks like a server problem when it bites:

- The map needs the image as a WebGL texture, which makes it a **CORS** request. A
  catalog owes us no `Access-Control-Allow-Origin`; Earth Search sends it, plenty do not.
- Worse, S3 returns that header **only when the request carries `Origin`**, and the
  response without it also carries no `Vary`. So one plain `<img>` load caches an entry
  the browser will reuse for *any* later request to that URL — and the CORS one is then
  refused from cache while `curl -H "Origin: ..."` cheerfully shows the header present.
  Reproduced in sequence on one URL: clean cache `200 cors`, one non-CORS load,
  `BLOCKED`, `cache: 'reload'`, `200 cors` again.

Proxying removes the class of problem rather than the instance: same origin, no
preflight, no entry anyone else can poison, and a catalog with no CORS at all still
works. `crossOrigin="anonymous"` on the card `<img>` was the first attempt and is *not*
enough — it makes the card break too instead of only the map.

A catalog's href is not necessarily fetchable over HTTP. Sentinel-1 GRD on Earth Search
gives its quick-look — and its bands — as `s3://sentinel-s1-l1c/...` with no https
alternate, and httpx refuses that scheme (`UnsupportedProtocol`), so every S1 card was a
broken image while Sentinel-2, whose thumbnails are already https, worked.
`fetchable_href` (in `app/tools/stac_search.py`, the module that owns catalog hrefs; both
proxies use it) rewrites `s3://` to the regionless virtual hosted-style URL, which keeps
the containment below intact — bucket and key still come from the item the catalog
returned — and resolves to whatever region holds the bucket; path style would 301 without
a `Location` for anything outside us-east-1.

**The endpoint takes an item id, never a URL.** That is the containment: `fetch_preview`
resolves the href through `fetch_item`, so the only thing it can ever fetch is what the
configured catalog returned for that id. It also refuses anything that is not a
browser-renderable image type — Earth Search's `overview` carries a preview role and is a
GeoTIFF — and caps the body, so a mislabelled full scene cannot be pulled through the API.
`properties.thumbnail` stays in the feature as the catalog's own href: it is how the
frontend knows a preview exists, not where it loads it from.

### Asset downloads are proxied too, but for different reasons

`GET /items/{id}/assets` lists a scene's assets and `GET /items/{id}/assets/{key}`
downloads one (`app/api/assets.py`), behind the "⤓ assets" control on each card.

The preview's CORS argument does **not** carry over — a download is a navigation, not a
`fetch`, so a plain link to the catalog would work and cost us nothing. Three things
decided it anyway: Sentinel-1 publishes its bands as `s3://`, which a browser cannot
follow at all; every Sentinel-2 scene's red band is named `B04.tif`, so ten of them in a
downloads folder are indistinguishable until `Content-Disposition` puts the scene id in
front; and a catalog that blocks hotlinking keeps working without the frontend learning
anything about it.

It **streams** rather than buffering, which is the one way it must not copy
`fetch_preview`: a Sentinel-1 GRD band is 721 MB (measured), and `MAX_BYTES` exists in
the preview precisely because a thumbnail that big is a bug. There is no cap here — the
size *is* the feature. Two consequences: the upstream request is made eagerly in
`open_asset` so a 404 is still a status code rather than a truncated body (the corner
`ask_stream` is stuck in), and the body generator closes the response in a `finally`, or
a few abandoned downloads exhaust the connection pool and every later one blocks.

`Range` is not forwarded, so a download that dies at 90% starts over.

The list is deliberately unfiltered where `stac_search` projects hard: 35 assets would
bury the model's context, but this is the endpoint a *person* uses to get at exactly
those bands, and the metadata XML is as legitimate a thing to want as the COG.

The popover renders through a portal on `document.body`, not inside the card. The card
is a `<button>` (an `<a>` nested in one has its activation dropped) and the strip is
`overflow-x-auto` (which clips anything a 168px card overflows) — either alone breaks it.
Its height comes from the room measured on the side it opens towards rather than a fixed
max: a separate flip threshold and a fixed `max-h` disagreed, and a popover that cleared
the threshold by 30px still ran 60px off the bottom of the viewport.

### Every tool returns a pydantic model

`stac_search` → `SearchResult`/`ItemSummary`, `rag_lookup` → `LookupResult`,
`compute_index` → `IndexResult` (with `Statistics`, `Bands`, `Reflectance`,
`PixelCounts`). `_run_tool` is the single place they become the string a `tool_result`
carries, and `rag_lookup` is the one that is **not** `json.dumps`'d: its passages already
carry `[Source: ...]` labels, and JSON would only add escaping to prose.

Validation is the smaller half of the point. The larger one is that a projection with two
consumers can now *name* what it gives each: `model_view` excludes
`{"items": {"__all__": {"geometry"}}}` by field rather than filtering keys by string, and
`index_footprint` reads `result.statistics` rather than hoping the key exists. Anything
crossing into JSON — the footprint properties the frontend receives — is `model_dump()`ed
at that boundary, since a pydantic object inside a GeoJSON Feature fails at serialization
time rather than at the mistake.

The tools' test fakes return those same models, for the reason the Anthropic block types
are real in `tests/test_agent_graph.py`: a fake of a shape the graph could not actually
consume hides the bug rather than catching it.

### The MCP server is a second front end, not a second implementation

`app/mcp/` exposes the same three tools and the same corpus. Four modules, and **only
`server.py` imports `mcp`** — `tools.py` and `resources.py` are SDK-free so that they and
their drift guards run in the default dev environment, where the optional extra is absent.

The adapters are wrappers rather than decorators on the originals because under MCP the
type hints *are* the schema: `compute_index`'s `bbox` is untyped, `stac_search` takes a
`Sequence`, and `Literal["ndvi","ndwi"]` is how the SDK is told what `COMPUTE_INDEX_TOOL`
hand-writes as an enum. Per-argument descriptions come from `Annotated[..., Field(...)]`
and are copied verbatim from the existing schemas — the allowlist named in the `collections`
text is what stops a client inventing `sentinel2`.

Six things that will bite otherwise, all found while building it:

- **`streamable_http_app()` already serves at `/mcp`.** Mounting it at `/mcp` without
  `streamable_http_path="/"` puts the endpoint at `/mcp/mcp` and leaves `/mcp` a 404 with
  nothing to explain it. The endpoint therefore really lives at `/mcp/`, and a bare `/mcp`
  gets a 307 — every client tried follows it, but that is why both spellings work.
- **The session manager must run in the app lifespan**, or every request fails `Task group
  is not initialized`. It is created lazily by `streamable_http_app()`, so the lifespan may
  only touch it after the mount — which is why the mount is at import and the lifespan at
  startup. It also **can only be run once per instance**, so a process cannot start the app
  twice; `tests/test_mcp_server.py` reloads both modules per test to get a fresh one.
- **Host validation refuses anything but localhost by default**, and its patterns
  (`127.0.0.1:*`, `localhost:*`) *require a port* — so a request on 80 or 443 is refused
  even from localhost. `MCP_ALLOWED_HOSTS` is not optional behind a proxy.
- **stdout belongs to the protocol** under stdio. `configure_logging()` defaults to a
  stdout handler; `app/mcp/server.py` passes `sys.stderr`, and must never import
  `app.main`, which configures logging at import.
- **`rag_lookup` returns a `str`, not `LookupResult`** — `scored` holds SQLAlchemy rows
  pydantic cannot schema, and the prose is right anyway, for the same reason `_run_tool`
  does not `json.dumps` it.
- **`httpx` and `httpx2` both live here.** The SDK depends on the latter; nothing else
  does. Different distributions, different top-level modules, not a mistake to consolidate.

Footprints are stripped by default (`include_geometry` opts in) because the SDK puts a
returned model into *both* `structured_content` and the model-visible text — so it is one
choice, not one per consumer, and 69,727 against 2,516 bytes decides it.

`/mcp` gets its own rate-limit tier: measured, one session's handshake plus three listings
is **8 HTTP requests**, so sharing `/ask`'s 10/minute would refuse the second client to
connect.

### The eval set is a file, and the metrics are not interchangeable

`evals/cases.yaml` is the labelled set; `app/evals/` is the harness; `scripts/eval.py` is
the CLI, deliberately **not** a pytest file — `pytest` here is the offline suite and stays
that way, while this needs a database, Bedrock, a catalog and a model, and costs money.

`app/db/models.py` still carries an `EvalCase` table from step 0. It is **dead schema**:
a labelled set is a stated opinion about what a correct answer is, and opinions belong
where they get reviewed — a YAML file diffs in a pull request, a row does not. It is left
in place only because `init_db.sql` runs once per volume, so dropping it would cost a
migration for a table nothing reads.

Relevance is labelled by **section**, because chunk ids are reassigned by every
re-ingestion while the section a fact lives in is a property of the document. That makes
the label coarse, and the three metrics are not equally informative as a result:

- **recall@k** saturates — `Item fields` spans many chunks, so any one of them scores a
  full hit. Useful only as a floor: below 1.0 the right section never came back at all.
- **MRR** is the discriminating one, because the model reads the top of the list first.
- **precision@k** is the counterweight that moves when chunking changes.

Two things the harness does that look like details and are not:

- **It drives `stream_answer`, not `answer_question`.** The tools a turn used are not on
  the `Answer` — `/ask` returns exactly `{answer, sources, conversation_id}` and
  `tests/test_ask.py` pins that — but every `tool_start` names one. So the harness reads
  the same events an SSE client would and needs no change to the API's response shape.
- **Aggregates are compared over the cases both runs contain.** Comparing an average
  across two different case sets is not a comparison: without it, adding one known-failing
  case drops `pass_rate` and trips the gate, which would mean the only way to record a bug
  is to break the build.

The regression gate fails on a case that passed and now fails, or a gated metric falling
by more than `TOLERANCE` (0.05 — retrieval moves on re-ingestion and model calls are not
deterministic, and a gate that cries wolf gets switched off). Cost and latency are
reported but never gated: a run that got cheaper by answering worse must not pass on that.

### Every turn is traced, and the log is the floor

`app/obs/tracing.py`. A `Turn` rides in the LangGraph **context** next to the DB session
(same reason: it holds a clock reading and an open span, neither of which belongs in a
checkpoint) and records three things — which tools ran and how they ended, what each model
call cost in tokens and milliseconds, and the cosine distance of every retrieved chunk.

It is **not** built by tapping the stream writer, which is the obvious move and the wrong
one. Two reasons: the timings have to bracket the real work rather than the moment an
event was emitted, and a wire protocol and a telemetry record drift apart the first time
either gains a consumer of its own. The `writer(...)` calls still exist and still serve
the SSE client; they are a different thing that happens to overlap today.

The reason the step existed: `get_stream_writer()` returns a writer that goes **nowhere**
under `.invoke()`, so `/ask` produced no record at all and an unwatched stream produced one
that was discarded.

Three things that will bite otherwise:

- **`configure_logging()` in `app/main.py` is load-bearing.** Uvicorn configures its own
  loggers and leaves root with no handler, so INFO records propagate to nothing —
  measured, a real question put *zero* trace lines in the server log before it existed.
  The handler goes on `eo_rag`, not root, and `propagate` is left alone because pytest's
  `caplog` captures through root.
- **Langfuse is optional** (`uv sync --extra observability`) and lazy, like every other
  client here. Not configured is silent — it is the default and what the suite runs in;
  configured but not installed warns once. `_safe()` swallows exporter failures because
  telemetry may not become a new way for a request to fail; verified against the real SDK
  pointed at an unreachable host, where the turn completed while the export did not.
- **The trace opens before the first frame**, so an abandoned stream still closes it. The
  shortest abandonment of all — a client that disconnects immediately — would otherwise be
  the one turn with no record, and would leak an open span.

`LookupResult.scored` is a third split alongside `context` and `sources` and reaches
neither the model nor the answer, exactly like `ItemSummary.geometry`. `rag_lookup` calls
`retrieve_with_scores` because the ranking is a `cosine_distance` ORDER BY either way, so
the distances were already being computed and thrown away.

### The rate limiter is keyed on the caller, the budget on the conversation

`app/api/ratelimit.py` is application middleware, and it exists because the conversation
budget above **cannot be an abuse control**: that budget is keyed on a `conversation_id`
the client picks, so a caller that sends a fresh one every request is bounded by nothing.
This one is keyed on the peer address.

Two tiers, because the endpoints cost different things: `/ask` and `/ask/stream` spend
money on a model, `/preview` and `/items` spend bandwidth. `/health` and the built UI are
**deliberately unlimited** — a limiter that trips the container healthcheck takes the app
down, and one page load is many small files.

Four decisions that look arbitrary until they bite:

- **Raw ASGI, not `BaseHTTPMiddleware`.** That base class re-emits the response through a
  memory stream, which would put it directly in the path of `/ask/stream` — the one
  endpoint built around a long-lived body and a client that may vanish mid-flight, and
  the reason `_repair_interrupted_turn` exists. This middleware only reads the request
  scope and then gets out of the way, so streaming and disconnect behaviour are untouched.
- **A refused request is not recorded.** Appending on refusal would let a client hammering
  the endpoint keep pushing its own window forward, staying locked out long after going
  quiet — a ban rather than a rate limit.
- **`X-Forwarded-For` is ignored unless `RATE_LIMIT_TRUST_PROXY_HEADER` is set**, because
  a header the client sets is a header the client can forge: trusting it with nothing in
  front lets anyone opt out by varying it. When trusted, the **rightmost** entry is taken
  — a proxy appends the peer it actually saw, so everything to its left is client-supplied.
- **The tracking table is evicted.** An unbounded map keyed by address is itself a memory
  exhaustion vector; the ceiling is the number of clients *currently inside the window*.

Being outermost is load-bearing and verified: a malformed `/ask` body comes back **429,
not 422**, because the refusal happens before the router validates it or opens a session.

Sliding window, in process. It dies with the process and each worker keeps its own tally,
so N workers means an effective limit of N × `limit`. That is the accepted trade for no
new dependency and no schema — and `SlidingWindow` is the only thing that would change if
it ever moves to Redis.

`tests/conftest.py` turns it **off for the whole suite**, because it is middleware and
would otherwise refuse other test files' requests depending on execution order;
`tests/test_ratelimit.py` turns it back on and drives a fake clock.

### Tool schemas are narrower than the functions

Both tools take arguments the model is not shown: `top_k` on `rag_lookup`, `asset_keys`
on `stac_search`. They are caller-side knobs (retrieval tuning; the bands
`compute_index` will need), not decisions to improvise per question. Each tool has a
test asserting its schema stays in sync with the function signature *minus* those.

### `stac_search` projects the catalog response down

`_summarize_item` exists because a Sentinel-2 L2A item on Earth Search carries **35
assets** — every band as both COG and JP2 — plus geometry and links. Measured on a live
three-scene search: 69,727 bytes raw against 2,516 projected, 28× smaller. Asset names
always come back (they cost about half the projection), hrefs only for previews or for
explicitly requested keys.

### Bare dates have to be completed before they reach the catalog

Earth Search rejects `2024-01-01` with a 400 (`does not match RFC3339 format`), and a
lone date sent as an *instant* matches only scenes acquired exactly at midnight — zero
results, no error. `_normalize_datetime` therefore expands a bare date to a full day and
completes each end of an interval, so `2024-01-01/2024-01-31` and `2024-01-15` both work.
Anything already carrying a time passes through untouched.

### An unknown collection is a typo, not an empty world

`_validate_collections` checks `collections` against `settings.allowed_collections`
locally and before the request, because Earth Search answers a typo'd id with an **empty
result set, not an error** — so without it the model is told "no scenes match" and
reports that as fact. An empty allowlist turns the check off, for a catalog whose ids have
not been listed yet.

The allowlist is also **named in the tool schema's description**, not merely enforced:
rejecting a call without saying what exists spends a round trip on something the model
cannot guess ("sentinel-2-l2a", not "sentinel2" or "S2"). Deliberately not an `enum` —
an empty allowlist has to mean "no constraint", and an empty enum would forbid every
value instead.

This is the class of bug the offline suite cannot catch. `tests/fixtures/earth_search_search.json`
is a real captured response for that reason; when changing the request shape of a tool,
the loop or the prompts, run the live checks in `VERIFY.md` too — that is where both the
datetime bug and a `MAX_TOKENS` truncation were found.

### Embeddings choke point

`app/rag/embeddings.py` is the only module that talks to Bedrock, deliberately: both
ingestion and retrieval call it so the model and dimension can't drift apart between
indexing and querying. `embed_texts` loops one call per text — Titan's InvokeModel has
no batch API. The boto3 client is built lazily and cached in `_cached_client` so that
importing the app never touches AWS.

### The embedding dimension is duplicated in three places

`settings.embedding_dim` (default 1024) → `Vector(...)` in `app/db/models.py` →
the literal `vector(1024)` in `scripts/init_db.sql`. Changing the embedding model
means updating the SQL column, `EMBEDDING_DIM`, then `TRUNCATE doc_chunks;` and
re-ingesting — vectors from different models are not comparable, and a dimension
mismatch fails at insert time.

### Schema has no migrations

`scripts/init_db.sql` is mounted into the pgvector image's
`/docker-entrypoint-initdb.d/`, so it runs **only when the data volume is first
created**. The SQLAlchemy models mirror it by hand. Editing the SQL has no effect on
an existing database — recreate the volume (`docker compose down -v`) or apply the
DDL manually.

### Chunking contract

`split_markdown` returns `list[dict]` with `content` and `section` keys (not LangChain
`Document`s). Headers are kept in the content (`strip_headers=False`), and `section`
falls back to the `#` title when there is no enclosing `##`. A post-pass
(`_merge_header_only_chunks`) prepends header-only chunks — which the char splitter
produces when it detaches a heading from its body — onto the next chunk of the same
section, dropping them if that section has no body.

## Testing

The suite runs fully offline: no AWS credentials, no network, no database. Every module
that talks to the outside world builds its client lazily into a module-level
`_cached_client` behind a `_client()` function, and tests monkeypatch that function:
Bedrock in `tests/test_embeddings.py`, the STAC catalog in `tests/test_stac_search.py`,
Anthropic in `tests/test_agent_graph.py`. Each of those files also has an import-purity
test that reloads the module with the client constructor sabotaged. Keep new tests to
that bar — nothing in `app/` should acquire a client or connection at import time.

The graph is tested by scripting the sequence of replies the fake Anthropic client hands
back (`FakeMessages` in `tests/test_agent_graph.py`), which is what makes the multi-turn
branches reachable. Those fakes use the **real** SDK block types (`TextBlock`,
`ToolUseBlock`) rather than stand-ins, because the graph calls `model_dump()` on them and
the checkpointer then serializes the result — a hand-rolled fake hid both steps and let a
serialization bug through. An autouse fixture resets `_cached_graph` so the in-memory
checkpointer never leaks between tests. `tests/test_ask.py` fakes the graph entirely and
only covers the HTTP contract.

Note that `app.config.settings` *is* instantiated at import, reading `.env` if present,
so tests should read values from `settings` rather than hardcoding defaults.

`tests/test_ask_stream.py` fakes `stream_answer` the way `test_ask.py` fakes
`answer_question` and covers the SSE envelope only. One test there is subtler than it
looks: the session-lifetime check asserts `db.closed` **inside the generator**, one
snapshot per event, rather than comparing a log's ordering — `TestClient` buffers the
body, so any ordering assertion would pass whether or not the dependency was still open.
Whether the frames really leave incrementally is a live check (`VERIFY.md` step 10a),
not something this suite can see.

Nothing in `app/main.py`'s static mount may make the suite depend on a build: the mount is
conditional on `frontend_dist/` existing, which it never does in a checkout.

The frontend tests only the two pieces with real edge cases: `src/api/stream.test.ts` for
the SSE frame parser (chunk boundaries, a multi-byte character split across two reads) and
`src/map/layers.test.ts` for `imageCorners`. Everything visual is verified in `VERIFY.md`
instead, which is this project's stated division of labour. They run under `vitest`, not
`pytest`.

## Conventions

Comments, docstrings, prompts and user-facing strings are in English throughout,
including the `SYSTEM_PROMPT` and the `[Source: ...]` citation label — both of which
shape the language and format of the model's answers.
