# Design decisions

Why EO-RAG is built the way it is. Each section covers one capability, what it does, and
the decisions behind it that are not obvious from reading the code — including the ones
that were wrong first and had to be corrected against live services.

This is a reference document, not a changelog. For the shape of the system see
[architecture.md](architecture.md); for the per-module detail see [CLAUDE.md](../CLAUDE.md);
for the live checks that produced most of the measurements quoted here see
[VERIFY.md](../VERIFY.md).

---

## Ingestion and retrieval

Markdown is chunked structurally — headers first, then paragraphs and sentences at 800
characters with 100 of overlap. Headers stay *in* the content (`strip_headers=False`), and
`section` falls back to the `#` title when there is no enclosing `##`. A post-pass
(`_merge_header_only_chunks`) prepends header-only chunks onto the next chunk of the same
section, or drops them when that section has no body — the character splitter produces
them whenever it detaches a heading from what it introduces.

**Embeddings have one choke point.** `app/rag/embeddings.py` is the only module that talks
to Bedrock, and both ingestion and retrieval go through it, so the model and the dimension
cannot drift apart between indexing time and query time. Vectors from different models are
not comparable, and a silent mismatch is the worst failure mode available here.

`embed_texts` fans one `InvokeModel` call per text across a `ThreadPoolExecutor`. Titan has
no batch API, and a text spends its time on the round trip to Bedrock rather than on local
CPU. `pool.map` keeps result order matching input order regardless of completion order, and
boto3's low-level clients are documented thread-safe, so the single cached client is shared
across the pool with no lock.

**The embedding dimension has one source.** `settings.embedding_dim` feeds both
`Vector(...)` in `app/db/models.py` and the initial Alembic revision's `vector({dim})`.
It used to be written out in three places, and the consequence was a mismatch that failed
silently until insert time. Changing the model still needs a migration to
`ALTER COLUMN ... TYPE vector(new_dim)` (or a dropped volume) — the column width is set
once, at table creation. What is gone is the duplication, not the migration step.

**Ingestion is deliberately not wired to the API.** It is a CLI path
(`python -m app.rag.ingest`), because indexing is an operator action with a cost and a
blast radius, not something an HTTP request should be able to trigger.

## The three tools

Every tool returns a pydantic model: `stac_search` → `SearchResult`/`ItemSummary`,
`rag_lookup` → `LookupResult`, `compute_index` → `IndexResult`. `_run_tool` is the single
place they become the string a `tool_result` carries, and `rag_lookup` is the one that is
*not* `json.dumps`'d — its passages already carry `[Source: ...]` labels, and JSON would
only add escaping to prose.

Validation is the smaller half of the point. The larger one is that a projection with two
consumers can *name* what it gives each: `model_view` excludes
`{"items": {"__all__": {"geometry"}}}` by field rather than filtering keys by string, and
`index_footprint` reads `result.statistics` rather than hoping the key exists.

**Tool schemas are narrower than the functions.** `top_k` on `rag_lookup` and `asset_keys`
on `stac_search` are caller-side knobs — retrieval tuning, and the bands `compute_index`
will need — not decisions to improvise per question. Each tool has a test asserting its
schema stays in sync with the function signature *minus* those.

### `stac_search`

**It projects the catalog response down hard.** A Sentinel-2 L2A item on Earth Search
carries 35 assets — every band as both COG and JP2 — plus geometry and links. Measured on a
live three-scene search: **69,727 bytes raw against 2,516 projected, 28x smaller**. Asset
names always come back (they are about half the projection); hrefs only for previews or for
explicitly requested keys.

**Bare dates are completed before they reach the catalog.** Earth Search rejects
`2024-01-01` with a 400, and a lone date sent as an *instant* matches only scenes acquired
exactly at midnight — zero results, no error. `_normalize_datetime` expands a bare date to a
full day and completes each end of an interval. Anything already carrying a time passes
through untouched.

**An unknown collection is a typo, not an empty world.** Earth Search answers a misspelled
collection id with an empty result set rather than an error, so without a local check the
model is told "no scenes match" and reports that as fact. `_validate_collections` checks
against `settings.allowed_collections` before the request is spent. The allowlist is also
*named in the tool schema's description*, not merely enforced — rejecting a call without
saying what exists spends a round trip on something the model cannot guess. Deliberately
not an `enum`: an empty allowlist has to mean "no constraint", and an empty enum would
forbid every value instead.

`_polygon_from_bbox` lives here rather than in the map code for one reason: `_validate_bbox`
deliberately allows `west > east` for an antimeridian crossing, so the module that owns
`[west, south, east, north]` is the one that must split such a bbox into a MultiPolygon.
The frontend then only ever sees one shape and never has to know the rule.

### `compute_index`

COG assets are opened by href over `/vsicurl/` and read **windowed** on the bbox — a full
Sentinel-2 tile is 10980x10980 per band. Statistics come back, never an array, and with
**percentiles rather than just a mean**, which over mixed land cover hides exactly the
split the question is about.

`ndwi` came along with `ndvi` for the cost of a dict entry in `INDICES`, and pays for itself
in verification: water gives negative NDVI and positive NDWI, so the sign flip proves the
bands are not swapped.

**`MAX_PIXELS` decimates rather than refuses.** "Too big, try again" spends a model turn on
something that can just be handled. Measured live, the whole tile at 40 m came back *faster*
than a 5 km window at 10 m, because GDAL serves the decimated read from the COGs' internal
overviews — so no wall-clock cap was added, there being no slow read to bound.

**The declared reflectance offset is checked against the pixels before it is applied.**
This is the bug that justified the whole live-verification practice. Earth Search advertises
`offset: -0.1` on every sentinel-2-l2a item while the sentinel-cogs COGs behind it hold
*unshifted* DNs. Applying the offset as advertised put 68% of the red band at negative
reflectance and sent NDVI to **-4.8e11**. Negative reflectance is physically impossible, so
a band producing much of it is evidence the offset does not describe it
(`_offsets_fit_the_pixels`). The decision is made from the data and jointly for the band
pair, so the tool stays correct for catalogs whose pixels genuinely *are* shifted.

The offline tests could not have caught it: they invent the metadata and the pixels
together, so they always agree.

With negative reflectance excluded, the denominator is a sum of non-negative terms, so the
index is confined to [-1, 1] **by construction** — nothing needs clipping after the fact.

### `rag_lookup`

Wraps `retrieve_with_scores` + `format_context`. It calls the scored variant because the
ranking is a `cosine_distance` `ORDER BY` either way, so the distances were already being
computed and thrown away; keeping them costs one float per chunk and gives observability
something real to record. `LookupResult.scored` is a third split alongside `context` and
`sources` and reaches neither the model nor the answer — the same idea as
`ItemSummary.geometry`, which the map needs and the model is only made worse by.

## The agent loop

`START → agent → (tool_use blocks?) → tools → agent → … → END`. The conditional edge *is*
the router: it dispatches on what the model actually asked for.

**There is no separate classification node, on purpose.** A hard upfront "documentation vs
data vs computation" decision cannot express *both*, and the question that chains
`rag_lookup` into `stac_search` is the one verified live. It would also cost an extra model
call to decide something the model decides for free as part of the turn it is taking anyway.

**The step cap is a hard cap.** The `agent` node stops passing `tools` once
`settings.max_agent_steps` is reached, so the last turn has no choice but to conclude with
what was gathered. Tools run at most `max_agent_steps` times, the model is called at most
`max_agent_steps + 1` times, and the caller always gets an answer. `recursion_limit` is
derived from the cap (`2 * max + 5`) because agent and tools alternate; LangGraph's default
of 25 would otherwise fire before the cap ever applied.

**A tool that raises does not surface as a 500.** The `tools` node catches
`ValueError`/`TypeError`/`RuntimeError` and hands the message back as an errored
`tool_result` — a malformed bbox or an unreachable catalog is something the model can
explain or retry.

**State is plain data; dependencies are not state.** `messages`, `turns` and `cost_usd`
accumulate across the thread. `steps`, `sources` and `features` describe the turn just taken
and are reset by each invocation's input. So a follow-up answered purely from history
legitimately returns empty sources and no footprints — no tool ran. Assistant turns are
stored as dicts rather than SDK block objects (`model_dump(exclude_none=True)`), because the
checkpointer serializes the state and SDK objects either fail to serialize or come back as
dicts on a resumed turn, forcing the code to handle both shapes. The SQLAlchemy `Session`
travels in the LangGraph *context*, not the state, precisely so it is never checkpointed — a
resumed conversation would otherwise come back holding a session that closed long ago.

**The agent node streams, and does so synchronously.** It calls `messages.stream()` and
pushes each text delta onto LangGraph's custom channel through `get_stream_writer()`, which
must happen inside the node body because it reads the running config. It is deliberately not
async: an async-only node makes `graph.invoke()` raise, so `answer_question` would need an
`asyncio.run` bridge — and a module-cached `AsyncAnthropic` holds pooled connections that die
with the loop that created them, so the second `/ask` onwards would 500 with
`RuntimeError: Event loop is closed` (reproduced). Under `.invoke()` the writer simply goes
nowhere, so one node body serves both entry points and there is no async twin to keep in step.

**Conversation memory** is `MemorySaver` keyed by `thread_id`, with `conversation_id` on the
request and the response. It is in memory only and dies with the process; Postgres needs the
separate `langgraph-checkpoint-postgres` package, and the state shape is already ready for it.

## Streaming, and the interrupted turn

`answer_question` invokes; `stream_answer` streams with `stream_mode=["custom", "values"]`
and yields event dicts with no transport framing. They share `_turn_input` and `_turn_config`
so the per-turn reset and the `recursion_limit` derivation cannot drift, and they share the
compiled graph and therefore the checkpointer — a conversation can move between them.

SSE frames are one JSON object on a single `data:` line with the type inside, not named
`event:` lines: `json.dumps` escapes newlines, so a frame is always one line and the client
parser stays trivial.

Two consequences that look like bugs otherwise. **The streamed tokens are a superset of
`done.answer`** — tokens come from every agent turn, including the preamble the model writes
next to a `tool_use`, while `_answer_text` reads the last turn's text blocks alone. And
**`steps` is exposed on the streaming path only**; `/ask` still returns exactly
`{answer, sources, conversation_id}`.

**A stopped stream leaves a tool call open, and every later turn pays for it.** This is the
subtlest failure in the project, because its symptom points at the wrong turn entirely. A
client that goes away mid-stream — the Stop button, a closed tab, a dropped connection —
makes Starlette close the response generator, which abandons the graph *between* the `agent`
and `tools` supersteps. `agent`'s write is already checkpointed, so the history now ends with
an assistant turn carrying `tool_use` blocks that nothing will ever answer, and Anthropic
refuses that outright:

```
messages.4: `tool_use` ids were found without `tool_result` blocks immediately after
```

The interrupted turn looks fine and *every subsequent turn on that thread* 400s. One Stop
and the conversation is dead until a new `conversation_id` is started — which is how it
presents: "Stop broke my chat", with a raw API error in the UI.

`_repair_interrupted_turn` runs before each turn and injects errored `tool_result`s for the
dangling calls, on the same principle as a tool that raises. It repairs on the way *in*
because there is no way out: by the time the abandonment is detectable, the generator is
already being closed. Its test abandons the graph's own generator between the two
supersteps rather than planting a damaged message — a planted one would only prove the
repair works against a shape the test invented.

## Frontend and map

React + Vite + TypeScript, Tailwind v4, MapLibre GL. Two panes, conversation left and map
right. The dev server proxies the API, so there is no cross-origin request in development,
and in production FastAPI serves the built assets from its own port — which is why **there
is no CORS middleware anywhere**.

Basemaps were chosen to need no API key: OpenFreeMap by default, Esri World Imagery as an
optional toggle added *into* the style rather than through `setStyle`, which would tear down
the source and every layer on each switch.

**The footprints go to the map, not to the model.** `_summarize_item` carries `geometry`;
`model_view` strips it before the result is serialized into a `tool_result`;
`item_footprint` turns what is left into GeoJSON, and the `tools` node accumulates those
into `features`, deduped by id. Measured live: 1699 bytes reach the model for two scenes,
polygons reach the UI.

`features` is per-turn like `sources`, and a turn with no new footprints sends **no
`features` event at all** rather than an empty collection — which is what leaves the map
showing the scenes a follow-up is *about*, with no clearing rule for the frontend to get
wrong.

**Quicklooks** lay a scene's preview over its footprint as a MapLibre `image` source,
positioned on the footprint's four corners rather than its bbox — an MGRS tile is a
quadrilateral rotated a few degrees off north, and the bbox would stretch the image into
that rotation. `imageCorners` sorts the ring north-to-south then west-to-east and falls back
to the bbox for anything that is not a clean four-corner ring; it has its own test, because
a wrong ordering does not fail, it silently mirrors the image. `syncQuicklooks` reconciles
drawn against wanted rather than adding and removing, because the two drift apart on their
own when a new search replaces the features under a quicklook still switched on.

`assertLonLat` in `frontend/src/map/layers.ts` is a tripwire for the reverse mistake — a
coordinate swap does not fail, it just draws Rome in the Gulf of Guinea.

## Why previews and assets are proxied

These are two endpoints that look redundant and are not, for different reasons each.

**`GET /preview/{item_id}`.** The browser never loads a catalog thumbnail directly:

- The map needs the image as a WebGL texture, which makes it a **CORS** request. A catalog
  owes us no `Access-Control-Allow-Origin`; Earth Search sends it, plenty do not.
- Worse, S3 returns that header **only when the request carries `Origin`**, and the response
  without it also carries no `Vary`. So one plain `<img>` load caches an entry the browser
  reuses for *any* later request to that URL — and the CORS one is then refused from cache
  while `curl -H "Origin: ..."` cheerfully shows the header present. Reproduced in sequence
  on one URL: clean cache `200 cors`, one non-CORS load, `BLOCKED`, `cache: 'reload'`,
  `200 cors` again.

Proxying removes the class of problem rather than the instance. `crossOrigin="anonymous"`
on the card `<img>` was the first attempt and is *not* enough — it makes the card break too
instead of only the map.

**The endpoint takes an item id, never a URL.** That is the containment: `fetch_preview`
resolves the href through `fetch_item`, so the only thing it can ever fetch is what the
configured catalog returned for that id. It also refuses anything that is not a
browser-renderable image type — Earth Search's `overview` carries a preview role and is a
GeoTIFF — and caps the body, so a mislabelled full scene cannot be pulled through the API.

**`GET /items/{id}/assets`** and its download sibling exist for reasons that do *not*
include CORS: a download is a navigation, not a `fetch`, so a plain link to the catalog
would have worked. Three things decided it anyway. Sentinel-1 publishes its bands as
`s3://`, which a browser cannot follow at all. Every Sentinel-2 scene's red band is named
`B04.tif`, so ten of them in a downloads folder are indistinguishable until
`Content-Disposition` puts the scene id in front. And a catalog that blocks hotlinking keeps
working without the frontend learning anything about it.

It **streams** rather than buffering, which is the one way it must not copy `fetch_preview`
— a Sentinel-1 GRD band is 721 MB (measured), and `MAX_BYTES` exists in the preview
precisely because a thumbnail that big is a bug. There is no cap here; the size *is* the
feature. The upstream request is made eagerly in `open_asset` so a 404 is still a status
code rather than a truncated body, and the body generator closes the response in a
`finally`, or a few abandoned downloads exhaust the connection pool and every later one
blocks. `Range` is not forwarded, so a download that dies at 90% starts over.

`fetchable_href` rewrites `s3://` to the regionless virtual hosted-style URL. It lives in
`app/tools/stac_search.py`, the module that owns catalog hrefs, and both proxies use it —
bucket and key still come from the item the catalog returned, so the containment above
stays intact. Path style would 301 without a `Location` for anything outside us-east-1.

## Guardrails

**The step cap bounds a turn; the budget bounds the thread.** `max_conversation_turns` and
`max_conversation_cost_usd` (either at 0 disabling its own check) are what the step cap
cannot express: a conversation could be continued forever, each turn resending a history
that only grows, and the step cap would permit every one of them. Both ride the checkpointer
as accumulating state, so they are per thread.

Two consequences that look like bugs otherwise:

- **The cap is crossed, not respected exactly.** `_check_budget` runs *before* a turn, so
  the turn that exceeds the budget runs to completion and the *next* one is refused.
  Checking afterwards would not be a limit — the tokens are already bought.
- **An unrecognized `CLAUDE_MODEL` is priced at the most expensive model known**, not at
  zero. A guardrail that fails open is not a guardrail; erring upwards ends a conversation
  early, which is the survivable direction.

`ConversationBudgetExceeded` is a `RuntimeError` so the streaming path's existing handling
covers it, and its own type so the routes can answer **429** rather than 500 — the request
is well formed and would have been served a few turns ago.

Prices in `MODEL_PRICING` are a transcribed copy that nothing can verify at runtime; the API
bills the account, it does not return a price. The cap bounds an *estimate* of list-price
spend. Cache tokens are priced despite always being zero here, so that adding prompt caching
does not silently start under-reporting.

Measured: a turn chaining two tools costs about $0.048 on `claude-sonnet-4-6`, so the $1.00
and 20-turn defaults bind at roughly the same point. That is a coincidence of the current
defaults, not a design invariant — the turn cap bounds *context growth*, the cost cap bounds
*spend*.

### The rate limiter is keyed on the caller, the budget on the conversation

`app/api/ratelimit.py` exists because the conversation budget **cannot be an abuse control**:
it is keyed on a `conversation_id` the client picks, so a caller that sends a fresh one every
request is bounded by nothing. This one is keyed on the peer address.

Two tiers, because the endpoints cost different things: `/ask` and `/ask/stream` spend money
on a model, `/preview` and `/items` spend bandwidth. `/health` and the built UI are
deliberately unlimited — a limiter that trips the container healthcheck takes the app down,
and one page load is many small files.

Four decisions that look arbitrary until they bite:

- **Raw ASGI, not `BaseHTTPMiddleware`.** That base class re-emits the response through a
  memory stream, which would put it directly in the path of `/ask/stream` — the one endpoint
  built around a long-lived body and a client that may vanish mid-flight. This middleware
  reads the request scope and then gets out of the way, so streaming and disconnect
  behaviour are untouched.
- **A refused request is not recorded.** Appending on refusal would let a client hammering
  the endpoint keep pushing its own window forward, staying locked out long after going
  quiet — a ban rather than a rate limit.
- **`X-Forwarded-For` is ignored unless `RATE_LIMIT_TRUST_PROXY_HEADER` is set**, because a
  header the client sets is a header the client can forge. When trusted, the **rightmost**
  entry is taken — a proxy appends the peer it actually saw, so everything to its left is
  client-supplied.
- **The tracking table is evicted.** An unbounded map keyed by address is itself a memory
  exhaustion vector; the ceiling is the number of clients currently inside the window.

Being outermost is load-bearing and verified: a malformed `/ask` body comes back **429, not
422**, because the refusal happens before the router validates it or opens a session.

It is a sliding window, in process. It dies with the process and each worker keeps its own
tally, so N workers means an effective limit of N x `limit`. That is the accepted trade for
no new dependency and no schema, and `SlidingWindow` is the only thing that would change if
it moved to Redis.

## Observability

A `Turn` rides in the LangGraph context next to the DB session — same reason, it holds a
clock reading and an open span, neither of which belongs in a checkpoint — and records three
things: which tools ran and how they ended, what each model call cost in tokens and
milliseconds, and the cosine distance of every retrieved chunk.

**It is not built by tapping the stream writer**, which is the obvious move and the wrong
one. The timings have to bracket the real work rather than the moment an event was emitted,
and a wire protocol and a telemetry record drift apart the first time either gains a
consumer of its own. The `writer(...)` calls still exist and still serve the SSE client;
they are a different thing that happens to overlap today.

The reason the work was needed at all: `get_stream_writer()` returns a writer that goes
**nowhere** under `.invoke()`, so `/ask` produced no record and an unwatched stream produced
one that was discarded.

Three things that will bite otherwise:

- **`configure_logging()` in `app/main.py` is load-bearing.** Uvicorn configures its own
  loggers and leaves root with no handler, so INFO records propagate to nothing — measured,
  a real question put *zero* trace lines in the server log before it existed. The handler
  goes on `eo_rag`, not root, which would switch on INFO for every library in the process.
- **Langfuse is optional and lazy.** Not configured is silent — it is the default and what
  the test suite runs in; configured but not installed warns once rather than raising at
  startup. `_safe()` swallows exporter failures because telemetry may not become a new way
  for a request to fail; verified against the real SDK pointed at an unreachable host, where
  the turn completed while the export did not.
- **The trace opens before the first frame**, so an abandoned stream still closes it. The
  shortest abandonment of all — a client that disconnects immediately — would otherwise be
  the one turn with no record, and would leak an open span.

Structured logs are the floor and need no account, no key and no dependency:
`docker compose logs api | grep eo_rag.trace` answers "which tools ran, what did it cost".
One JSON object per line, so a question containing newlines cannot split a record and hide
half of it from grep.

**What the first real trace showed**, and why it mattered. One documentation question —
"What are STAC Items and what fields must they have?" — was answered in **6 model calls, 5
`rag_lookup` calls, 23.7s and $0.085**. It hit the step cap. Every retrieval came back with
a best cosine distance between 0.34 and 0.46, which is mediocre: the model kept rephrasing
and re-querying because no lookup returned anything decisive, then answered from the best of
a poor set. None of that is visible in the response, which cites `stac-spec-core` and reads
fine. Worth stating plainly — the observability did not confirm the system was healthy, it
showed that it is not.

## Evaluation

`evals/cases.yaml` is the labelled set, `app/evals/` the harness, `scripts/eval.py` the CLI.

**The labelled set is a file, not a table.** A labelled set is a stated opinion about what a
correct answer is, and opinions belong where they get reviewed: a YAML file diffs in a pull
request, a database row does not. `app/db/models.py` still carries an `EvalCase` table; it
is dead schema, left in place only because dropping it would cost a migration for something
nothing reads.

**Relevance is labelled by section**, because chunk ids are reassigned by every re-ingestion
while the section a fact lives in is a property of the document. That makes the label
coarse, and the three metrics are not equally informative as a result:

- **recall@k saturates** — `Item fields` spans many chunks, so any one of them scores a full
  hit. Useful only as a floor: below 1.0 the right section never came back at all.
- **MRR is the discriminating one**, because the model reads the top of the list first.
- **precision@k is the counterweight** that moves when chunking changes.

The end-to-end check is a deliberately low bar: `expect_tools` is a *subset* check (an extra
tool is thorough, not wrong), `must_contain` is case-insensitive substrings, plus
`expect_sources`. Anything finer either over-fits to wording or needs a model to grade it,
and a grader that costs money per run is a grader people stop running.

Two things the harness does that look like details and are not:

- **It drives `stream_answer`, not `answer_question`.** The tools a turn used are not on the
  `Answer`, but every `tool_start` event names one. So the harness reads the same events an
  SSE client would and needs no change to the API's response shape.
- **Aggregates are compared over the cases both runs contain.** Comparing an average across
  two different case sets is not a comparison: without this, adding one known-failing case
  drops `pass_rate` and trips the gate, which would mean the only way to record a bug is to
  break the build.

The regression gate fails on a case that passed and now fails, or a gated metric falling by
more than `TOLERANCE` (0.05 — retrieval moves on re-ingestion and model calls are not
deterministic, and a gate that cries wolf gets switched off). Cost and latency are reported
but never gated: a run that got cheaper by answering worse must not pass on that.

`--smoke` checks database, Bedrock, catalog and model in about five seconds and for no
tokens — the Models API resolves the configured model without buying a completion. That is
what separates "the prompt regressed" from "Bedrock lost model access in this region".

**Kept out of `pytest` deliberately.** The offline suite is fast because it has no network,
no credentials and no database; this needs all three and costs about a dollar a run. What
*is* in `pytest` is every scoring and comparison function, because a metric nobody checked
is a number rather than a measurement.

**First baseline**, 12 cases against the live stack: 12/12 passed, recall@5 0.944,
precision@5 0.244, MRR 0.800, $0.4521, 197s. Two cases carry the retrieval weakness the
tracing predicted — `collection-extent` finds its section only at rank 5 (MRR 0.20) and
`item-assets` at rank 2 while spending 6 steps and $0.086.

**The harness found a bug in itself first, which is the point.** Its first run reported
`catalog-vs-collection` as a total miss. It was not: `Collection Overview` came back at rank
1 and answers the question directly, while the dedicated `Catalogs vs Collections` section
ranked 6th. The *label* was wrong about where the answer lives. That is recorded in the
case's own note rather than quietly corrected, because a too-narrow label is the failure
that makes an eval actively harmful — it sends somebody off to fix retrieval that was
working.

## The MCP server

`app/mcp/` is a second front end, not a second implementation: the same three tools and the
same corpus, over the Model Context Protocol. `rag_lookup` is included because semantic
search is the actual value of the project and a static resource cannot do it.

Four modules, and **only `server.py` imports `mcp`** — `tools.py` and `resources.py` are
SDK-free so that they and their drift guards run in the default dev environment, where the
optional extra is absent.

The adapters are wrappers rather than decorators on the originals because under MCP the type
hints *are* the schema: `compute_index`'s `bbox` is untyped, `stac_search` takes a
`Sequence`, and `Literal["ndvi","ndwi"]` is how the SDK is told what `COMPUTE_INDEX_TOOL`
hand-writes as an enum. Per-argument descriptions come from `Annotated[..., Field(...)]` and
are copied verbatim from the existing schemas — they were tuned against the live catalog, and
are the reason a client does not invent `sentinel2` or send a bare date as an instant.

Footprints are stripped by default (`include_geometry` opts in) because the SDK puts a
returned model into *both* `structured_content` and the model-visible text — so it is one
choice, not one per consumer. Measured: 2,560 bytes against 3,691 for two scenes.

`app/rag/documents.py` reads the corpus by identity rather than by similarity, kept out of
`app/rag/retrieval.py` because every function there pays Bedrock and these never touch AWS.
Tested against real SQL on in-memory SQLite, so the ordering assertions mean something.

Six things that will bite otherwise, all found while building it:

- **`streamable_http_app()` already serves at `/mcp`.** Mounting it at `/mcp` without
  `streamable_http_path="/"` puts the endpoint at `/mcp/mcp` and leaves `/mcp` a 404 with
  nothing to explain it. The endpoint therefore really lives at `/mcp/`, and a bare `/mcp`
  gets a 307 — every client tried follows it, which is why both spellings work.
- **The session manager must run in the app lifespan**, or every request fails
  `Task group is not initialized`. It is created lazily by `streamable_http_app()`, so the
  lifespan may only touch it after the mount. It can also only be run once per instance, so
  a process cannot start the app twice; the tests reload both modules per test to get a
  fresh one.
- **Host validation refuses anything but localhost by default**, and its patterns
  (`127.0.0.1:*`, `localhost:*`) *require a port* — so a request on 80 or 443 is refused even
  from localhost. `MCP_ALLOWED_HOSTS` is not optional behind a proxy, and the symptom when
  it is missing is `421 Invalid Host header`, which reads as a broken server rather than a
  policy.
- **stdout belongs to the protocol** under stdio. `configure_logging()` defaults to a stdout
  handler; `app/mcp/server.py` passes `sys.stderr`, and must never import `app.main`, which
  configures logging at import.
- **`rag_lookup` returns a `str`, not `LookupResult`** — `scored` holds SQLAlchemy rows
  pydantic cannot schema, and the prose is right anyway.
- **`httpx` and `httpx2` both live here.** The SDK depends on the latter; nothing else does.
  Different distributions, different top-level modules, not a mistake to consolidate. See
  [Known trade-offs](#known-trade-offs).

`/mcp` gets its own rate-limit tier: measured, one session's handshake plus three listings is
**8 HTTP requests**, so sharing `/ask`'s 10/minute would refuse the second client to connect
— and a run at 5/minute failed the handshake outright.

`[project.scripts] eo-rag-mcp` is the project's console entry point, because an MCP client's
config is a `command` plus `args`, and that is more robust than a `python -m` whose meaning
depends on the working directory.

**Three bugs the live checks found**, two of which no unit test could have. The
`/mcp/mcp` mount path, above. Then the one worth the whole exercise: `Mount("/mcp")`
compiles to `^/mcp(?P<path>/.*)$`, which does **not** match `/mcp` itself. In a checkout
that is invisible — the router's `redirect_slashes` covers it — but every deployed image
mounts the built UI at `/`, and `StaticFiles` matches `/mcp` first and answers **405**,
because it serves GET and HEAD only. So `POST /mcp` was 405 in the container while `/mcp/`
was 200, and no test could see it, **because no test run has a frontend build**. Fixed with
an explicit redirect route, and the regression test now creates a real `frontend_dist/` for
the duration of one test. Third: the session manager's once-per-instance rule, above.

## Schema migrations

`alembic/` replaces what used to be a mounted `init_db.sql`. `app/db/models.py` still mirrors
the schema by hand for SQLAlchemy's benefit — Alembic does not read the models at runtime,
only `--autogenerate` would, and the one revision committed so far is hand-written rather
than generated.

That revision is raw SQL using `CREATE ... IF NOT EXISTS` throughout, mirroring the old
`init_db.sql` byte for byte on a fresh database — and, deliberately, **also a no-op on a
database that file already initialized**, which is every volume created before it existed.
Verified against both: a fresh `pgvector/pgvector:pg16` container and one seeded with the old
mount produce the same schema, and only the `alembic_version` table is added.

The image runs migrations for you: `scripts/docker-entrypoint.sh` is the container
`ENTRYPOINT` and calls `alembic upgrade head` before `exec`-ing whatever `CMD` was going to
run. `docker compose up -d db` plus a locally-run API is the one path that does **not** go
through it.

`alembic.ini` carries no `sqlalchemy.url` — `alembic/env.py` imports `app.config.settings`
and sets it from `settings.database_url`, the same URL the app connects with, rather than
keeping a second copy that can drift from `.env`.

## Deployment

Two Terraform stacks, split on lifetime rather than on layer. `infra/persistent/` holds the
S3 state bucket and the ECR repository and is created once, ever; `infra/` holds RDS, the
ALB, ECS, Secrets Manager and IAM, and is created and destroyed cheaply and often. Without
the split, every deploy/destroy cycle would also pay for a Docker build and push of a
`rasterio`/GDAL image.

RDS is destroyed and recreated each cycle (`skip_final_snapshot = true`, on purpose), so
`deploy.sh` always re-runs ingestion — the corpus is 65 KB and ingestion takes seconds, while
RDS provisioning is the slow part.

The default VPC is used deliberately rather than a purpose-built one: a private-subnet design
would need a NAT Gateway, the single largest line item it would add. RDS sits in a public
subnet with `publicly_accessible = false`, so it never actually gets a public IP.

Full detail, including sizing and the explicit no-authentication warning, is in
[infra/README.md](../infra/README.md).

## Testing

The suite runs fully offline: no AWS credentials, no network, no database. Every module that
talks to the outside world builds its client lazily into a module-level `_cached_client`
behind a `_client()` function, and tests monkeypatch that function. Each of those files also
has an import-purity test that reloads the module with the client constructor sabotaged —
nothing in `app/` should acquire a client or connection at import time.

The graph is tested by scripting the sequence of replies a fake Anthropic client hands back,
which is what makes the multi-turn branches reachable. Those fakes use the **real** SDK block
types (`TextBlock`, `ToolUseBlock`) rather than stand-ins, because the graph calls
`model_dump()` on them and the checkpointer then serializes the result — a hand-rolled fake
hid both steps and let a serialization bug through. The tools' test fakes return the real
pydantic models for the same reason: a fake of a shape the graph could not actually consume
hides the bug rather than catching it.

`compute_index` is tested on real GeoTIFFs written to `tmp_path` — rasterio opens a local
path exactly as a remote one, so the windowing, masking and scaling under test are the real
thing and only the transport is missing.

**The suite must pass with and without each optional extra.** Tracing works without Langfuse
and the MCP adapters are testable without the SDK; that is the point of both extras being
separate. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the exact commands.

The frontend tests only the two pieces with real edge cases: the SSE frame parser (chunk
boundaries, a multi-byte character split across two reads) and `imageCorners`. Everything
visual is verified in [VERIFY.md](../VERIFY.md) instead, which is this project's stated
division of labour.

**And the offline suite is structurally blind to one class of bug**: the external service
behaving differently from how we imagined it. Three were found only by running against live
services — the bare-date rejection, a `MAX_TOKENS` truncation, and the reflectance offset. A
captured live response (`tests/fixtures/earth_search_search.json`) is in the suite for that
reason, but it is a snapshot, not a substitute.

## Known trade-offs

Things that are deliberate, documented, and not going to be "fixed":

- **`httpx` and `httpx2` are both installed** once the `mcp` extra is in. The SDK depends on
  the second; nothing else does. They are different distributions with different top-level
  modules, so this is not a duplicate to consolidate — and the choice belongs to the MCP SDK,
  not to this project.
- **Conversation history is in memory** and dies with the process. Persisting it needs
  `langgraph-checkpoint-postgres`; the state shape is already ready for it.
- **The rate limiter is per process.** N workers means an effective limit of N x the
  configured value.
- **Cache-token pricing is implemented but exercises nothing**, since no request here sets
  `cache_control`. It is priced anyway so that adding prompt caching later does not silently
  start under-reporting.
- **`EvalCase` is dead schema.** See [Evaluation](#evaluation).
- **`Range` is not forwarded** on asset downloads, so a download that dies at 90% starts over.
