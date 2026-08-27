# Architecture

How a question becomes an answer, and why the pieces sit where they do.

This is the human-readable companion to [CLAUDE.md](../CLAUDE.md), which carries the same
material at module level for anyone (or anything) editing the code. For the reasoning
behind individual decisions, see [decisions.md](decisions.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/architecture-dark.svg">
  <img alt="EO-RAG system architecture: clients, the FastAPI application containing the rate limiter, endpoints, LangGraph agent and three tools, and the external services each tool talks to." src="images/architecture.svg">
</picture>

## The shape of it

One FastAPI process serves everything: the JSON API, the built React interface, and the
MCP endpoint. There is no separate worker, queue or scheduler. A question is answered
inside the request that asked it.

Behind that sits a single LangGraph agent with three tools. Claude decides which to call
and in what order; the graph runs them and hands the results back until the model produces
text instead of another tool call.

| Layer | Module | Responsibility |
|---|---|---|
| Rate limiting | `app/api/ratelimit.py` | Per-caller sliding window, outermost in the stack |
| Endpoints | `app/api/routes.py`, `preview.py`, `assets.py` | Thin adapters; no business logic |
| Agent | `app/agents/graph.py` | The loop, the step cap, the conversation budget |
| Tools | `app/tools/` | `rag_lookup`, `stac_search`, `compute_index` |
| Retrieval | `app/rag/` | Chunking, embeddings, cosine search over pgvector |
| Observability | `app/obs/` | Per-turn trace, optional Langfuse export |
| MCP | `app/mcp/` | The same three tools over the Model Context Protocol |

## The lifecycle of a request

1. **Rate limiter.** Raw ASGI middleware, outermost. It reads the request scope, decides,
   and gets out of the way. Because it runs before routing, a malformed `/ask` body comes
   back `429` rather than `422` — the refusal happens before anything is parsed or any
   database session is opened.
2. **Route.** `/ask` and `/ask/stream` both validate the body, open a SQLAlchemy session as
   a dependency, and call into the graph. They contain no logic of their own.
3. **Budget check.** Before the turn runs, the accumulated turn count and estimated cost
   for this `conversation_id` are compared against their caps. Over budget is a `429`.
4. **Repair.** `_repair_interrupted_turn` inspects the thread's history for a `tool_use`
   with no matching `tool_result` — the fingerprint of a previous stream that was
   abandoned mid-turn — and injects errored results so the model can answer around them.
5. **The graph runs.** `agent` calls Claude with the tools attached and streams text
   deltas. If the reply contains `tool_use` blocks, the conditional edge routes to `tools`,
   which executes them and appends `tool_result`s; control returns to `agent`. This repeats
   until the model returns text alone or the step cap is reached.
6. **Response.** `/ask` returns `{answer, sources, conversation_id}`. `/ask/stream` has
   been emitting events the whole time and closes with a `done` frame.

## The graph is two nodes and a conditional edge

```
START → agent → (tool_use blocks?) → tools → agent → … → END
```

That conditional edge **is** the router. It dispatches on what the model actually asked
for, rather than on a classification made in advance.

There is deliberately no separate "documentation vs data" classifier node. A question like
*"what is a STAC Item, and which Sentinel-2 scenes cover Rome last January?"* is both, and
an upfront decision cannot express that. It would also cost an extra model call to decide
something the model decides for free as part of the turn it is already taking.

**The step cap is a hard cap.** Once `MAX_AGENT_STEPS` rounds have been spent, the `agent`
node stops passing `tools`, so the final call has no choice but to answer with what was
gathered. Tools run at most `MAX_AGENT_STEPS` times, the model is called at most
`MAX_AGENT_STEPS + 1` times, and the caller always gets an answer rather than a timeout.

`recursion_limit` is derived from the cap (`2 * max + 5`) because agent and tools
alternate; LangGraph's default of 25 would otherwise fire first.

## Two entry points, one graph

`answer_question` invokes the graph; `stream_answer` streams it. They share `_turn_input`
and `_turn_config`, so the per-turn reset and the recursion limit cannot drift apart, and
they share the compiled graph and therefore the checkpointer — **a conversation can move
between them**. A test pins that the two paths agree.

The streaming path emits one JSON object per SSE `data:` line, with the type inside the
object rather than as a named `event:` line. `json.dumps` escapes newlines, so a frame is
always exactly one line and the client parser stays trivial.

| Event | Carries |
|---|---|
| `start` | `conversation_id`, before the graph runs |
| `token` | a fragment of text, as the model writes it |
| `tool_start` | `id`, `name`, `input` |
| `tool_end` | `ok`, `ms`, and `detail` when it failed |
| `features` | footprints so far, as a GeoJSON `FeatureCollection` |
| `done` | `answer`, `sources`, `steps` |
| `error` | a failure after the response had already begun |

Two things about those events that look like bugs and are not. The streamed tokens are a
**superset** of `done.answer` — they come from every agent turn, including the "let me
check" a model writes next to a tool call, while `answer` is the last turn alone. And a
turn that ran no tools sends **no `features` event at all**, rather than an empty
collection, which is what leaves the map showing the scenes a follow-up is about.

## State, and what deliberately is not state

The agent state has two kinds of field, and the reducer is the difference:

- **Accumulating across the thread:** `messages`, `turns`, `cost_usd`. These ride the
  checkpointer, which is what makes the conversation budget per-conversation.
- **Reset every turn:** `steps`, `sources`, `features`. These describe the turn just taken.

So a follow-up answered purely from history legitimately returns empty sources and no
footprints — nothing ran. That is a feature, not a gap in the reporting.

Two things stay *out* of the state on purpose. The SQLAlchemy `Session` travels in the
LangGraph **context**, because a checkpointed session would come back on a resumed
conversation already closed. The per-turn `Turn` trace object rides there too, for the same
reason: it holds a clock reading and an open span.

Assistant turns are stored as plain dicts rather than Anthropic SDK objects, because the
checkpointer serializes the state and SDK objects either fail to serialize or return as
dicts on resume — which would force every reader to handle both shapes.

## Why the agent node is synchronous

It streams — it calls `messages.stream()` and pushes deltas onto LangGraph's custom channel
— but it is a normal `def`, and that is deliberate.

An async-only node makes `graph.invoke()` raise `TypeError`, so `answer_question` would
need an `asyncio.run` bridge. A module-cached `AsyncAnthropic` holds pooled connections
that die with the loop that created them, so the second `/ask` onwards would fail with
`RuntimeError: Event loop is closed`. Under `.invoke()` the stream writer simply goes
nowhere, so **one node body serves both entry points** and there is no async twin to keep
in step.

## What the model sees, and what the map sees

The single most important thing `stac_search` does is throw most of the catalog response
away. A Sentinel-2 L2A item on Earth Search carries 35 assets, full geometry and a list of
links: **69,727 bytes for three scenes, against 2,516 after projection.**

Geometry is the interesting case, because two consumers want different things:

- `_summarize_item` keeps `geometry` on the internal model.
- `model_view` strips it out before the result is serialized into a `tool_result` — the
  model is only made worse by a polygon.
- `item_footprint` turns it into a GeoJSON feature for the map, and the `tools` node
  accumulates those into `features`, deduped by id.

`LookupResult.scored` is the same idea on the retrieval side: cosine distances that reach
the tracing but never the model or the answer.

## Why previews and asset downloads are proxied

Both go through this API rather than straight to the catalog, for reasons that do not
overlap.

**Previews** (`GET /preview/{item_id}`) because the map needs the image as a WebGL texture,
which makes it a CORS request against a host under no obligation to allow it — and because
S3 sends `Access-Control-Allow-Origin` only when `Origin` is present while returning no
`Vary`, so one ordinary `<img>` load poisons the browser cache for every later CORS request
to that URL. Proxying removes the class of problem rather than the instance.

The endpoint takes an **item id, never a URL**. The href is resolved through the configured
catalog, so the only thing it can fetch is what that catalog returned for that id.

**Asset downloads** (`GET /items/{id}/assets/{key}`) for three unrelated reasons:
Sentinel-1 publishes bands as `s3://`, which a browser cannot follow; every Sentinel-2
scene's red band is called `B04.tif`, so `Content-Disposition` is what makes ten of them
distinguishable in a downloads folder; and a catalog that blocks hotlinking keeps working.
Unlike the preview it **streams** and has no size cap — a Sentinel-1 GRD band is 721 MB,
and the size is the feature.

## Deployment

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/infra-dark.svg">
  <img alt="EO-RAG AWS deployment: an internet-facing ALB in front of a single ECS Fargate task and an RDS PostgreSQL instance with pgvector, in the account's default VPC, alongside Secrets Manager, Bedrock, the Anthropic API and Earth Search." src="images/infra.svg">
</picture>

The Terraform is split on **lifetime**, not on layer. `infra/persistent/` holds the state
bucket and the ECR repository and is created once, ever. `infra/` holds the ALB, ECS, RDS,
Secrets Manager and IAM, and is designed to be created and destroyed often — without the
split, every cycle would also pay for a rebuild and push of a `rasterio`/GDAL image.

The default VPC is used on purpose: a private-subnet design would need a NAT Gateway, the
largest line item it would add. RDS sits in a public subnet with
`publicly_accessible = false`, so it never receives a public IP.

Full instructions, sizing and the explicit no-authentication warning are in
[infra/README.md](../infra/README.md).
