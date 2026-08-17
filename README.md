# EO-RAG

Conversational assistant over EO/STAC technical documentation: RAG over technical
specifications + tool calling against real STAC catalogs.

Covers steps 0-6 of the architecture described in `eo-copilot-architettura.md`
(Setup, Ingestion, RAG endpoint, `stac_search` tool, LangGraph orchestration,
`compute_index`, and the chat interface with a map).

`POST /ask` runs a LangGraph agent: Claude decides whether to look something up in the
indexed documentation (`rag_lookup`), query a live STAC catalog (`stac_search`), measure
a spectral index over a scene's pixels (`compute_index`), or several of those, and
answers from what comes back. Conversations are remembered across turns.

`docker compose up -d` then <http://localhost:8000/> gives the interface: the answer
streams in as it is written, the tools show what they are doing while they do it, and
the scenes it finds are drawn on a map — click a footprint to lay its quicklook over it,
click again to take it off.

## Prerequisites

- AWS credentials reachable by boto3 (environment variables, the
  `~/.aws/credentials` profile, or an instance role).
- Access to the **Titan Text Embeddings V2** model (`amazon.titan-embed-text-v2:0`)
  enabled in the region configured in `AWS_REGION`: Bedrock console >
  *Model access*. Without it, calls fail with `AccessDeniedException`.

## Local setup

```bash
# 1. Copy the env file and fill in your API keys / AWS config
cp .env.example .env

# 2. Start Postgres + pgvector and the API
docker compose up -d

# 3. Check everything is up
curl http://localhost:8000/health
```

## Indexing a document (Step 1)

Put a markdown file in the `data/` folder and run:

```bash
docker compose exec api python -m app.rag.ingest data/stac-spec-core.md --source "stac-spec.md"
```

Note: embeddings from different providers/models are not comparable with each other.
If you change embedding provider or model you have to wipe and reindex:

```sql
TRUNCATE doc_chunks;
```

(then re-run the ingestion above; if the dimension changes too, update `EMBEDDING_DIM`
— `app/db/models.py` and the initial Alembic migration both read it from there, so
that's the only place to change it — then either write a migration to
`ALTER COLUMN ... TYPE vector(new_dim)`, or drop the volume and let
`alembic upgrade head` recreate the column at the new width)

## Checking the retrieval quality

Runs a set of test questions through the same embedding + pgvector path used by
`/ask`, and prints the top-k chunks with their cosine similarity — no LLM in the
way, so you can see the ranking itself:

```bash
docker compose exec api python -m scripts.retrieve_test --top-k 3
docker compose exec api python -m scripts.retrieve_test --query "which fields are required on an Item?"
```

From the host instead of the container, point `DATABASE_URL` at the published port:

```bash
DATABASE_URL="postgresql://eorag:eorag@localhost:5432/eorag" uv run python -m scripts.retrieve_test
```

## Asking a question (Steps 2-3)

A documentation question, answered from the indexed chunks via `rag_lookup`:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What are STAC Items and how are they structured?"}'
```

A data question, answered from the catalog via `stac_search` (no ingestion needed, but
`STAC_API_URL` has to be reachable):

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024 with less than 20% cloud?"}'
```

Which tool to call is the model's decision. `MAX_AGENT_STEPS` caps how many rounds of
tools it gets before it has to answer with what it has.

### Continuing a conversation (Step 4)

Every answer carries a `conversation_id`. Pass it back to ask a follow-up against the
same history:

```bash
CID=$(curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024?"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["conversation_id"])')

curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"Which of those has the least cloud?\", \"conversation_id\": \"$CID\"}"
```

History lives in memory and dies with the process — persisting it to Postgres is the
remaining part of step 4.

The `sources` field tells you which tools actually ran: the ingested document name for
`rag_lookup`, the catalog URL for `stac_search`. It is per turn, so a follow-up answered
from what is already in the history reports no sources at all.
[VERIFY.md](VERIFY.md) walks through checking the whole path against the live services,
which the offline test suite cannot.

## The interface (Step 6)

`docker compose up -d` builds the frontend into the API image and serves it at
<http://localhost:8000/>. To work on it instead, run the dev server, which proxies the
API and reloads on save:

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
npm test               # the SSE frame parser
```

The proxy is why there is no CORS middleware anywhere: in development the browser sees a
single origin, and in production FastAPI serves the built assets from its own port.

### Streaming (`POST /ask/stream`)

Same request body as `/ask`, reported as it happens. One JSON object per `data:` line,
the type inside it:

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

- **The streamed text is a superset of `done.answer`.** Tokens come from every agent
  turn, including the "let me check" a model writes next to a tool call; `answer` is the
  last turn alone. The UI renders the stream and uses `answer` only as a fallback.
- **A turn that runs no tool sends no `features` event at all**, rather than an empty
  collection — so a follow-up about the scenes already on screen leaves them there. It is
  the same property that makes `sources` empty on such a turn.

`/ask` itself is unchanged: it still returns `{answer, sources, conversation_id}` in one
response, and both endpoints share a graph and therefore a conversation.

### Scene previews (`GET /preview/{item_id}`)

The image the map lays over a footprint, and the one on each card. It is proxied rather
than loaded from the catalog's asset host, because the map needs it as a WebGL texture —
a CORS request against a host under no obligation to allow it. Same origin here means no
CORS is involved at all, and a catalog that sends no such headers still works.

It takes an **item id, not a URL**: the href is resolved through the configured catalog,
so nothing can point the API at an arbitrary host.

```bash
curl -s -o rome.jpg http://localhost:8000/preview/S2B_33TTG_20240130_0_L2A
```

## The MCP server (Step 10)

The same three tools and the same documentation, reachable over the Model Context Protocol
by any MCP client rather than only through this project's own agent.

```bash
uv sync --extra mcp
```

**Three tools**, named exactly as the agent's own:

| Tool | What it returns |
|---|---|
| `stac_search` | Matching scenes — id, collection, datetime, cloud cover, asset names. Footprint polygons are **left out by default**; pass `include_geometry: true` for them. |
| `compute_index` | NDVI or NDWI statistics over a bbox of one scene. Reads real pixels, so 5–15 seconds. |
| `rag_lookup` | The best-matching documentation passages, as prose with `[Source: ...]` labels. |

**Three resources.** Start at the index — MCP can advertise a template's shape but never
its values, so `docs://sources` is the only way a client learns what exists:

```
docs://sources                      every document, its sections, its chunk count
docs://document/{source}            one document, reassembled in document order
docs://section/{source}/{section}   one section — prefer this, the corpus is 65 KB
```

### stdio, for a desktop client

Add to `claude_desktop_config.json`:

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

**`--directory` is load-bearing.** `app/config.py` reads `env_file=".env"`, a *relative*
path, and a desktop client launches its servers with the working directory set to `/` —
without it the `.env` is silently not read and every setting falls back to its default.

For Claude Code:

```bash
claude mcp add eo-rag -- uv --directory /path/to/eo-rag run --extra mcp eo-rag-mcp
```

Only `rag_lookup` and the documentation resources need Postgres and Bedrock; `stac_search`
and `compute_index` need nothing but network access. A broken `DATABASE_URL` therefore
degrades to two working tools rather than a dead server.

### HTTP, from the running container

`docker compose up -d` already serves it — the image installs the extra:

```bash
claude mcp add --transport http eo-rag http://localhost:8000/mcp
```

Two things about that URL. The endpoint really lives at `/mcp/`; a bare `/mcp` is a 307
that every client tested follows. And the SDK validates the `Host` header, refusing
anything but localhost **with a port** — behind a proxy or a real hostname set
`MCP_ALLOWED_HOSTS`, or every request comes back `421 Invalid Host header`.

## Development without Docker

```bash
uv venv
uv pip install -e ".[dev]"
uv run uvicorn app.main:app --reload
```

(you still need a reachable Postgres+pgvector: you can keep just the `db`
service with `docker compose up -d db` and run the API locally)

This serves the API only. The frontend is built in the Docker image's node stage, never
in the repository, so a checkout with no `frontend_dist/` simply has nothing mounted at
`/` — run `npm run dev` alongside it for the interface.

## Next steps

**Steps 0-10 are all done**: ingestion, retrieval, the three tools, the LangGraph agent,
the chat interface and map, guardrails, observability, the eval harness and the MCP server.

What is left is the cross-cutting list rather than a next step — and the two that now cost
the most are **version control** (ten steps of decisions with no history of any of them)
and **CI** (ruff, pytest with and without each optional extra, the frontend build, and the
free half of the eval harness).

The full activity list, with what is already in place and why each decision was taken,
lives in [ROADMAP.md](ROADMAP.md).
