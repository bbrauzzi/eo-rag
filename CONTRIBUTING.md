# Contributing

Thanks for taking a look. This document covers how to get a development environment
running, what the test suite does and does not cover, and the one rule that is easy to
miss and expensive to get wrong.

## Setting up

Python tooling runs through [`uv`](https://docs.astral.sh/uv/). There is no committed
virtualenv.

```bash
git clone https://github.com/bbrauzzi/eo-rag.git
cd eo-rag
cp .env.example .env          # fill in ANTHROPIC_API_KEY and your AWS config
```

The offline test suite needs none of those values. They are only needed to actually run
the app.

```bash
docker compose up -d          # Postgres+pgvector on :5432, API + UI on :8000
uv run --extra dev pytest -q  # the suite
```

The API container's entrypoint runs `alembic upgrade head` before uvicorn, so the schema is
never a separate step on that path. If you instead run the database alone and the API
locally, you have to apply migrations yourself first:

```bash
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The frontend has its own toolchain and is not wired into `pytest`:

```bash
cd frontend
npm install
npm run dev        # :5173, proxying /ask, /ask/stream and /health to :8000
npm test           # vitest, over the SSE frame parser and imageCorners
npm run build      # what the Docker image's node stage runs
npx tsc -b --noEmit
```

## The rule that is easy to miss

**The suite must pass with and without each optional extra.** Tracing works without
Langfuse; the MCP adapters are testable without the MCP SDK. That is the entire point of
both being separate extras, and it is enforced in CI.

`uv run --extra dev` is **additive** — it will not remove an extra that a previous command
installed. So the without-the-extra run needs an explicit sync:

```bash
uv sync --extra dev && uv run --no-sync pytest -q   # expect 1 skipped: tests/test_mcp_server.py
uv run --extra dev --extra mcp pytest -q            # everything
uv run --extra dev ruff check .
```

If the first command does not report a skip, you are accidentally testing with the extra
installed and the guard is not doing anything.

## What the offline suite covers, and what it cannot

The suite runs **fully offline**: no AWS credentials, no network, no database. Every module
that talks to the outside world builds its client lazily into a module-level
`_cached_client` behind a `_client()` function, and tests monkeypatch that function. Each of
those modules also has an import-purity test that reloads it with the client constructor
sabotaged.

**Keep new tests to that bar.** Nothing in `app/` should acquire a client or a connection at
import time.

A few conventions worth knowing before you write a fake:

- Fakes for the Anthropic client return the **real** SDK block types (`TextBlock`,
  `ToolUseBlock`), because the graph calls `model_dump()` on them and the checkpointer
  serializes the result. A hand-rolled fake hid both steps and let a serialization bug
  through.
- Tool fakes return the **real** pydantic result models for the same reason.
- Read configuration from `app.config.settings` rather than hardcoding defaults —
  `settings` is instantiated at import and reads `.env` if one is present.
- The rate limiter is middleware, so `tests/conftest.py` turns it off for the whole suite;
  `tests/test_ratelimit.py` turns it back on and drives a fake clock.

### The part the suite is blind to

The offline suite cannot see an **external service behaving differently from how we
imagined it**. Three real bugs were found only by running against live services: Earth
Search rejecting a bare `2024-01-01` date, a `MAX_TOKENS` truncation that silently cut an
answer in half, and a reflectance offset the catalog advertises but its own COGs do not
apply.

So: **if you change the request shape of a tool, the graph, or the prompts, run the live
checks in [VERIFY.md](VERIFY.md) as well.** They cost a handful of cents. That document is
the project's stated division of labour — anything visual or service-dependent is verified
there rather than mocked here.

## Migrations

`alembic/` owns the schema.

```bash
alembic revision -m "add a column"
alembic upgrade head
```

`app/db/models.py` mirrors the schema by hand for SQLAlchemy's benefit; Alembic does not
read the models at runtime. `alembic.ini` deliberately carries no `sqlalchemy.url` —
`alembic/env.py` takes it from `app.config.settings`, so there is no second copy to drift
from `.env`.

## The eval harness

Not part of `pytest`, because it needs live services and spends money.

```bash
python -m scripts.eval --smoke           # are the services up? no cases, no spend
python -m scripts.eval --retrieval-only  # embeddings + pgvector only, free
python -m scripts.eval                   # everything, live model and catalog calls
python -m scripts.eval --compare         # judge against evals/baseline.json
python -m scripts.eval --save-baseline   # accept the current scores as the standard
```

If you change chunking, the embedding model, retrieval or the prompts, run `--compare` and
say what moved in your pull request. If a metric legitimately got worse in exchange for
something else, say that too — the gate exists to start a conversation, not to be silently
re-baselined.

## Pull requests

- Keep the diff to one concern.
- Run `ruff check .`, both `pytest` invocations above, and the frontend build if you touched
  `frontend/`.
- Explain the *why*, not just the *what*. This codebase documents reasoning heavily
  (see [docs/decisions.md](docs/decisions.md)); a change that invalidates a documented
  decision should update that document in the same pull request.
- If you ran the live checks, say which ones and what you saw.

## Style

Comments, docstrings, prompts and user-facing strings are in **English** throughout —
including the system prompt and the `[Source: ...]` citation label, both of which shape the
language and format of the model's answers.

`ruff` is configured in `pyproject.toml` with `target-version = "py311"` and
`line-length = 120`. The rule set is ruff's default and stays that way.
