# EO-RAG

**Ask questions about Earth observation data in plain language — and get answers grounded
in both the specification and the live catalog.**

[![CI](https://github.com/bbrauzzi/eo-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/bbrauzzi/eo-rag/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

<p align="center">
  <img src="docs/images/demo.gif" width="960"
       alt="One question — &quot;What is a STAC Item, and which Sentinel-2 scenes cover Rome in January 2024 with less than 20% cloud?&quot; — answered by chaining a documentation lookup and a live catalog search: the tool trace shows both running, the answer streams in with the scene cards below it, three footprints are drawn over Lazio, and clicking one lays that scene's quicklook over it.">
</p>

<p align="center"><sub>One question, both halves: the STAC specification looked up in the indexed docs, and the matching Sentinel-2 scenes fetched live from the catalog and drawn on the map.</sub></p>

Working with satellite imagery means holding two things in your head at once: what the
data model says, and what is actually in the archive. *What exactly is a STAC Item?* is a
documentation question. *Which Sentinel-2 scenes cover Rome last January under 20% cloud?*
is a catalog question. *And how green was it?* is neither — it needs the pixels.

EO-RAG answers all three in one conversation. It is a self-hosted assistant that combines
retrieval over EO technical documentation with live tool calls against real STAC catalogs,
and draws what it finds on a map.

Built for **EO analysts, geospatial developers and data teams** who want a faster path from
question to scene than reading the specification and hand-writing catalog queries.

---

## What it does

- **Answers documentation questions with citations.** The STAC specification is chunked,
  embedded and searched by meaning, and every answer names the sources it used.
- **Searches live STAC catalogs.** Place, time window and cloud cover in natural language;
  scene identifiers, acquisition times and cloud percentages back.
- **Measures spectral indices on demand.** NDVI and NDWI computed from the real
  Cloud-Optimized GeoTIFF pixels of a chosen scene, over a bounding box you describe.
- **Draws the results.** Scene footprints on a MapLibre map; click one to lay that scene's
  quicklook over its actual four corners.
- **Streams as it thinks.** Tokens appear as the model writes them, and each tool call
  shows what it is doing, whether it succeeded, and how long it took.
- **Downloads the data.** Browse a scene's full asset list and pull individual bands
  through the API, including catalogs that publish over `s3://`.
- **Remembers the conversation.** Follow-ups resolve against what was already found — "which
  of those has the least cloud?" needs no second search.
- **Bounds what it can spend.** A hard cap on tool rounds per turn, cost and turn budgets
  per conversation, and per-caller rate limiting.
- **Reports on itself.** Every turn writes a structured trace — tools used, tokens, latency,
  retrieval distances — to the log, with optional export to Langfuse.
- **Doubles as an MCP server.** The same three tools and the same corpus, available to any
  Model Context Protocol client.

## How it works

One FastAPI process serves the JSON API, the built React interface and the MCP endpoint.
Behind it is a single agent with three tools; Claude decides which to call and in what
order, and the loop runs until it produces an answer.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.svg">
  <img alt="EO-RAG system architecture: clients on the left, the FastAPI application containing the rate limiter, endpoints, LangGraph agent and three tools in the middle, and the external services each tool talks to on the right." src="docs/images/architecture.svg">
</picture>

There is no upfront "documentation or data?" classifier, on purpose — plenty of real
questions are both, and the model decides for free as part of the turn it is already
taking. See [docs/architecture.md](docs/architecture.md) for the request lifecycle, and
[docs/decisions.md](docs/decisions.md) for why each part is built this way.

## Quickstart

**Prerequisites**

- Docker and Docker Compose.
- An **Anthropic API key**.
- **AWS credentials** reachable by boto3 (environment variables, an `~/.aws/credentials`
  profile, or an instance role), with the **Titan Text Embeddings V2** model
  (`amazon.titan-embed-text-v2:0`) enabled in your `AWS_REGION` — Bedrock console >
  *Model access*. Without it, calls fail with `AccessDeniedException`.

```bash
git clone https://github.com/bbrauzzi/eo-rag.git
cd eo-rag

cp .env.example .env          # fill in ANTHROPIC_API_KEY and your AWS config

docker compose up -d          # Postgres+pgvector on :5432, API + UI on :8000
curl http://localhost:8000/health
```

Index the bundled STAC specification so documentation questions have something to search:

```bash
docker compose exec api python -m app.rag.ingest data/stac-spec-core.md --source "stac-spec.md"
```

Then open **<http://localhost:8000/>** and ask something.

> The API container runs `alembic upgrade head` before starting, so the schema is never a
> separate step. Catalog search and index computation need no ingestion — only network
> access to `STAC_API_URL`.

## Using it

### The web interface

`docker compose up -d` builds the frontend into the API image and serves it at
<http://localhost:8000/>. Answers stream in, tool activity is shown live, scene footprints
are drawn as they arrive, and clicking a footprint toggles that scene's quicklook.

### The HTTP API

**`POST /ask`** — one request, one answer.

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024 with less than 20% cloud?"}'
```

Returns `{answer, sources, conversation_id}`. `sources` names what actually ran — the
document name for documentation lookups, the catalog URL for catalog searches. It is **per
turn**, so a follow-up answered from history reports none.

Pass the `conversation_id` back to continue:

```bash
CID=$(curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024?"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["conversation_id"])')

curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"Which of those has the least cloud?\", \"conversation_id\": \"$CID\"}"
```

**`POST /ask/stream`** — the same request body, reported as it happens. One JSON object per
`data:` line, with the type inside it.

| Event | Carries |
|---|---|
| `start` | `conversation_id`, before the graph runs — a stream that dies halfway is still resumable |
| `token` | a fragment of text, as the model writes it |
| `tool_start` | `id`, `name`, `input` |
| `tool_end` | `ok`, `ms`, and `detail` when it failed |
| `features` | the footprints so far, as a GeoJSON `FeatureCollection` |
| `done` | `answer`, `sources`, `steps` |
| `error` | a failure after the response had already begun |

```bash
curl -N -X POST http://localhost:8000/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024?"}'
```

Two things worth knowing about the events:

- **The streamed text is a superset of `done.answer`.** Tokens come from every agent turn,
  including the "let me check" a model writes next to a tool call; `answer` is the last turn
  alone. The UI renders the stream and uses `answer` only as a fallback.
- **A turn that runs no tool sends no `features` event at all**, rather than an empty
  collection — so a follow-up about the scenes already on screen leaves them there.

**`GET /preview/{item_id}`** — a scene's quicklook, proxied rather than loaded from the
catalog's asset host. It takes an **item id, not a URL**: the href is resolved through the
configured catalog, so nothing can point the API at an arbitrary host.

```bash
curl -s -o rome.jpg http://localhost:8000/preview/S2B_33TTG_20240130_0_L2A
```

**`GET /items/{item_id}/assets`** lists a scene's assets; **`GET /items/{item_id}/assets/{key}`**
streams one, with the scene id in the filename. Deliberately unfiltered — this is the
endpoint a person uses to get at exactly the band they want.

### As an MCP server

The same three tools and the same documentation, reachable by any MCP client rather than
only through this project's own agent.

```bash
uv sync --extra mcp
```

| Tool | What it returns |
|---|---|
| `stac_search` | Matching scenes — id, collection, datetime, cloud cover, asset names. Footprint polygons are **left out by default**; pass `include_geometry: true` for them. |
| `compute_index` | NDVI or NDWI statistics over a bbox of one scene. Reads real pixels, so 5–15 seconds. |
| `rag_lookup` | The best-matching documentation passages, as prose with `[Source: ...]` labels. |

Three resources. Start at the index — MCP can advertise a template's shape but never its
values, so `docs://sources` is the only way a client learns what exists:

```
docs://sources                      every document, its sections, its chunk count
docs://document/{source}            one document, reassembled in document order
docs://section/{source}/{section}   one section — prefer this, the corpus is 65 KB
```

**stdio**, for a desktop client — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "eo-rag": {
      "command": "uv",
      "args": ["--directory", "/path/to/eo-rag", "run", "--extra", "mcp", "eo-rag-mcp"],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://eorag:eorag@localhost:5432/eorag",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

`--directory` is load-bearing: `app/config.py` reads `env_file=".env"`, a *relative* path,
and a desktop client launches its servers with the working directory set to `/`. Without it
the `.env` is silently not read and every setting falls back to its default.

For Claude Code:

```bash
claude mcp add eo-rag -- uv --directory /path/to/eo-rag run --extra mcp eo-rag-mcp
```

**HTTP**, from the running container — `docker compose up -d` already serves it:

```bash
claude mcp add --transport http eo-rag http://localhost:8000/mcp
```

The endpoint really lives at `/mcp/`; a bare `/mcp` is a 307 that every client tested
follows. The SDK also validates the `Host` header and refuses anything but localhost **with
a port** — behind a proxy or a real hostname set `MCP_ALLOWED_HOSTS`, or every request comes
back `421 Invalid Host header`.

Only `rag_lookup` and the documentation resources need Postgres and Bedrock; `stac_search`
and `compute_index` need nothing but network access. A broken `DATABASE_URL` therefore
degrades to two working tools rather than a dead server.

## Configuration

Everything is environment variables, read by `app/config.py`.
[`.env.example`](.env.example) is the annotated reference — the table below is the short
version.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | An unrecognised value is priced at the most expensive known model, on purpose. |
| `DATABASE_URL` | local compose default | `postgresql://` URLs are pinned to psycopg v3. |
| `AWS_REGION` | `us-east-1` | Must have Titan embeddings model access enabled. |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | Titan v2 / `1024` | Changing these means re-ingesting; see below. |
| `STAC_API_URL` | Earth Search v1 | **A trusted input** — see [SECURITY.md](SECURITY.md). |
| `ALLOWED_COLLECTIONS` | Sentinel-1/2 ids | Empty means no constraint. Also named in the tool schema. |
| `MAX_AGENT_STEPS` | `5` | Hard cap on tool rounds per turn. |
| `MAX_CONVERSATION_TURNS` / `MAX_CONVERSATION_COST_USD` | `20` / `1.00` | Per conversation. Either at `0` disables its own check. |
| `RATE_LIMIT_*` | on, 10/min for `/ask` | Per caller. See `.env.example` for the full tier list. |
| `LANGFUSE_*` | off | Optional trace export; needs `uv sync --extra observability`. |
| `MCP_HTTP_ENABLED` / `MCP_ALLOWED_HOSTS` | on / localhost | Not optional behind a proxy. |

Embeddings from different models are not comparable, so changing the model means wiping and
re-indexing:

```sql
TRUNCATE doc_chunks;
```

If the *dimension* changes too, update `EMBEDDING_DIM` — `app/db/models.py` and the initial
Alembic migration both read it from there, so that is the only place to change it — then
either write a migration to `ALTER COLUMN ... TYPE vector(new_dim)` or drop the volume and
let `alembic upgrade head` recreate the column at the new width.

## Deploying

`infra/` contains Terraform for a small, inexpensive AWS deployment: an internet-facing ALB
in front of a single ECS Fargate task, with RDS PostgreSQL and pgvector behind it.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/infra-dark.svg">
  <img alt="EO-RAG AWS deployment: an internet-facing ALB in front of a single ECS Fargate task and an RDS PostgreSQL instance with pgvector in the account's default VPC, alongside Secrets Manager, Bedrock, the Anthropic API and Earth Search." src="docs/images/infra.svg">
</picture>

It is split into two Terraform stacks on **lifetime** rather than layer: `infra/persistent/`
(state bucket, ECR repository) is created once and never destroyed, while `infra/` is
designed to be created and torn down at will with `./infra/deploy.sh` and
`./infra/undeploy.sh`. Without that split, every cycle would also pay for rebuilding and
pushing a `rasterio`/GDAL image.

> **No authentication sits in front of `/ask`.** The built-in guardrails bound *cost*, not
> *access* — anyone with the URL can spend your model budget. Put authentication and TLS in
> front of the load balancer before this is anything more than a short-lived demo. See
> [SECURITY.md](SECURITY.md).

Full instructions, sizing and teardown are in [infra/README.md](infra/README.md).

## Development

```bash
uv run --extra dev pytest -q                        # the offline suite
uv run --extra dev pytest tests/test_ingest.py -q   # one file
uv run --extra dev ruff check .                     # lint
```

The suite runs **fully offline** — no AWS credentials, no network, no database. Every module
that talks to the outside world builds its client lazily so that importing the app never
touches anything external.

It must also pass **with and without** each optional extra, which needs an explicit sync
because `uv run --extra` is additive:

```bash
uv sync --extra dev && uv run --no-sync pytest -q   # 1 skipped: tests/test_mcp_server.py
uv run --extra dev --extra mcp pytest -q            # everything
```

Running without Docker:

```bash
docker compose up -d db      # database only
uv run alembic upgrade head  # this path does NOT run migrations for you
uv run uvicorn app.main:app --reload
```

That serves the API only. The frontend is built in the Docker image's node stage, never in
the repository, so a checkout with no `frontend_dist/` has nothing mounted at `/` — run the
Vite dev server alongside it:

```bash
cd frontend && npm install
npm run dev        # :5173, proxying the API
npm test           # vitest
```

The dev server's proxy is why there is **no CORS middleware anywhere**: in development the
browser sees a single origin, and in production FastAPI serves the built assets from its own
port.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow, and [VERIFY.md](VERIFY.md) for
the live checks that cover what an offline suite structurally cannot.

## Project status

Working and in use: ingestion and retrieval, all three tools, the agent loop with streaming,
the chat interface and map, guardrails, tracing, the eval harness and the MCP server. CI
runs lint, both test configurations and the frontend build on every push.

Known limitations, stated plainly:

- **Conversation history is in memory** and dies with the process. Persisting it needs the
  separate `langgraph-checkpoint-postgres` package; the state shape is already ready for it.
- **No authentication.** See above, and [SECURITY.md](SECURITY.md).
- **One catalog at a time**, set by `STAC_API_URL`.
- **The rate limiter is per process**, so N workers means an effective limit of N x the
  configured value.
- **Retrieval is labelled by section in the eval set**, which makes `recall@k` saturate;
  MRR is the metric that discriminates. The first baseline showed genuinely mediocre
  retrieval distances on core questions — see [docs/decisions.md](docs/decisions.md).

Next up: authentication, persisted conversation history, and multi-catalog support.

## Documentation

| Document | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The request lifecycle, the graph, state, and why the proxies exist |
| [docs/decisions.md](docs/decisions.md) | Why each part is built the way it is, including the bugs that shaped it |
| [VERIFY.md](VERIFY.md) | The live verification runbook — what the offline suite cannot see |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, the testing rules, migrations, evals |
| [SECURITY.md](SECURITY.md) | Threat model, what the guardrails do and do not bound, reporting |
| [infra/README.md](infra/README.md) | AWS deployment, sizing, teardown |
| [CLAUDE.md](CLAUDE.md) | Module-level design notes, for anyone (or anything) editing the code |

## License

[MIT](LICENSE).

Built with [FastAPI](https://fastapi.tiangolo.com/), [LangGraph](https://langchain-ai.github.io/langgraph/),
[pgvector](https://github.com/pgvector/pgvector), [rasterio](https://rasterio.readthedocs.io/),
[MapLibre GL](https://maplibre.org/) and [Anthropic Claude](https://www.anthropic.com/claude).
Catalog data from [Earth Search](https://earth-search.aws.element84.com/v1) by Element 84;
basemaps from [OpenFreeMap](https://openfreemap.org/).
