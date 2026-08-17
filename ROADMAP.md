# Roadmap

Activity list for the architecture described in `eo-copilot-architettura.md`.
**Steps 0-10 are all complete.** What remains is the Cross-cutting list at the end, which
is where the next work is.

Each step records what was built at that point, so an entry can be superseded by a
later step without being wrong: step 2's single `messages.create` is what step 3b
replaced with the tool-use loop.

Legend: `[x]` done, `[ ]` not started, `[~]` partially done (groundwork in place).

---

## Step 0 - Setup ✅

- [x] `pyproject.toml` with `uv` as the dev tooling (`--extra dev` pulls pytest/ruff)
- [x] `docker-compose.yml`: `pgvector/pgvector:pg16` (:5432) with healthcheck + API (:8000, `--reload`)
- [x] `Dockerfile` for the API service
- [x] Configuration through `pydantic-settings` (`app/config.py`) reading `.env`, with `.env.example`
- [x] `postgresql://` URLs pinned to psycopg v3 by a field validator (we do not depend on psycopg2)
- [x] `scripts/init_db.sql` mounted into `/docker-entrypoint-initdb.d/`: `vector` extension,
      `doc_chunks`, `eval_cases`, IVFFlat cosine index
- [x] `GET /health`

## Step 1 - Ingestion ✅

- [x] Structural markdown chunking (`split_markdown`): headers first, then paragraphs/sentences
      at 800 chars with 100 of overlap
- [x] Headers kept in the content (`strip_headers=False`); `section` falls back to the `#` title
- [x] `_merge_header_only_chunks` post-pass: header-only chunks are prepended to the next chunk
      of the same section, or dropped if that section has no body
- [x] Embeddings via Bedrock / Titan Text Embeddings V2, isolated in `app/rag/embeddings.py`
      so model and dimension cannot drift between indexing and querying
- [x] boto3 client built lazily and cached (`_cached_client`): importing the app never touches AWS
- [x] `AccessDeniedException` turned into an actionable message (Bedrock model access)
- [x] `DocChunk` model mirroring the SQL schema by hand
- [x] CLI: `python -m app.rag.ingest <path> --source <name> [--url <url>]`
- [x] Offline tests for the chunking contract (`tests/test_ingest.py`) and for Bedrock
      (`tests/test_embeddings.py`, with a fake client)

Deliberately not wired to the API: ingestion is a CLI-only path.

## Step 2 - Minimal RAG endpoint ✅

- [x] `embed_query` reusing the ingestion-time embedding model
- [x] Cosine top-k over pgvector (`retrieve`, `retrieve_with_scores`)
- [x] `format_context` with the `[Source: <source> - <section>]` citation label
- [x] `POST /ask`: single context-only `messages.create`, answer plus deduped sorted sources
- [x] `scripts/retrieve_test.py`: inspect the raw ranking with cosine similarity, no LLM in the way
- [x] Offline endpoint tests (`tests/test_ask.py`), fake LLM and fake retrieval
- [x] Regression test: importing `app.rag.embeddings` / `app.rag.retrieval` builds no boto3 client

---

## Step 3 - `stac_search` tool ✅

The first real tool. Nothing to orchestrate in step 4 until this exists.

**3a - the tool itself, without touching the API** ✅

- [x] `app/tools/stac_search.py`: httpx client against `settings.stac_api_url`
      (Earth Search v1), `POST /search` with `bbox`, `datetime`, `collections`, `limit`
- [x] `max_cloud_cover` mapped onto the query extension (`eo:cloud_cover` `lt`)
- [x] Lazy, cached client on the `app/rag/embeddings.py` model: no connection at import time
- [x] Compact projection of the response (`id`, `collection`, `datetime`, `cloud_cover`,
      `platform`, `bbox`) instead of the raw GeoJSON, which would flood the model's context
- [x] Asset names always returned; hrefs only for previews, or for the keys the caller asks
      for through `asset_keys` — the hook compute_index will use for its bands
- [x] bbox validated before the request is spent, antimeridian crossing allowed
- [x] Bare dates completed to full RFC 3339 (`_normalize_datetime`): the catalog 400s on
      `2024-01-01`, and a lone date as an instant silently matches nothing
- [x] `limit` clamped to `MAX_LIMIT`
- [x] Error handling: non-200 (with the catalog's own message), timeout, unreachable host,
      non-JSON body. Empty result sets are a result, not an error.
- [x] Anthropic tool schema (`STAC_SEARCH_TOOL`), with `asset_keys` deliberately not exposed
- [x] Offline tests with a fake client over a **live captured** Earth Search response
      (`tests/fixtures/earth_search_search.json`), plus a drift guard tying the tool schema
      to the function signature and an import-purity test
- [x] Verified end to end against the live catalog: bare interval, single day, open ended,
      no datetime, explicit `asset_keys`, and a rejected datetime

**3b - tool-use loop in `/ask`** ✅

- [x] `app/tools/rag_lookup.py` wrapping `retrieve` + `format_context`, with its own schema
- [x] `app/agents/loop.py`: offers both tools, iterates on the results, returns an `Answer`
      (text, sources, steps)
- [x] Capped by `settings.max_agent_steps`, until now defined and never read. Once spent, a
      final call is made with no `tools` at all, so the caller always gets an answer.
- [x] Failing tools come back to the model as errored `tool_result`s rather than a 500
- [x] `SYSTEM_PROMPT` rewritten: "answer only from the provided context" was wrong once the
      model can go and fetch things itself
- [x] Sources accumulated across tool calls (documentation sources for `rag_lookup`, the
      catalog URL for `stac_search`), deduped and sorted
- [x] `/ask` reduced to an adapter; `tests/test_ask.py` now covers the HTTP contract only,
      with the loop's own behaviour in `tests/test_agent_loop.py`
- [x] Anthropic client made lazy on the way through (was built at import time in `routes.py`)
- [x] Verified live end to end (`VERIFY.md`): documentation question, catalog question, and
      one question chaining both tools. `MAX_TOKENS` raised from step 2's 1000 to 4096 —
      a two-tool answer was being cut off mid-sentence with nothing signalling it.

## Step 4 - Orchestration with LangGraph ✅

- [x] `app/agents/graph.py`: `START → agent → (tool_use?) → tools → agent → … → END`,
      replacing the hand-rolled loop of step 3b
- [x] Router — **built as the conditional edge, not as the separate classifier this entry
      originally called for.** A hard upfront "documentation vs. data vs. computation"
      decision cannot express *both*, and the question that chains `rag_lookup` and
      `stac_search` is the one verified live. It would also spend an extra model call to
      decide what the model decides for free in the turn it is already taking.
- [x] Conversational memory: `MemorySaver` keyed by `thread_id`, `conversation_id` on the
      request and the response
- [x] Behaviour of step 3b preserved — its tests carried over almost verbatim: step cap,
      tools-withheld final call, errored tool_results, source accumulation
- [x] Only `messages` accumulates; `steps` and `sources` are per turn, so a follow-up
      answered from history correctly reports no sources
- [x] State holds plain dicts, not SDK block objects, so it survives the checkpointer; the
      DB session travels in the LangGraph context and is never checkpointed
- [x] `recursion_limit` derived from `max_agent_steps` instead of LangGraph's default 25
- [x] Verified live: two-turn conversation resolving a pronoun from history without
      re-searching, and a fresh thread correctly seeing no history
- [~] Persist conversation state — in memory only. Postgres needs the separate
      `langgraph-checkpoint-postgres` package; the state shape is already ready for it.

## Step 5 - `compute_index` tool (NDVI) ✅

- [x] `rasterio` added to the dependencies, and `numpy` next to it: `compute_index`
      imports it directly rather than relying on rasterio to pull it in
- [x] `libexpat1` installed in the image — rasterio's wheels bundle GDAL but still link
      against the system libexpat, which `python:3.12-slim` omits
- [x] `app/tools/compute_index.py`: COG assets opened by href over `/vsicurl/`, read
      **windowed** on the bbox. A full Sentinel-2 tile is 10980x10980 per band.
- [x] NDVI and summary statistics — **with percentiles, not just the mean**, which over
      mixed land cover hides the split it is being asked about
- [x] `ndwi` came along with `ndvi` for the cost of a dict entry in `INDICES`, and pays
      for itself in verification: water gives negative NDVI and positive NDWI, so the
      sign flip proves the bands are not swapped
- [x] `fetch_item` added to `stac_search`: `compute_index` needs the `raster:bands`
      scale, offset and nodata that `_summarize_item` deliberately drops
- [x] Guardrails — **`MAX_PIXELS` decimates rather than refuses.** "Too big, try again"
      spends a model turn on something we can just handle. Measured live: the whole tile
      at 40 m came back *faster* than a 5 km window at 10 m, because GDAL serves the
      decimated read from the COGs' internal overviews. So no wall-clock cap was added —
      there was no slow read to bound.
- [x] Reflectance conversion from `raster:bands` rather than hardcoded constants: a
      normalized difference is invariant to a common scale but **not** to a common offset,
      and Sentinel-2 baselines from 04.00 on carry one
- [x] **The declared offset is checked against the pixels before being applied.** Earth
      Search advertises `offset: -0.1` on every sentinel-2-l2a item while the
      sentinel-cogs COGs hold unshifted DNs; applying it as advertised put 68% of `red`
      at negative reflectance and sent NDVI to -4.8e11. Negative reflectance is
      impossible, so a band producing much of it is evidence the offset does not describe
      it (`_offsets_fit_the_pixels`). Decided from the data, and jointly for the band
      pair, so the tool stays right for catalogs whose pixels *are* shifted.
- [x] The index is confined to [-1, 1] **by construction**: excluding negative
      reflectance leaves the denominator a sum of non-negative terms, so nothing needs
      clipping after the fact
- [x] Statistics come back, never an array
- [x] Offline tests on real GeoTIFFs written to `tmp_path` — rasterio opens a local path
      exactly as a remote one, so the windowing, masking and scaling under test are the
      real thing and only the transport is missing
- [x] Verified live (`VERIFY.md` step 9), which is where the offset bug was found: the
      offline tests invent the metadata and the pixels together, so they always agree

## Step 6 - Chat frontend + map ✅

- [x] `frontend/`: React + Vite + TypeScript, Tailwind v4, MapLibre GL. Two panes -
      conversation left, map right - with the dev server proxying the API, so there is
      no cross-origin request in development and **no CORS middleware was added**: in
      production FastAPI serves the built assets itself from the same port
- [x] Token-level streaming on a new `POST /ask/stream`, alongside an unchanged `/ask`.
      One JSON object per `data:` line with the type inside it, not named `event:` lines
- [x] Streaming built on LangGraph's **custom channel with a synchronous node**, not the
      async one this entry originally implied. An async-only `agent` makes
      `graph.invoke()` raise, so `answer_question` would need an `asyncio.run` bridge -
      and a module-cached `AsyncAnthropic` holds connections bound to the loop that made
      them, so the second `/ask` onwards would 500 with `Event loop is closed`
      (reproduced). `get_stream_writer()` returns a writer that goes nowhere under
      `.invoke()`, so one node body serves both entry points and there is no async twin
      to keep in step
- [x] `tool_start` / `tool_end` events carrying the arguments, the outcome and the
      elapsed time - the first thing outside the model's own context to say that a tool
      failed and the answer worked around it
- [x] `_repair_interrupted_turn`: a client that stops listening abandons the graph
      between `agent` and `tools`, leaving a checkpointed `tool_use` that nothing will
      answer. Anthropic refuses that history, so the damage lands on **every later turn**
      of the thread and one Stop ended the conversation for good. Found while verifying
      the Stop button; the repair injects errored `tool_result`s on the way in, since by
      the time it is detectable the response generator is already closing
- [x] Real footprints, hidden from the model: `_summarize_item` carries `geometry`,
      `model_view` strips it before the tool result, and a per-turn `features` field on
      the state carries it to the map. Measured live: 1699 bytes to the model, polygons
      to the UI
- [x] `features` is per-turn like `sources`, and the stream sends **no event at all**
      when a turn ran no tools - so a follow-up about the scenes already on screen leaves
      them there, and the frontend has no rule to get wrong
- [x] The bbox fallback (`_polygon_from_bbox`) lives in `stac_search` next to
      `_validate_bbox`, and splits an antimeridian-crossing bbox into a MultiPolygon:
      the module that owns [west, south, east, north] is the one that knows west may be
      greater than east
- [x] `steps` exposed on the streaming path only, as a first slice of step 8's
      observability. `/ask`'s response shape is unchanged and still tested as such
- [x] Basemaps chosen to need no API key: OpenFreeMap by default, Esri World Imagery as
      an optional toggle added *into* the style rather than through `setStyle`, which
      would tear down the source and every layer on each switch
- [x] Quicklooks: clicking a footprint lays the scene's preview over it as an `image`
      source, positioned on the **four corners** rather than the bbox — an MGRS tile is a
      rotated quadrilateral, and the bbox would stretch the image into that rotation.
      Toggled per scene, so overlapping tiles can be compared
- [x] `GET /preview/{item_id}` (`app/api/preview.py`): previews are **proxied**, never
      loaded from the asset host by the browser. The map needs the image as a WebGL
      texture, which makes it a CORS request against a host that owes us nothing — and
      S3 sends `Access-Control-Allow-Origin` only when `Origin` is present, while the
      response without it carries no `Vary`, so a single plain `<img>` load poisons the
      cache entry for every later CORS request to that URL. Found live, twice: first as
      a CORS failure, then again after `crossOrigin="anonymous"`, which only spread the
      breakage to the cards. **The endpoint takes an item id, not a URL**, so the only
      hrefs it can fetch are the ones the configured catalog returned; it also refuses
      non-renderable types (Earth Search's `overview` is a GeoTIFF) and caps the body
- [x] Multi-stage `Dockerfile` with a node build stage; `app/main.py` mounts the result
      only if it exists, so `uvicorn app.main:app --reload` on an unbuilt checkout still
      runs and the tests never depend on a build artifact
- [x] Verified live (`VERIFY.md` step 10): footprints reached the map at 4.18s against a
      first token at 5.24s, coordinates `[lon, lat]` over Lazio, the follow-up left the
      map untouched, and the built image served the same UI from `:8000`

Note for whoever reads the streamed text next to `done.answer`: they differ on purpose.
Tokens come from *every* agent turn, including the preamble the model writes alongside a
tool call; `_answer_text` reads the last turn alone. The UI renders the stream and falls
back to `answer` only when no token arrived.

## Step 7 - Guardrails and structured output ✅

- [x] `settings.max_agent_steps` enforced by the tool-use loop (step 3b)
- [x] System prompt rewritten for tool use (step 3b)
- [x] Structured output for tool results: every tool now returns a pydantic model rather
      than a dict - `SearchResult`/`ItemSummary`, `LookupResult`, `IndexResult` with
      `Statistics`, `Bands`, `Reflectance`, `PixelCounts`. `_run_tool` is the single
      place they become the string a `tool_result` carries
- [x] The benefit is **not mainly validation** - it is that a projection with two
      consumers can name what it hands each of them. `model_view` excludes
      `{"items": {"__all__": {"geometry"}}}` by field instead of filtering keys by
      string, and `index_footprint` reads `result.statistics` instead of hoping the key
      is there
- [x] The tools' test fakes return those same models, on the principle already applied
      to the Anthropic block types: a fake of a shape the graph could not really consume
      hides the bug instead of catching it
- [x] Input validation: bbox (step 3a), date ranges and allowed collections - the last
      two existed but were **untested and unpublished**. Both now have their own tests,
      including the backwards interval Earth Search accepts and silently matches nothing
      for
- [x] The allowlist is **named in the tool schema**, not just enforced. Rejecting
      `sentinel2` without saying what exists costs a whole round trip to learn it, and
      the real ids are not guessable from the name of a satellite. Not an `enum`: an
      empty allowlist has to mean "no constraint", and an empty enum forbids everything
- [x] Cost cap and turn cap per conversation (`max_conversation_turns`,
      `max_conversation_cost_usd`, either at 0 disabling its check). `app/agents/cost.py`
      prices each call from the `usage` the response reports; `cost_usd` and `turns` are
      the first state fields since `messages` with **accumulating** reducers, so the
      budget rides the checkpointer and is per thread
- [x] **The cap is crossed, not respected exactly.** The check runs before a turn, so the
      turn that exceeds the budget completes and the next is refused - the only honest
      way to bound something whose cost is unknown until it has been paid
- [x] An unknown `CLAUDE_MODEL` is priced at the **most expensive** model in the table,
      not at zero: a guardrail that fails open is not a guardrail, and a misconfigured
      model would otherwise disable the cost cap silently
- [x] `429` on both endpoints, not 500 - the request is fine, a limit was enforced. The
      streaming path checks in the **route**, before the generator exists, because that
      is the last moment a status code can still be chosen; `stream_answer` keeps its own
      check as the guarantee
- [x] Measured: a turn chaining two tools costs about $0.048 on `claude-sonnet-4-6`, so
      the $1.00 default and the 20-turn default bind at roughly the same point. That is a
      coincidence of the current defaults, not a design invariant - the turn cap is the
      one that bounds *context growth*, the cost cap the one that bounds *spend*

- [x] A **real rate limiter** (`app/api/ratelimit.py`), added after the budget above made
      it obvious that a cap keyed on a client-supplied `conversation_id` bounds nothing:
      omit it, or send a fresh one each request, and the budget never applies. Keyed on
      the peer address, sliding window, in process
- [x] Two tiers - `/ask` strict, the preview and asset proxies loose - because a model
      call and a thumbnail do not cost the same thing. `/health` and the static UI are
      unlimited on purpose: a limiter that trips the container healthcheck takes the app
      down
- [x] **Raw ASGI middleware, not `BaseHTTPMiddleware`**, which re-emits the response
      through a memory stream and would sit directly in the path of `/ask/stream` - the
      endpoint whose disconnect behaviour step 6 spent a bug on. This one reads the
      request scope and then gets out of the way
- [x] A refused request is **not** recorded: otherwise a client hammering the endpoint
      keeps pushing its own window forward and stays locked out after going quiet, which
      is a ban rather than a rate limit
- [x] `X-Forwarded-For` ignored unless a proxy is declared, and the **rightmost** entry
      taken when it is - a header the client sets is a header the client can forge, and
      trusting it with nothing in front is a bypass rather than a feature
- [x] The tracking table is evicted by expiry, since an unbounded map keyed by address is
      itself a memory exhaustion vector
- [x] Verified live: `/health` unaffected at any rate, `/ask` refused with a `Retry-After`
      that counts down, the two tiers holding separate budgets, and a **malformed `/ask`
      body answered 429 rather than 422** - proof the refusal lands before the router
      validates it or opens a database session

Not done, and deliberately: cache-token pricing is implemented but exercises nothing,
since no request here sets `cache_control`. It is priced anyway so that adding prompt
caching does not silently start under-reporting.

The rate limiter is **in process**, so it dies with the process and each worker keeps its
own tally - N workers means an effective limit of N times the configured one. That is the
accepted trade for adding no dependency and no schema; `SlidingWindow` is the only thing
that changes if it moves to Redis.

## Step 8 - Observability ✅

- [x] Tracing of LLM calls and tool calls (`app/obs/tracing.py`). A `Turn` travels in the
      LangGraph **context** next to the DB session, for the same reason: it holds a clock
      reading and an open span, and neither belongs in a checkpoint
- [x] The diagnosis this step started from was right and slightly understated. `/ask`
      produced no record of anything, because `get_stream_writer()` returns a writer that
      goes **nowhere** under `.invoke()` — and a stream nobody watched produced one that
      was thrown away. `sources` really was the only external signal
- [x] **Not** built by tapping the stream writer, which was the obvious move. The timings
      have to bracket the real work rather than the moment an event was emitted, and a
      wire protocol and a telemetry record drift apart the first time either gains a
      consumer of its own
- [x] Per-request token, cost and latency accounting, reusing step 7's `turn_cost_usd`.
      One `generation` record per model call, so a turn that chained five tool calls shows
      six of them and the totals are the sum
- [x] Retrieval quality logging: `rag_lookup` now calls `retrieve_with_scores` rather than
      `retrieve`. The distances were being computed and discarded anyway — the ranking is
      a `cosine_distance` ORDER BY either way — so this costs one float per chunk
- [x] `LookupResult.scored` is a **third** split alongside `context` and `sources`, and
      reaches neither the model nor the answer: the same idea as `ItemSummary.geometry`,
      which the map needs and the model is only made worse by
- [x] Structured logs are the floor and need no account, no key and no dependency:
      `docker compose logs api | grep eo_rag.trace` answers "which tools ran, what did it
      cost". One JSON object per line, so a question containing newlines cannot split a
      record and hide half of it from grep
- [x] **The floor was silently broken and had to be fixed.** Uvicorn configures its own
      loggers and leaves root with no handler, so INFO records propagated to nothing:
      measured, a real question answered end to end put **zero** trace lines in the server
      log. `configure_logging()` attaches a handler to `eo_rag` — not to root, which would
      switch on INFO for every library in the process
- [x] Langfuse as an optional exporter (`app/obs/langfuse_exporter.py`), lazy and cached
      like every other client here, in the `observability` extra rather than the base
      dependencies — it is an OpenTelemetry distribution and pulls a tree larger than
      everything else combined, while the tracing is fully useful without it
- [x] Three states, one of which is not a failure: **not configured** (silent, the default
      and what the test suite runs in), **configured but not installed** (one warning, not
      an ImportError at startup), **configured and installed**
- [x] Telemetry may not become a new way for a request to fail. Verified against the real
      SDK pointed at an unreachable host: spans were created and queued, the OTLP export
      failed on its background thread, and the turn completed normally
- [x] An abandoned stream still closes its trace. The `Turn` is opened **before** the
      first frame goes out, so the shortest abandonment of all — a client that disconnects
      immediately — is not the one turn with no record
- [x] Verified live end to end (`VERIFY.md` step 12), which is where it earned its keep
      immediately: see the note below

**What the first real trace showed.** One documentation question, "What are STAC Items and
what fields must they have?", answered in **6 model calls, 5 `rag_lookup` calls, 23.7s and
$0.085** — it hit the step cap. Every retrieval came back with a best cosine distance
between **0.34 and 0.46**, which is mediocre: the model kept rephrasing and re-querying
because no lookup returned anything decisive, and then answered from the best of a poor
set. None of that is visible in the response, which cites `stac-spec-core` and reads fine.

That is one question costing 8.5% of the default $1.00 conversation budget, and it is the
kind of thing step 9's eval harness and a chunking revisit should act on. Worth stating
plainly: the observability did not confirm the system was healthy, it showed it is not.

## Step 9 - Eval harness ✅

Step 8 gave this one its reason: the first live trace showed retrieval distances of
0.34-0.46 on a core documentation question and five lookups to answer it. Metrics against
a labelled set are how that stops being one anecdote.

- [x] The labelled set is `evals/cases.yaml`, **not** the `eval_cases` table. A labelled
      set is a stated opinion about what a correct answer is, and opinions belong where
      they get reviewed: a file diffs in a pull request, a row does not. The table is left
      as dead schema rather than dropped - `init_db.sql` runs once per volume, so removing
      it would cost a migration for something nothing reads
- [x] Relevance labelled by **section**, because chunk ids are reassigned by every
      re-ingestion while the section a fact lives in is a property of the document
- [x] Retrieval metrics (recall@k, MRR, precision@k), and the honest reading of them: the
      section label is coarse, so **recall saturates and MRR is the discriminating one**.
      `Item fields` spans many chunks, and retrieving any of them is a full recall hit
- [x] End-to-end eval: `expect_tools` (a **subset** check - an extra tool is thorough, not
      wrong), `must_contain` case-insensitive substrings, and `expect_sources`. A low bar
      on purpose: anything finer either over-fits to wording or needs a model to grade it,
      and a grader that costs money per run is a grader people stop running
- [x] The runner drives `stream_answer`, not `answer_question`: the tools a turn used are
      not on the `Answer` (and `tests/test_ask.py` pins that response shape), but every
      `tool_start` event names one. No change to the API to make it measurable
- [x] Saved baseline plus regression comparison, gating on exactly two things: a case that
      passed and now fails, and a gated metric falling by more than `TOLERANCE` (0.05).
      **A new failing case is not a regression** - adding one is how a bug gets recorded,
      and gating on it would mean the only way to write a failing test is to break the build
- [x] Aggregates are compared over the cases **both runs contain**. Caught by its own test:
      without it, adding one known-failing case drops `pass_rate` and trips the gate on
      arithmetic rather than on anything having got worse
- [x] Cost and latency are reported but never gated - a run that got cheaper by answering
      worse must not be able to pass on that
- [x] Smoke checks (`--smoke`): database, Bedrock, catalog and model, in five seconds and
      for no tokens - the Models API resolves the configured model without buying a
      completion. This is what separates "the prompt regressed" from "Bedrock lost model
      access in this region"
- [x] Kept out of `pytest` deliberately. The offline suite is fast because it has no
      network, no credentials and no database; this needs all three and costs a dollar a
      run. What *is* in `pytest` is every scoring and comparison function, because a metric
      nobody checked is a number rather than a measurement

**First baseline**, 12 cases against the live stack: **12/12 passed, recall@5 0.944,
precision@5 0.244, MRR 0.800, $0.4521, 197s.** Two cases carry the retrieval weakness step
8 predicted — `collection-extent` finds its section only at rank 5 (MRR 0.20) and
`item-assets` at rank 2 while spending 6 steps and $0.086.

**The harness found a bug in itself first, which is the point.** Its first run reported
`catalog-vs-collection` as a total miss. It was not: `Collection Overview` came back at
rank 1 and answers the question directly, while the dedicated `Catalogs vs Collections`
section ranked 6th. The **label** was wrong about where the answer lives. That is recorded
in the case's own note rather than quietly corrected, because a too-narrow label is the
failure that makes an eval actively harmful — it sends somebody off to fix retrieval that
was working.

## Step 10 - MCP server ✅

- [x] `stac_search`, `compute_index` **and `rag_lookup`** as MCP tools. The third was not
      in this entry; it is here because semantic search is the actual value of the project
      and a static resource cannot do it
- [x] The documentation as MCP resources: `docs://sources` (the index), plus templates
      `docs://document/{source}` and `docs://section/{source}/{section}`
- [x] **Both transports from one definition**: `python -m app.mcp.server` / `eo-rag-mcp`
      for stdio, and the same object mounted at `/mcp` by `app/main.py` for streamable HTTP
- [x] Four modules, and **only `server.py` imports `mcp`**. `tools.py` and `resources.py`
      are SDK-free, which is what lets them and their drift guards run in the default dev
      environment where the optional extra is absent - the extra costs nothing in testing
- [x] Adapters rather than decorators on the originals, because under MCP the type hints
      *are* the schema: `compute_index`'s `bbox` was untyped, `stac_search` takes a
      `Sequence`, and `Literal["ndvi","ndwi"]` is how the SDK is told what
      `COMPUTE_INDEX_TOOL` hand-writes. A test ties that Literal back to `INDICES`
- [x] Per-argument descriptions carried over verbatim through
      `Annotated[..., Field(description=...)]`, including the collection allowlist. They
      were tuned against the live catalog and are the reason a client does not invent
      `sentinel2` or send a bare date as an instant
- [x] Footprints stripped by default, `include_geometry` to opt in - **one choice, not one
      per consumer**, because the SDK puts a returned model into *both* `structured_content`
      and the model-visible text. Measured live: 2,560 bytes against 3,691 for two scenes
- [x] `rag_lookup` returns a `str`. `LookupResult.scored` holds SQLAlchemy rows pydantic
      cannot schema, and the prose is right anyway - the same reason `_run_tool` does not
      `json.dumps` it
- [x] `app/rag/documents.py`: reading the corpus by identity rather than similarity, kept
      out of `app/rag/retrieval.py` because every function there pays Bedrock and these
      never touch AWS. Tested against **real SQL** on in-memory SQLite, so the ordering
      assertions mean something
- [x] Its own rate-limit tier. Measured: one session's handshake plus three listings is
      **8 HTTP requests**, and a run at 5/minute failed the handshake outright - which is
      exactly what sharing `/ask`'s 10/minute would have done to the second client
- [x] `[project.scripts] eo-rag-mcp` - the project's first console entry point, because an
      MCP client's config is a `command` plus `args` and that is more robust than a
      `python -m` whose meaning depends on the working directory
- [x] The image installs `.[mcp]`: the deployed API is what serves `/mcp`, and an image
      where the mount is silently absent is the footgun this project documents its way out of
- [x] Verified live (`VERIFY.md` step 14): stdio driven as a real JSON-RPC client, the SDK's
      own HTTP client against the container, resources read, `compute_index` over MCP
      **identical to a direct call**, and a broken `DATABASE_URL` contained to `rag_lookup`
      while the transport and the network-only tools carried on

**Three bugs the live checks found, two of which no unit test could have.**

`streamable_http_app()` already serves at `/mcp` inside its own app, so the documented
`app.mount("/mcp", ...)` puts the endpoint at **`/mcp/mcp`**. Fixed with
`streamable_http_path="/"`, and pinned by a test.

Then the one worth the whole exercise: `Mount("/mcp")` compiles to `^/mcp(?P<path>/.*)$`,
which does **not** match `/mcp` itself. In a checkout that is invisible - the router's
`redirect_slashes` covers it - but every deployed image mounts the built UI at `/`, and
`StaticFiles` matches `/mcp` first and answers **405**, because it serves GET and HEAD only.
So `POST /mcp` was 405 in the container while `/mcp/` was 200, and **no test could see it,
because no test run has a frontend build**. Fixed with an explicit redirect route, and the
regression test now creates a real `frontend_dist/` for the duration of one test.

Third: a session manager **can only be run once per instance**, so a process cannot start
the app twice - which is why the MCP tests reload both modules per test rather than reusing
one `TestClient`.

Also worth writing down: DNS-rebinding protection is on by default and its patterns
(`127.0.0.1:*`, `localhost:*`) *require a port*, so a request arriving on 80 or 443 is
refused even from localhost. `MCP_ALLOWED_HOSTS` is not optional behind a proxy.

---

## Cross-cutting

Not roadmap steps, but they get more expensive the longer they wait — and with steps 0-10
done, this list *is* the backlog. **Version control and CI are the two that now matter
most**: there are ten steps of decisions here and no history of any of them, and the MCP
step alone added a Dockerfile change and an image-only bug that only a build would catch.

- [ ] **Two optional extras now, and the "passes without them" rule is manual.** `uv run
      --extra dev` is additive and will not remove a previously installed extra, so the
      without-the-extra run needs `uv sync --extra dev` first. That is exactly the kind of
      thing CI should be doing rather than a person remembering.
- [ ] **`httpx` and `httpx2` are both installed** once the `mcp` extra is in — the SDK
      depends on the second, nothing else does. Not a fault, but worth knowing before
      somebody "consolidates" them.

- [ ] **Version control**: the project is not a git repo. No diffs, no undo, right as the first
      non-trivial component lands. Step 6 roughly doubled the file count — this will never
      be cheaper to fix than it is now.
- [ ] **The build step is unguarded**: `frontend/` is compiled in the image's node stage,
      and nothing checks it. A TypeScript error fails `docker compose build`, which is the
      worst place to find one.
- [x] ~~`Anthropic(...)` built at import time~~ — fixed in step 3b: the client is now lazy and
      cached in `app/agents/graph.py`, like the Bedrock and STAC ones
- [ ] **Embedding dimension duplicated in three places**: `settings.embedding_dim`,
      `Vector(...)` in `app/db/models.py`, `vector(1024)` in `scripts/init_db.sql`.
      A mismatch only shows up at insert time.
- [ ] **No migrations**: `init_db.sql` runs only when the data volume is first created, and the
      SQLAlchemy models mirror it by hand
- [ ] **No CI**: ruff and pytest only run when someone remembers to run them, and since
      step 6 that goes for `npm run build` and `vitest` too
- [ ] **No `[tool.ruff]` section** in `pyproject.toml`: line length and rule set are implicit
- [ ] **`embed_texts` is sequential** — one InvokeModel call per text. Fine at this corpus size;
      the docstring already flags `ThreadPoolExecutor` as the way out.
